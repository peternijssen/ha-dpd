"""DPD Germany Paketnavigator SOAP session: KeyPhase signing + lifecycle.

Every call to api.paketnavigator.de carries a PartnerCredentials block with a
time-based MD5 KeyPhase signature. This module owns that signing and the
session-token lifecycle; other DE modules call into it rather than touching
the wire directly.

No refresh operation exists — polling keeps the session alive, since every
getSessionFullState response re-issues SessionToken. Reauth is error-driven
(ERROR_SESSION_NOT_VALID / ERROR_KEYPHASE) and retried once, never looped.
Two consecutive ERROR_KEYPHASE responses likely mean the app secret rotated,
not routine session expiry — see :func:`_warn_keyphase_rotation_once`.
"""
from __future__ import annotations

import base64
import hashlib
import logging
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from typing import Any

import aiohttp

from ...const import (
    DPD_DE_CLOCK_SKEW_TOLERANCE_MINUTES,
    DPD_DE_LANGUAGE,
    DPD_DE_PARTNER_NAME,
    DPD_DE_PARTNER_SECRET,
    DPD_DE_PARTNER_TOKEN,
    DPD_DE_SOAP_NAMESPACE,
    DPD_DE_SOAP_URL,
    DPD_DE_USER_AGENT,
    DpdApiError,
    DpdAuthError,
)

_LOGGER = logging.getLogger(__name__)

_NEW_ISSUE_URL = (
    "https://github.com/ha-parcel-integrations/ha-dpd/issues/new"
    "?template=unrecognised_status.yml"
)

# cloudUserID for the one call that happens before any account is known.
_ANONYMOUS_CLOUD_USER_ID = "0"

# A bad signature and a dead token report the same codes.
_SESSION_ERROR_CODES = frozenset({"ERROR_SESSION_NOT_VALID", "ERROR_KEYPHASE"})

_keyphase_rotation_warned = False
_clock_skew_warned = False


def compute_key_phase(now_utc: datetime, cloud_user_id: str, method_name: str) -> str:
    """Return the KeyPhase signature for one SOAP call.

        p = ((utc_hours * 60 + utc_minutes + 1000) * 3)
        KeyPhase = str(p) + base64(md5(
            str(p) + partner_name + cloud_user_id + method_name + partner_secret
        ))[:16]

    ``method_name`` is the bare operation name and varies per call, so this
    cannot be cached — it also rotates every minute, so ``now_utc`` must be
    the real call time, always UTC.
    """
    p = (now_utc.hour * 60 + now_utc.minute + 1000) * 3
    digest_input = (
        f"{p}{DPD_DE_PARTNER_NAME}{cloud_user_id}{method_name}{DPD_DE_PARTNER_SECRET}"
    )
    digest = hashlib.md5(digest_input.encode("utf-8")).digest()  # noqa: S324 - carrier's own scheme, not ours to choose
    b64 = base64.b64encode(digest).decode("ascii")
    return f"{p}{b64[:16]}"


def _strip_ns(tag: str) -> str:
    """Return an XML tag name with its ``{namespace}`` prefix removed."""
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def _leaf_text_value(text: str | None) -> Any:
    """Coerce a leaf element's text: XSD ``true``/``false`` literals to ``bool``, else ``str``."""
    if not text or not text.strip():
        return None
    value = text.strip()
    if value in ("true", "false"):
        return value == "true"
    return value


def _element_to_value(element: ET.Element) -> Any:
    """Convert one XML element into a dict/list/bool/str/``None``.

    Repeated sibling tags fold into a list.
    """
    children = list(element)
    if not children:
        return _leaf_text_value(element.text)

    grouped: dict[str, list[Any]] = {}
    order: list[str] = []
    for child in children:
        tag = _strip_ns(child.tag)
        if tag not in grouped:
            grouped[tag] = []
            order.append(tag)
        grouped[tag].append(_element_to_value(child))

    result: dict[str, Any] = {}
    for tag in order:
        values = grouped[tag]
        result[tag] = values[0] if len(values) == 1 else values
    return result


def as_list(value: Any) -> list[dict]:
    """Normalize a parsed SOAP array field into a list.

    ``_element_to_value`` keeps an array field's own wrapper element (e.g.
    ``ReceiveTrackingDataList`` -> ``{"TrackingDataType": [...]}``) rather
    than flattening it, so this unwraps that single inner key before
    normalizing arity.
    """
    if value is None:
        return []
    if isinstance(value, list):
        return [v for v in value if isinstance(v, dict)]
    if isinstance(value, dict):
        if len(value) == 1:
            inner = next(iter(value.values()))
            if isinstance(inner, list):
                return [v for v in inner if isinstance(v, dict)]
            if isinstance(inner, dict):
                return [inner]
            return []
        return [value]
    return []


def _dict_to_xml(parent: ET.Element, data: dict[str, Any]) -> None:
    """Append ``data`` as child elements of ``parent``."""
    for key, value in data.items():
        if isinstance(value, dict):
            child = ET.SubElement(parent, key)
            _dict_to_xml(child, value)
        elif isinstance(value, (list, tuple)):
            for item in value:
                child = ET.SubElement(parent, key)
                if isinstance(item, dict):
                    _dict_to_xml(child, item)
                else:
                    child.text = _xml_text(item)
        elif value is not None:
            child = ET.SubElement(parent, key)
            child.text = _xml_text(value)
        else:
            ET.SubElement(parent, key)


def _xml_text(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _build_envelope(method_name: str, fields: dict[str, Any]) -> bytes:
    """Build the SOAP 1.1 envelope for one Navigator3Service operation."""
    envelope = ET.Element(
        "soap:Envelope",
        {
            "xmlns:xsi": "http://www.w3.org/2001/XMLSchema-instance",
            "xmlns:xsd": "http://www.w3.org/2001/XMLSchema",
            "xmlns:soap": "http://schemas.xmlsoap.org/soap/envelope/",
        },
    )
    body = ET.SubElement(envelope, "soap:Body")
    # The wire wraps each call twice: a bare operation element around an
    # inner ``<...Request>`` element that actually holds the fields.
    outer = ET.SubElement(body, method_name, {"xmlns": DPD_DE_SOAP_NAMESPACE})
    inner = ET.SubElement(
        outer, f"{method_name}Request", {"xmlns": DPD_DE_SOAP_NAMESPACE}
    )
    _dict_to_xml(inner, fields)
    return b'<?xml version="1.0" encoding="utf-8"?>' + ET.tostring(
        envelope, encoding="utf-8"
    )


def _parse_response(method_name: str, body_bytes: bytes) -> dict[str, Any]:
    """Parse a SOAP response body into the operation's response fields."""
    root = ET.fromstring(body_bytes)  # noqa: S314 - carrier's own SOAP response, no external entities expected
    body_elements = [
        child for child in root if _strip_ns(child.tag) == "Body"
    ]
    if not body_elements:
        return {}
    response_root = body_elements[0]
    children = list(response_root)
    if not children:
        return {}
    response_el = children[0]
    parsed = _element_to_value(response_el)
    if not isinstance(parsed, dict):
        return {}
    # The response is wrapped the same way the request is: a
    # ``<...Response>`` element (already unwrapped above) around a
    # ``<...Result>`` element that holds the actual fields.
    result_key = f"{method_name}Result"
    if list(parsed.keys()) == [result_key] and isinstance(parsed[result_key], dict):
        return parsed[result_key]
    return parsed


def _warn_keyphase_rotation_once() -> None:
    """One-shot: two ``ERROR_KEYPHASE`` responses in a row."""
    global _keyphase_rotation_warned
    if _keyphase_rotation_warned:
        return
    _keyphase_rotation_warned = True
    _LOGGER.warning(
        "DPD may have rotated the app secret; this integration's German "
        "support may need to be rebuilt. Two ERROR_KEYPHASE responses in a "
        "row from api.paketnavigator.de mean the request signature no "
        "longer validates, not a routine session expiry. Open an issue: %s",
        _NEW_ISSUE_URL,
    )


def _warn_clock_skew_once(skew_minutes: float) -> None:
    """One-shot: a response ``TimeStamp`` more than the tolerance away from local time."""
    global _clock_skew_warned
    if _clock_skew_warned:
        return
    _clock_skew_warned = True
    _LOGGER.warning(
        "DPD Germany rejected a request due to clock skew (~%.1f minutes) "
        "between this Home Assistant host and DPD's server — the KeyPhase "
        "signature is minute-derived, so an out-of-sync clock breaks every "
        "call. Check this host's clock/NTP sync. Open an issue if it looks "
        "correct: %s",
        skew_minutes,
        _NEW_ISSUE_URL,
    )


_OVERLONG_FRACTION = re.compile(r"(\.\d{6})\d+")


def _check_clock_skew(timestamp: str | None) -> None:
    if not timestamp:
        return
    # .NET's "o" format emits up to 7 fractional-second digits (100ns
    # ticks); fromisoformat only accepts up to 6.
    timestamp = _OVERLONG_FRACTION.sub(r"\1", timestamp)
    try:
        remote = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    except ValueError:
        return
    if remote.tzinfo is None:
        remote = remote.replace(tzinfo=timezone.utc)
    skew = abs((datetime.now(timezone.utc) - remote).total_seconds()) / 60
    if skew > DPD_DE_CLOCK_SKEW_TOLERANCE_MINUTES:
        _warn_clock_skew_once(skew)


def _error_codes(body: dict[str, Any]) -> set[str]:
    errors = as_list(body.get("ErrorDataList"))
    codes: set[str] = set()
    for error in errors:
        code = error.get("ErrorCode")
        if code:
            codes.add(code)
    # getUserLogin's own rejection shape is a singular Ack/ErrorCode pair,
    # not an ErrorDataList entry.
    top_level_code = body.get("ErrorCode")
    if top_level_code:
        codes.add(top_level_code)
    return codes


class DpdDeSession:
    """One config entry's DPD Germany Paketnavigator session.

    Owns the credentials, the device identity, the current ``SessionToken``
    and the ``cloudUserID`` used to sign authenticated calls. Constructed
    once per config entry, held by the coordinator's runtime data.
    """

    def __init__(
        self,
        session: aiohttp.ClientSession,
        username: str,
        password: str,
        hardware_id: str,
    ) -> None:
        """Initialize with the credentials and device identity for this entry."""
        self._session = session
        self._username = username
        self._password = password
        self._hardware_id = hardware_id
        self._session_token: str | None = None
        self._cloud_user_id = _ANONYMOUS_CLOUD_USER_ID
        self._keyphase_streak = 0

    @property
    def session_token(self) -> str | None:
        """Return the current ``SessionToken``, or ``None`` before the first login."""
        return self._session_token

    def _device_data(self) -> dict[str, Any]:
        """Build the ``DeviceData`` block sent with every call; no push token, we don't register for push."""
        return {
            "Version": "1",
            "HardwareID": self._hardware_id,
            "BootSystemID": "Android_Phone",
            "Name": "Home Assistant",
            "AppVersion": "4.1.2",
            "PushToken": "",
            "AllowPushNotifications": False,
        }

    async def async_login(self) -> dict[str, Any]:
        """Two-stage login.

        An anonymous ``getSessionFullState`` issues a bootstrap
        ``SessionToken``; ``getUserLogin`` exchanges it plus credentials for
        the account session and ``cloudUserID``.
        """
        anon_body = await self._async_raw_call(
            "getSessionFullState",
            {"SessionToken": "", "DeviceData": self._device_data()},
            cloud_user_id=_ANONYMOUS_CLOUD_USER_ID,
        )
        anon_token = anon_body.get("SessionToken") or (
            anon_body.get("SessionFullState") or {}
        ).get("SessionToken")
        if not anon_token:
            raise DpdAuthError("DPD Germany did not return an anonymous SessionToken")

        body = await self._async_raw_call(
            "getUserLogin",
            {
                "SessionToken": anon_token,
                "UserName": self._username,
                "UserPassword": self._password,
            },
            cloud_user_id=_ANONYMOUS_CLOUD_USER_ID,
        )
        if _error_codes(body) or body.get("Ack") is False:
            # Nothing to retry — credentials or signature were rejected outright.
            self._note_keyphase_error(body)
            raise DpdAuthError(
                "DPD Germany login failed: "
                f"{', '.join(sorted(_error_codes(body))) or body.get('ErrorMsg') or 'rejected'}"
            )
        self._keyphase_streak = 0
        session_state = body.get("SessionFullState") or body
        token = session_state.get("SessionToken") or body.get("SessionToken")
        if not token:
            raise DpdAuthError("DPD Germany login did not return a SessionToken")
        self._session_token = token
        account_data = session_state.get("AccountData") or {}
        cloud_user_id = (
            body.get("cloudUserID")
            or body.get("CloudUserID")
            or account_data.get("CloudUserID")
            or account_data.get("cloudUserID")
        )
        if cloud_user_id:
            self._cloud_user_id = str(cloud_user_id)
        _check_clock_skew(body.get("TimeStamp"))
        return session_state

    async def async_get_parcels(self) -> dict[str, Any]:
        """``getSessionFullState``: the account inbox, reauth-once on session expiry."""
        if self._session_token is None:
            return await self.async_login()

        body = await self._async_raw_call(
            "getSessionFullState",
            {"SessionToken": self._session_token, "DeviceData": self._device_data()},
            cloud_user_id=self._cloud_user_id,
        )
        error_codes = _error_codes(body)
        if error_codes & _SESSION_ERROR_CODES:
            self._note_keyphase_error(body)
            self._session_token = None
            return await self.async_login()

        self._keyphase_streak = 0
        _check_clock_skew(body.get("TimeStamp"))
        session_state = body.get("SessionFullState") or {}
        token = session_state.get("SessionToken")
        if token:
            self._session_token = token
        return session_state

    async def async_call(self, method_name: str, fields: dict[str, Any]) -> dict[str, Any]:
        """One authenticated call beyond login/inbox; reauth-once, retried once, never looped."""
        if self._session_token is None:
            await self.async_login()
        assert self._session_token is not None

        call_fields = {"SessionToken": self._session_token, **fields}
        body = await self._async_raw_call(
            method_name, call_fields, cloud_user_id=self._cloud_user_id
        )
        error_codes = _error_codes(body)
        if error_codes & _SESSION_ERROR_CODES:
            self._note_keyphase_error(body)
            self._session_token = None
            await self.async_login()
            assert self._session_token is not None
            call_fields = {"SessionToken": self._session_token, **fields}
            body = await self._async_raw_call(
                method_name, call_fields, cloud_user_id=self._cloud_user_id
            )
        else:
            self._keyphase_streak = 0
        _check_clock_skew(body.get("TimeStamp"))
        return body

    def _note_keyphase_error(self, body: dict[str, Any]) -> None:
        """Track consecutive ``ERROR_KEYPHASE`` responses."""
        if "ERROR_KEYPHASE" in _error_codes(body):
            self._keyphase_streak += 1
            if self._keyphase_streak >= 2:
                _warn_keyphase_rotation_once()
        else:
            self._keyphase_streak = 0

    async def _async_raw_call(
        self, method_name: str, fields: dict[str, Any], *, cloud_user_id: str
    ) -> dict[str, Any]:
        """POST one signed SOAP call and return its parsed response body."""
        now = datetime.now(timezone.utc)
        key_phase = compute_key_phase(now, cloud_user_id, method_name)
        request_fields = {
            "Version": 100,
            "Language": DPD_DE_LANGUAGE,
            "PartnerCredentials": {
                "Name": DPD_DE_PARTNER_NAME,
                "Token": DPD_DE_PARTNER_TOKEN,
                "KeyPhase": key_phase,
            },
            **fields,
        }
        envelope = _build_envelope(method_name, request_fields)
        headers = {
            "Content-Type": "text/xml; charset=utf-8",
            "SOAPAction": f"{DPD_DE_SOAP_NAMESPACE}{method_name}",
            "Accept": "text/xml",
            "User-Agent": DPD_DE_USER_AGENT,
        }
        async with self._session.post(
            DPD_DE_SOAP_URL, data=envelope, headers=headers
        ) as response:
            if response.status not in (200, 500):
                # 500 can carry a parseable SOAP fault; anything else is transport-level.
                raise DpdApiError(response.status)
            payload = await response.read()

        try:
            body = _parse_response(method_name, payload)
        except ET.ParseError as err:
            raise DpdApiError(response.status) from err
        if "faultstring" in body:
            # A SOAP fault, not a domain ErrorDataList entry — a server-side
            # exception, not a credentials/session rejection. Surfacing it as
            # DpdAuthError would misreport it as "invalid password".
            raise DpdApiError(response.status)
        return body


__all__ = [
    "DpdDeSession",
    "as_list",
    "compute_key_phase",
]
