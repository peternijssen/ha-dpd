"""DPD's shared myDPD/dpdgroup.com backend — NL plus 14 other business units.

A pure relocation of what used to live in ``api.py`` (the Keycloak /
mobile-guest-token / consignee-SSO auth chain, parcels + detail fetch,
Follow My Parcel, DPD-UK's keyless tracker lookup) and ``parcels.py`` (the
status map, event history, tracking-URL derivation, ``normalize_parcel``) —
behaviour unchanged. ``api.py``/``parcels.py`` now import from here and
re-export under the old names.

A package (not a flat module) to mirror ``countries/de/``'s nested shape.

``CONF_BU``/``BUSINESS_UNITS`` are this backend's own domain — a BU is a
parameter of it, not a suite-level concept, and DE has none. They stay
defined in ``const.py`` to avoid an import cycle with ``config_flow.py``.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

import aiohttp

from ...const import (
    BU_API_OVERRIDES,
    BU_COUNTRY_OVERRIDES,
    BU_TRACKING_URL_OVERRIDES,
    BUSINESS_UNITS,
    DEFAULT_BU,
    DELIVERED_DESCRIPTION,
    DPD_BASIC_TOKEN,
    DPD_CONSIGNEE_SSO_URL,
    DPD_FMP_AUTHENTICATE_URL,
    DPD_FMP_SHIPMENT_URL,
    DPD_GUEST_TOKEN_URL,
    DPD_PARCEL_DETAIL_URL,
    DPD_PARCELS_URL,
    DPD_UK_REFERENCE_URL,
    DPD_UK_TRACKING_URL,
    HISTORY_MAX_EVENTS,
    KEYCLOAK_CLIENT_ID,
    KEYCLOAK_TOKEN_URL,
    KNOWN_DESCRIPTIONS,
    NEW_COUNTRY_ISSUE_URL,
    STATUS_AT_DELIVERY_CENTER,
    STATUS_AVAILABLE_FOR_COLLECTION,
    STATUS_DELIVERED,
    STATUS_IN_TRANSIT,
    STATUS_ORDER_CREATED,
    STATUS_PARCEL_HANDED,
    STATUS_PARCEL_OUT_FOR_DELIVERY,
    STATUS_RETURN_TO_SENDER,
    STATUS_UNSUCCESSFUL_DELIVERY,
    USER_AGENT,
    DpdApiError,
    DpdAuthError,
    ParcelStatus,
)

_LOGGER = logging.getLogger(__name__)

__all__ = [
    "BUSINESS_UNITS",
    "DEFAULT_BU",
    "NEW_COUNTRY_ISSUE_URL",
    "GeneralApiClient",
    "DpdApiError",
    "DpdAuthError",
    "map_parcel_status",
    "normalize_parcel",
    "log_unknown_descriptions",
    "map_event_status",
    "build_history",
    "filter_active_shipments",
    "filter_delivered_shipments",
    "fmp_hashcode",
    "shipment_delivery_dt",
    "shipment_planned_dt",
    "shipment_planned_window",
]


# ---------------------------------------------------------------------------
# Transport — Keycloak → mobile guest token → consignee-SSO, JSON over HTTPS
# ---------------------------------------------------------------------------


class GeneralApiClient:
    """Client for DPD's shared myDPD/dpdgroup.com parcel tracking API."""

    def __init__(
        self,
        email: str,
        password: str,
        session: aiohttp.ClientSession,
        bu: str = DEFAULT_BU,
    ) -> None:
        """Initialize the API client."""
        self._email = email
        self._password = password
        self._session = session
        self._bu = bu
        # The value actually sent to Keycloak/consignee-sso/parcels/detail —
        # remapped through BU_API_OVERRIDES for BUs with no business-unit
        # code of their own (e.g. DPD-UK -> DPD-NL). `self._bu` itself stays
        # the configured value: it drives the tracking URL and unique_id.
        self._request_bu = BU_API_OVERRIDES.get(bu, bu)
        self._token: str | None = None
        self._reauth_lock = asyncio.Lock()

    @property
    def access_token(self) -> str | None:
        """Return the current DPD access token, or ``None`` if not yet logged in."""
        return self._token

    @property
    def bu(self) -> str:
        """Return the configured DPD business unit (e.g. ``DPD-NL``)."""
        return self._bu

    async def async_login(self) -> str:
        """Run the three-step auth flow and return the DPD access token.

        1. Keycloak login with username/password
        2. Fetch a guest token using the hardcoded mobile app client credentials
        3. Exchange the Keycloak token for a DPD user token via consignee-sso
        """
        kc_token = await self._async_keycloak_login()
        guest_token = await self._async_guest_token()
        dpd_token = await self._async_consignee_sso(kc_token, guest_token)
        self._token = dpd_token
        return dpd_token

    async def _async_keycloak_login(self) -> str:
        data = {
            "client_id": KEYCLOAK_CLIENT_ID,
            "grant_type": "password",
            "scope": "openid",
            "username": self._email,
            "password": self._password,
        }
        async with self._session.post(
            KEYCLOAK_TOKEN_URL,
            data=data,
            headers={"User-Agent": USER_AGENT},
        ) as response:
            if response.status >= 500:
                raise DpdApiError(response.status)
            body: dict[str, Any] = await response.json(content_type=None)

        token = body.get("access_token")
        if not token:
            error = body.get("error_description") or body.get("error", "unknown")
            raise DpdAuthError(f"Keycloak login failed: {error}")
        return token

    async def _async_guest_token(self) -> str:
        async with self._session.post(
            DPD_GUEST_TOKEN_URL,
            params={"grant_type": "client_credentials"},
            headers={
                "Authorization": f"Basic {DPD_BASIC_TOKEN}",
                "Content-Type": "application/json",
                "User-Agent": USER_AGENT,
            },
        ) as response:
            if response.status >= 500:
                raise DpdApiError(response.status)
            body: dict[str, Any] = await response.json(content_type=None)

        token = body.get("access_token")
        if not token:
            raise DpdAuthError("DPD guest token request did not return a token")
        return token

    async def async_get_parcels(self) -> dict[str, Any]:
        """Retrieve the parcels payload, re-authenticating once on session expiry."""
        async def _fetch() -> dict[str, Any]:
            async with self._session.post(
                DPD_PARCELS_URL,
                params={"bu": self._request_bu, "lang": "en"},
                json={
                    "incomingParcels": [],
                    "sendingParcels": [],
                    "confirmedParcels": None,
                    "shipmentCollections": [],
                    "confirmedShipmentCollections": None,
                },
                headers={
                    "Authorization": f"Bearer {self._token}",
                    "Content-Type": "application/json",
                    "User-Agent": USER_AGENT,
                },
            ) as response:
                if response.status != 200:
                    raise DpdApiError(response.status)
                data: dict[str, Any] = await response.json(content_type=None)
            return data

        return await self._async_call_with_reauth(_fetch)

    async def async_get_parcel_detail(
        self,
        parcel_number: str,
        *,
        shipment_bu_code: str | None = None,
        parcel_type: str = "INCOMING",
    ) -> dict[str, Any] | None:
        """Fetch /v10/parcels/details/{n}. Best-effort: returns None on any failure.

        The list endpoint is a thin summary; this detail endpoint carries the
        recipient block, weight, dimensions and current truck position. We use
        it only to populate the canonical ``receiver`` field; failures are
        swallowed so a broken detail call never breaks the main parcels poll.
        """
        params: dict[str, str] = {
            "parcelType": parcel_type,
            "businessUnit": self._request_bu,
            "lang": "en",
            "continueWithoutVerification": "false",
        }
        if shipment_bu_code:
            params["shipmentBUCode"] = shipment_bu_code
        try:
            async with self._session.post(
                f"{DPD_PARCEL_DETAIL_URL}/{parcel_number}",
                params=params,
                headers={
                    "Authorization": f"Bearer {self._token}",
                    "Content-Type": "application/json",
                    "User-Agent": USER_AGENT,
                },
            ) as response:
                if response.status != 200:
                    return None
                return await response.json(content_type=None)
        except aiohttp.ClientError:
            return None

    async def async_fmp_delivery_window(self, hashcode: str) -> dict[str, Any] | None:
        """Fetch the Follow My Parcel delivery window for a parcel hashcode.

        Returns the raw ``deliveryDateAndTime`` dict
        (``{"deliveryDate": ..., "timeRange": {"from": ..., "to": ...}}``)
        when DPD has scheduled a precise window — typically on the day a
        parcel is out for delivery. Returns ``None`` when the window is
        not (yet) available or the call fails; FMP is best-effort and
        should never break the main parcels poll.
        """
        try:
            fmp_token = await self._async_fmp_authenticate(hashcode)
        except DpdApiError as err:
            _LOGGER.debug(
                "FMP authenticate failed for hashcode %s: HTTP %d",
                hashcode[:8], err.status_code,
            )
            return None
        except DpdAuthError as err:
            _LOGGER.debug("FMP authenticate returned no token: %s", err)
            return None

        try:
            shipment = await self._async_fmp_shipment(fmp_token)
        except DpdApiError as err:
            _LOGGER.debug(
                "FMP shipment fetch failed for hashcode %s: HTTP %d",
                hashcode[:8], err.status_code,
            )
            return None

        delivery = shipment.get("deliveryDateAndTime")
        return delivery if isinstance(delivery, dict) else None

    async def _async_fmp_authenticate(self, hashcode: str) -> str:
        """Exchange a parcel hashcode for an FMP access token."""
        async with self._session.post(
            DPD_FMP_AUTHENTICATE_URL,
            json={"authMethod": "HASHCODE", "credentials": hashcode},
            headers={
                "Authorization": f"Bearer {self._token}",
                "Content-Type": "application/json",
                "User-Agent": USER_AGENT,
            },
        ) as response:
            if response.status != 200:
                raise DpdApiError(response.status)
            body: dict[str, Any] = await response.json(content_type=None)

        token = body.get("access_token")
        if not token:
            raise DpdAuthError("DPD FMP authenticate did not return a token")
        return token

    async def _async_fmp_shipment(self, fmp_token: str) -> dict[str, Any]:
        """Fetch the FMP shipment detail for the current FMP token."""
        async with self._session.get(
            DPD_FMP_SHIPMENT_URL,
            params={"lang": "en"},
            headers={
                "Authorization": f"Bearer {fmp_token}",
                "Content-Type": "application/json",
                "User-Agent": USER_AGENT,
            },
        ) as response:
            if response.status != 200:
                raise DpdApiError(response.status)
            data: dict[str, Any] = await response.json(content_type=None)
        return data

    async def async_get_uk_tracking_code(self, parcel_number: str) -> str | None:
        """Resolve the ``track.dpd.co.uk`` parcelCode for a UK parcel number.

        Best-effort, returns ``None`` on any failure. ``apis.track.dpd.co.uk``
        is a wholly separate, keyless host from the myDPD backend the rest of
        this client talks to — no Bearer token, no session. The ``postcode``
        param is sent empty deliberately: a live check (2026-08-17) confirmed
        the endpoint returns the same result regardless of postcode value, so
        there is nothing gained by threading the receiver's postcode through
        here. The returned ``parcelCode`` (``<14-digit-number>*<sequence>``)
        is DPD-assigned and stable — safe to cache forever per barcode.
        """
        try:
            async with self._session.get(
                DPD_UK_REFERENCE_URL,
                params={
                    "origin": "PRTK",
                    "postcode": "",
                    "referenceNumber": parcel_number,
                },
                headers={
                    "Accept": "application/json",
                    "User-Agent": USER_AGENT,
                },
            ) as response:
                if response.status != 200:
                    return None
                body: dict[str, Any] = await response.json(content_type=None)
        except aiohttp.ClientError:
            return None

        matches = body.get("data") or []
        if not matches:
            return None
        return matches[0].get("parcelCode")

    async def _async_call_with_reauth(self, coro_fn: Any) -> Any:
        """Call coro_fn(), re-authenticating once if the session has expired."""
        try:
            return await coro_fn()
        except DpdApiError as err:
            if err.status_code not in (401, 403):
                raise
        async with self._reauth_lock:
            await self.async_login()
        return await coro_fn()

    async def _async_consignee_sso(self, kc_token: str, guest_token: str) -> str:
        async with self._session.post(
            DPD_CONSIGNEE_SSO_URL,
            params={"bu": self._request_bu},
            data=kc_token,
            headers={
                "Authorization": f"Bearer {guest_token}",
                "Content-Type": "text/plain",
                "User-Agent": USER_AGENT,
            },
        ) as response:
            if response.status >= 500:
                raise DpdApiError(response.status)
            body: dict[str, Any] = await response.json(content_type=None)

        token = body.get("access_token")
        if not token:
            raise DpdAuthError("DPD consignee-sso did not return a token")
        return token


# ---------------------------------------------------------------------------
# Payload mapping — status.description -> canonical ParcelStatus, history,
# tracking URL, normalize_parcel
# ---------------------------------------------------------------------------


def _augment_dimensions(dims: dict | None) -> dict | None:
    """Return a copy of ``dims`` with a ``text`` field added (``L x W x H cm``).

    Suite-wide format: integer values, lowercase ``x`` separator, length
    first per the L × W × H shipping convention. ``text`` is ``None`` when
    any of the three required fields is missing so callers can still rely
    on the key being present.
    """
    if not dims:
        return None
    length = dims.get("length")
    width = dims.get("width")
    height = dims.get("height")
    if length is None or width is None or height is None:
        text: str | None = None
    else:
        text = f"{int(round(length))} x {int(round(width))} x {int(round(height))} cm"
    return {**dims, "text": text}

# DPD status.description → canonical ParcelStatus. Every value in
# KNOWN_DESCRIPTIONS is mapped here; anything else falls back to
# ParcelStatus.UNKNOWN and triggers a one-shot info log via
# log_unknown_descriptions.
_DESCRIPTION_MAP: dict[str, ParcelStatus] = {
    STATUS_ORDER_CREATED: ParcelStatus.REGISTERED,
    STATUS_PARCEL_HANDED: ParcelStatus.IN_TRANSIT,
    STATUS_IN_TRANSIT: ParcelStatus.IN_TRANSIT,
    STATUS_AT_DELIVERY_CENTER: ParcelStatus.IN_TRANSIT,
    STATUS_PARCEL_OUT_FOR_DELIVERY: ParcelStatus.OUT_FOR_DELIVERY,
    STATUS_AVAILABLE_FOR_COLLECTION: ParcelStatus.AT_PICKUP_POINT,
    STATUS_UNSUCCESSFUL_DELIVERY: ParcelStatus.IN_TRANSIT,
    STATUS_RETURN_TO_SENDER: ParcelStatus.RETURNING,
    STATUS_DELIVERED: ParcelStatus.DELIVERED,
}

# DPD parcelEvents ``eventType`` → canonical ParcelStatus (history timeline).
# History maps from the stable ``eventType`` code, not the ``eventTypeText``
# (which is ``lang``-dependent and brittle). Unmapped codes resolve to
# ``None`` (+ a one-shot warning, see feature B).
#
# Codes are DPD "Geo Event codes" from their GSMT matrix. We deliberately
# map only the subset a consumer parcel realistically emits
# (the ``CC*`` customs, ``PK*``/``CR*`` sender-side, ``MT*``/``QR*``/``MI*``
# contact codes are left unmapped on purpose — feature B will surface any
# that actually appear).
_EVENT_TYPE_MAP: dict[str, ParcelStatus] = {
    # --- Data / registration ---
    "ENA": ParcelStatus.REGISTERED,        # Data received and integrated
    # --- In the network (origin, hub, sorting, destination depot) ---
    "ORI": ParcelStatus.IN_TRANSIT,        # Origin depot – Inbound
    "ORW": ParcelStatus.IN_TRANSIT,        # Origin depot – Held
    "HUI": ParcelStatus.IN_TRANSIT,        # Hub – Inbound
    "HUS": ParcelStatus.IN_TRANSIT,        # Hub – Sorted
    "HUW": ParcelStatus.IN_TRANSIT,        # Hub – Held
    "HUZ": ParcelStatus.IN_TRANSIT,        # Hub – Scan
    "SPE": ParcelStatus.IN_TRANSIT,        # Status parcel – Information (benign)
    "SPL": ParcelStatus.IN_TRANSIT,        # Status parcel – Loaded
    "SPS": ParcelStatus.IN_TRANSIT,        # Status parcel – Sorted
    "SPV": ParcelStatus.IN_TRANSIT,        # Status parcel – Control
    "SPW": ParcelStatus.IN_TRANSIT,        # Status parcel – Held (benign)
    "SPZ": ParcelStatus.IN_TRANSIT,        # Status parcel – Scan
    "DLI": ParcelStatus.IN_TRANSIT,        # Destination depot – Inbound
    "DLS": ParcelStatus.IN_TRANSIT,        # Destination depot – Sorted
    "DLW": ParcelStatus.IN_TRANSIT,        # Destination depot – Held
    "DLZ": ParcelStatus.IN_TRANSIT,        # Destination depot – Scan
    "DLR": ParcelStatus.IN_TRANSIT,        # Destination depot – Driver's return (failed attempt, back to depot)
    "MSDLO": ParcelStatus.IN_TRANSIT,      # Message Notification — a "delivery coming" heads-up, not the physical out-for-delivery scan (that's DLO)
    # --- Out for delivery ---
    "DLO": ParcelStatus.OUT_FOR_DELIVERY,  # Destination depot – Out for delivery
    # --- Parcelshop / PUDO flow (not yet confirmed in consumer parcelEvents) ---
    "DEHD": ParcelStatus.IN_TRANSIT,       # Handover by the driver to the PUDO (arriving)
    "DEHDY": ParcelStatus.IN_TRANSIT,      # Proof of handover by the driver to the PUDO
    "DOMSDLO": ParcelStatus.IN_TRANSIT,    # PUDO – Notification sent: available in PUDO
    "DOPKY": ParcelStatus.IN_TRANSIT,      # PUDO – Drop Off (sender drop-off)
    "DODEI": ParcelStatus.AT_PICKUP_POINT, # PUDO – Received and available for consignee collection
    # --- Delivered / collected ---
    "DEY": ParcelStatus.DELIVERED,         # Delivery – Delivered
    "DEYY": ParcelStatus.DELIVERED,        # Delivery – Proof of delivery
    "DODEY": ParcelStatus.DELIVERED,       # PUDO – Collected by the consignee
    "DODEYY": ParcelStatus.DELIVERED,      # PUDO – Proof of delivery, signature in PUDO
    # --- Returning to sender ---
    "SPR": ParcelStatus.RETURNING,         # Status parcel – Return
    "DEN": ParcelStatus.RETURNING,         # Delivery – Refusal
    "DODEN": ParcelStatus.RETURNING,       # PUDO – Not collected by the consignee
    "DODEH": ParcelStatus.RETURNING,       # PUDO – Handed back from PUDO to the driver
    # --- Exceptions / anomalies (problem) ---
    "ENX": ParcelStatus.PROBLEM,           # Data exchange – Exception
    "ORX": ParcelStatus.PROBLEM,           # Origin depot – Exception
    "HUX": ParcelStatus.PROBLEM,           # Hub – Exception
    "SPX": ParcelStatus.PROBLEM,           # Status parcel – Exception
    "DLX": ParcelStatus.PROBLEM,           # Destination depot – Exception
    "DEX": ParcelStatus.PROBLEM,           # Delivery – Delivery Exception
    "DODEX": ParcelStatus.PROBLEM,         # PUDO – Collection anomaly
}

# New-issue link surfaced in the unknown-status warnings so users can paste a
# ready-made line into a bug report.
# Points at the pre-filled issue template rather than a blank form, so a
# user following this link from their log lands somewhere that already
# asks the right questions.
_NEW_ISSUE_URL = (
    "https://github.com/ha-parcel-integrations/ha-dpd/issues/new"
    "?template=unrecognised_status.yml"
)

# Values we have already warned about once in this HA session, so repeated
# polls do not flood the log with the same "new status" message.
_unknown_descriptions_logged: set[str] = set()
_unknown_event_types_logged: set[str] = set()


def _description(shipment: dict) -> str | None:
    return (shipment.get("status") or {}).get("description")


def map_parcel_status(parcel: dict) -> ParcelStatus:
    """Map a raw DPD parcel to a canonical :class:`ParcelStatus`.

    Reads ``status.description`` and looks it up in ``_DESCRIPTION_MAP``;
    unknown values (or a missing status field) fall back to
    ``ParcelStatus.UNKNOWN``. New raw values are surfaced separately via
    :func:`log_unknown_descriptions` so the map can be extended.
    """
    description = _description(parcel)
    return _DESCRIPTION_MAP.get(description or "", ParcelStatus.UNKNOWN)


def log_unknown_descriptions(shipments: list[dict]) -> None:
    """Warn about any status.description we have not mapped yet, once per value.

    Lets us extend ``_DESCRIPTION_MAP`` as new lifecycle stages surface
    in real accounts without spamming the log on every poll. Anything
    not in the map is reported as ``ParcelStatus.UNKNOWN`` until it is.
    The message carries a copy-paste issue link (feature B).
    """
    for shipment in shipments:
        description = _description(shipment)
        if (
            description
            and description not in KNOWN_DESCRIPTIONS
            and description not in _unknown_descriptions_logged
        ):
            _unknown_descriptions_logged.add(description)
            code = (shipment.get("status") or {}).get("status")
            _LOGGER.warning(
                "Unrecognised DPD status — help us map it. Open an issue and "
                "paste this line: %s\n  [parcel] status.description=%s (code %s) "
                "→ reported as 'unknown'",
                _NEW_ISSUE_URL,
                description,
                code,
            )


def map_event_status(
    event_type: str | None, event_type_text: str | None = None
) -> ParcelStatus | None:
    """Map a DPD parcelEvents ``eventType`` to a canonical status.

    Returns ``None`` for an unmapped (or absent) code — history entries keep
    ``status: null`` rather than guessing — and surfaces a one-shot warning
    with copy-paste issue text so users can help extend the map.
    """
    if not event_type:
        return None
    status = _EVENT_TYPE_MAP.get(event_type)
    if status is not None:
        return status

    if event_type not in _unknown_event_types_logged:
        _unknown_event_types_logged.add(event_type)
        _LOGGER.warning(
            "Unrecognised DPD status — help us map it. Open an issue and "
            "paste this line: %s\n  [history] eventType=%s text=%r "
            "→ reported as 'unknown'",
            _NEW_ISSUE_URL,
            event_type,
            event_type_text,
        )
    return None


def _parse_iso(value: str | None) -> datetime | None:
    """Parse an ISO 8601 string to a datetime, or ``None`` on failure.

    DPD event timestamps are naive (no offset); a naive value is treated as
    UTC so a list always sorts without crashing on a mixed set.
    """
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def build_history(
    events: list[dict] | None, *, max_events: int = HISTORY_MAX_EVENTS
) -> list[dict]:
    """Build the canonical ``history`` list from DPD ``parcelEvents``.

    Each entry is ``{timestamp, status, raw_status}`` — identical across all
    suite carriers. ``timestamp`` combines the event ``date`` + ``time``;
    ``status`` maps from the stable ``eventType``; ``raw_status`` is DPD's
    own ``eventTypeText``. Sorted oldest → newest (unparseable timestamps keep
    their original order, after the parseable ones) and capped to the most
    recent ``max_events``.
    """
    parseable: list[tuple[datetime, dict]] = []
    unparseable: list[dict] = []
    for event in events or []:
        date = event.get("date")
        time = event.get("time")
        if not date or not time:
            continue
        timestamp = f"{date}T{time}"
        text = event.get("eventTypeText")
        entry = {
            "timestamp": timestamp,
            "status": map_event_status(event.get("eventType"), text),
            "raw_status": text,
        }
        dt = _parse_iso(timestamp)
        if dt is None:
            unparseable.append(entry)
        else:
            parseable.append((dt, entry))
    parseable.sort(key=lambda item: item[0])
    ordered = [entry for _, entry in parseable] + unparseable
    return ordered[-max_events:]


def filter_active_shipments(shipments: list[dict]) -> list[dict]:
    """Return shipments that have not yet reached the DELIVERED state."""
    return [s for s in shipments if _description(s) != DELIVERED_DESCRIPTION]


def filter_delivered_shipments(shipments: list[dict]) -> list[dict]:
    """Return shipments in the DELIVERED state."""
    return [s for s in shipments if _description(s) == DELIVERED_DESCRIPTION]


def _tracking_url(parcel: dict, bu: str = DEFAULT_BU) -> str | None:
    """Build the DPD tracking URL for a parcel, or ``None`` when no parcelNumber.

    The country segment is derived from the business unit (``DPD-DE`` ->
    ``de``) rather than hardcoded, so it follows whichever BU the account
    was set up with. ``CHR-PT`` doesn't follow the ``DPD-<CC>`` shape (see
    ``BU_COUNTRY_OVERRIDES``); ``BRT`` (Italy) isn't on dpdgroup.com at all
    (see ``BU_TRACKING_URL_OVERRIDES``).
    """
    parcel_number = parcel.get("parcelNumber")
    if not parcel_number:
        return None
    if override := BU_TRACKING_URL_OVERRIDES.get(bu):
        return override.format(parcel_number=parcel_number)
    country = BU_COUNTRY_OVERRIDES.get(bu, bu.removeprefix("DPD-").lower())
    return (
        f"https://www.dpdgroup.com/{country}/mydpd/my-parcels/search?"
        f"parcelNumber={parcel_number}"
    )


def normalize_parcel(
    parcel: dict,
    *,
    receiver: str | None = None,
    weight: float | None = None,
    dimensions: dict | None = None,
    history: list[dict] | None = None,
    bu: str = DEFAULT_BU,
    uk_tracking_code: str | None = None,
) -> dict:
    """Return a carrier-agnostic parcel dict with the DPD payload under ``raw``.

    Mirrors the shape every other carrier integration (DHL, PostNL)
    publishes, so the parcel aggregator and cross-carrier dashboards
    can read parcels the same way regardless of source. The original
    DPD shipment object stays available under ``raw``.

    ``planned_from`` / ``planned_to`` are derived from
    :func:`shipment_planned_window` (FMP window first, then the top-level
    ``deliveryTime{From,To}`` pair, finally the all-day fallback), and
    cleared for delivered parcels where ``delivered_at`` carries the
    actual moment instead. The raw DPD payload is passed through under
    ``raw`` without modification.

    ``receiver``, ``weight`` and ``dimensions`` come from the per-parcel
    detail endpoint — the list endpoint doesn't carry them, so the
    coordinator fetches them lazily and passes them in. DPD's native
    units (kg + cm) already match the canonical contract, so no
    conversion is needed here.

    For a ParcelShop delivery, DPD's detail endpoint returns the ParcelShop's
    own name in ``receiver.name`` instead of a person, with no separate field
    for the actual recipient — so that value becomes ``pickup_point`` instead,
    and ``receiver`` is left ``None``.

    ``history`` is the optional per-parcel status timeline (opt-in option,
    default off → ``None``). It is also detail-endpoint sourced and stays
    top-level so it survives the aggregator's ``strip_raw()``.

    ``bu`` is the account's business unit (e.g. ``DPD-DE``), used only to
    build the country segment of the tracking URL.

    ``uk_tracking_code`` is DPD-UK's own ``track.dpd.co.uk`` parcelCode,
    resolved and cached by the coordinator via a separate keyless lookup
    (:meth:`GeneralApiClient.async_get_uk_tracking_code`) — when present it
    wins over :func:`_tracking_url`'s NL-page fallback. ``None`` for every
    other BU, and for a UK parcel the lookup hasn't resolved (yet, or ever).
    """
    description = _description(parcel)
    delivered = description == DELIVERED_DESCRIPTION
    delivered_at: str | None = None
    if delivered:
        dt = shipment_delivery_dt(parcel)
        delivered_at = dt.isoformat() if dt else None
    planned_from: str | None = None
    planned_to: str | None = None
    if not delivered:
        start, end = shipment_planned_window(parcel)
        planned_from = start.isoformat() if start else None
        planned_to = end.isoformat() if end else None
    is_pickup = (parcel.get("status") or {}).get("deliveryType") == "PARCELSHOP"
    return {
        "carrier": "DPD",
        "barcode": parcel.get("parcelNumber"),
        "sender": parcel.get("senderName"),
        "receiver": None if is_pickup else receiver,
        "status": map_parcel_status(parcel),
        "raw_status": description,
        "delivered": delivered,
        "delivered_at": delivered_at,
        "planned_from": planned_from,
        "planned_to": planned_to,
        "pickup": is_pickup,
        "pickup_point": receiver if is_pickup else None,
        "url": (
            f"{DPD_UK_TRACKING_URL}/{uk_tracking_code}"
            if uk_tracking_code
            else _tracking_url(parcel, bu)
        ),
        "weight": weight,
        "dimensions": dimensions,
        "history": history,
        "raw": parcel,
    }


def shipment_planned_window(shipment: dict) -> tuple[datetime | None, datetime | None]:
    """Return the planned ``(from, to)`` delivery window for a shipment.

    Resolution order:

    1. The nested Follow My Parcel block (``fmpDeliveryDateAndTime``),
       which gives a precise hour range like ``(10:34, 11:34)`` on the
       day of delivery.
    2. The top-level ``deliveryTimeFrom`` / ``deliveryTimeTo`` pair
       (combined with ``deliveryDate``), which DPD attaches once a
       parcel is out for delivery.
    3. A whole-day window in the parcel's local timezone, when only
       ``deliveryDate`` is known.

    Returns ``(None, None)`` when even the date is missing or
    unparseable.
    """
    tz_id = (shipment.get("status") or {}).get("eventDateAndTimeZoneId")
    tz: timezone | ZoneInfo = timezone.utc
    if tz_id:
        try:
            tz = ZoneInfo(tz_id)
        except Exception:  # noqa: BLE001 - bad tz string from API
            tz = timezone.utc

    fmp = shipment.get("fmpDeliveryDateAndTime") or {}
    fmp_date = fmp.get("deliveryDate")
    time_range = fmp.get("timeRange") or {}
    fmp_from = time_range.get("from")
    fmp_to = time_range.get("to")
    if fmp_date and fmp_from and fmp_to:
        try:
            return (
                datetime.fromisoformat(f"{fmp_date}T{fmp_from}").replace(tzinfo=tz),
                datetime.fromisoformat(f"{fmp_date}T{fmp_to}").replace(tzinfo=tz),
            )
        except ValueError:
            pass

    date_str = shipment.get("deliveryDate")
    if not date_str:
        return (None, None)

    top_from = shipment.get("deliveryTimeFrom")
    top_to = shipment.get("deliveryTimeTo")
    if top_from and top_to:
        try:
            return (
                datetime.fromisoformat(f"{date_str}T{top_from}").replace(tzinfo=tz),
                datetime.fromisoformat(f"{date_str}T{top_to}").replace(tzinfo=tz),
            )
        except ValueError:
            pass

    try:
        d = datetime.fromisoformat(date_str)
    except ValueError:
        return (None, None)
    start = d.replace(tzinfo=tz)
    end = d.replace(hour=23, minute=59, second=59, tzinfo=tz)
    return (start, end)


def shipment_planned_dt(shipment: dict) -> datetime | None:
    """Return the start of the planned delivery window, or ``None``.

    Thin wrapper around :func:`shipment_planned_window` that keeps the
    "start time" semantics callers (e.g. the next-delivery sensor) rely
    on for sorting.
    """
    return shipment_planned_window(shipment)[0]


def fmp_hashcode(shipment: dict) -> str | None:
    """Pluck the Follow My Parcel hashcode off a shipment, when present.

    DPD lists ``availableActions.FOLLOW_MY_PARCEL`` as an array with at
    most one entry; the hashcode is what the FMP authenticate endpoint
    expects as credentials. Returns ``None`` for shipments that have not
    yet been scheduled into the FMP system (typically anything before
    the day of delivery).
    """
    actions = shipment.get("availableActions") or {}
    fmp_actions = actions.get("FOLLOW_MY_PARCEL") or []
    if not fmp_actions:
        return None
    hashcode = fmp_actions[0].get("hashcode")
    return hashcode if isinstance(hashcode, str) and hashcode else None


def shipment_delivery_dt(shipment: dict) -> datetime | None:
    """Return the delivery datetime of a shipment, or ``None`` if unknown.

    Prefers ``status.eventDateAndTime`` (naive ISO 8601) combined with
    ``status.eventDateAndTimeZoneId`` (IANA timezone). Falls back to the
    plain ``deliveryDate`` (date) at start-of-day UTC.
    """
    status = shipment.get("status") or {}
    moment = status.get("eventDateAndTime")
    if moment:
        try:
            dt = datetime.fromisoformat(moment.replace("Z", "+00:00"))
        except ValueError:
            dt = None
        if dt is not None:
            if dt.tzinfo is None:
                tz_id = status.get("eventDateAndTimeZoneId")
                tz: timezone | ZoneInfo = timezone.utc
                if tz_id:
                    try:
                        tz = ZoneInfo(tz_id)
                    except Exception:  # noqa: BLE001 - bad tz string from API
                        tz = timezone.utc
                dt = dt.replace(tzinfo=tz)
            return dt

    date_str = shipment.get("deliveryDate")
    if not date_str:
        return None
    try:
        d = datetime.fromisoformat(date_str)
    except ValueError:
        return None
    return d.replace(tzinfo=timezone.utc)
