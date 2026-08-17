"""Tests for DPD Germany's SOAP session transport: KeyPhase signing, the
envelope/response codec, and ``DpdDeSession``'s login/reauth lifecycle.
"""
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.dpd.const import DpdApiError, DpdAuthError
from custom_components.dpd.countries.de.session import (
    DpdDeSession,
    _build_envelope,
    _check_clock_skew,
    _dict_to_xml,
    _element_to_value,
    _error_codes,
    _leaf_text_value,
    _parse_response,
    _strip_ns,
    as_list,
    compute_key_phase,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _mock_response(status: int, body: bytes) -> MagicMock:
    response = MagicMock()
    response.status = status
    response.read = AsyncMock(return_value=body)
    return response


def _mock_session(*responses: MagicMock) -> MagicMock:
    """Return a session whose ``.post`` yields ``responses`` in order."""
    queue = list(responses)

    @asynccontextmanager
    async def _ctx(*_args, **_kwargs):
        yield queue.pop(0)

    session = MagicMock()
    session.post = MagicMock(side_effect=_ctx)
    return session


def _envelope_body(inner_xml: str) -> bytes:
    return (
        b'<?xml version="1.0" encoding="utf-8"?>'
        b'<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/">'
        b"<soap:Body>" + inner_xml.encode("utf-8") + b"</soap:Body></soap:Envelope>"
    )


def _session_full_state_response(
    *, session_token: str = "TOKEN-1", cloud_user_id: int = 0, ack: str = "true"
) -> bytes:
    return _envelope_body(
        '<getSessionFullStateResponse xmlns="https://cloud.dpd.com/">'
        "<getSessionFullStateResult>"
        f"<Ack>{ack}</Ack>"
        "<TimeStamp>2026-08-17T16:51:43.340213+02:00</TimeStamp>"
        f"<SessionFullState><SessionToken>{session_token}</SessionToken>"
        f"<AccountData><cloudUserID>{cloud_user_id}</cloudUserID></AccountData>"
        "</SessionFullState>"
        "</getSessionFullStateResult></getSessionFullStateResponse>"
    )


def _user_login_response(
    *, session_token: str = "TOKEN-2", cloud_user_id: int = 10593904, ack: str = "true"
) -> bytes:
    return _envelope_body(
        '<getUserLoginResponse xmlns="https://cloud.dpd.com/">'
        "<getUserLoginResult>"
        f"<Ack>{ack}</Ack>"
        "<TimeStamp>2026-08-17T16:52:25.629169+02:00</TimeStamp>"
        f"<SessionFullState><SessionToken>{session_token}</SessionToken>"
        f"<AccountData><cloudUserID>{cloud_user_id}</cloudUserID></AccountData>"
        "</SessionFullState>"
        "</getUserLoginResult></getUserLoginResponse>"
    )


def _login_rejected_response(error_code: str = "ERROR_LOGIN_FAILED") -> bytes:
    return _rejected_response("getUserLogin", error_code)


def _rejected_response(method_name: str, error_code: str) -> bytes:
    """A same-shape-as-real rejection for whichever operation is being called."""
    return _envelope_body(
        f'<{method_name}Response xmlns="https://cloud.dpd.com/">'
        f"<{method_name}Result>"
        "<Ack>false</Ack>"
        f"<ErrorCode>{error_code}</ErrorCode>"
        "<ErrorMsg>rejected</ErrorMsg>"
        f"</{method_name}Result></{method_name}Response>"
    )


def _fault_response() -> bytes:
    return _envelope_body(
        "<soap:Fault>"
        "<faultcode>soap:Server</faultcode>"
        "<faultstring>Der Objektverweis wurde nicht auf eine Objektinstanz "
        "festgelegt.</faultstring>"
        "</soap:Fault>"
    )


def _session() -> DpdDeSession:
    return DpdDeSession(_mock_session(), "user@example.de", "secret", "hw-1")


# ---------------------------------------------------------------------------
# compute_key_phase
# ---------------------------------------------------------------------------


def test_key_phase_is_time_derived_and_deterministic():
    now = datetime(2026, 8, 17, 14, 51, tzinfo=timezone.utc)
    phase = compute_key_phase(now, "0", "getSessionFullState")
    p = (14 * 60 + 51 + 1000) * 3
    assert phase.startswith(str(p))
    assert phase == compute_key_phase(now, "0", "getSessionFullState")


def test_key_phase_varies_with_method_name():
    now = datetime(2026, 8, 17, 14, 51, tzinfo=timezone.utc)
    assert compute_key_phase(now, "0", "getUserLogin") != compute_key_phase(
        now, "0", "getSessionFullState"
    )


# ---------------------------------------------------------------------------
# XML codec — _strip_ns / _leaf_text_value / _element_to_value / as_list
# ---------------------------------------------------------------------------


def test_strip_ns_removes_namespace_prefix():
    assert _strip_ns("{https://cloud.dpd.com/}Ack") == "Ack"
    assert _strip_ns("Ack") == "Ack"


def test_leaf_text_value_blank_or_missing_is_none():
    assert _leaf_text_value(None) is None
    assert _leaf_text_value("") is None
    assert _leaf_text_value("   ") is None


def test_leaf_text_value_coerces_xsd_booleans():
    assert _leaf_text_value("true") is True
    assert _leaf_text_value("false") is False


def test_leaf_text_value_passes_through_other_strings():
    assert _leaf_text_value("hello") == "hello"


def test_element_to_value_folds_repeated_siblings_into_a_list():
    import xml.etree.ElementTree as ET

    root = ET.fromstring(
        "<Errors><ErrorDataList><Item>A</Item><Item>B</Item></ErrorDataList></Errors>"
    )
    assert _element_to_value(root) == {"ErrorDataList": {"Item": ["A", "B"]}}


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, []),
        ([{"a": 1}, "not-a-dict"], [{"a": 1}]),
        ({"Item": [{"a": 1}, {"a": 2}]}, [{"a": 1}, {"a": 2}]),
        ({"Item": {"a": 1}}, [{"a": 1}]),
        ({"Item": "leaf"}, []),
        ({"a": 1, "b": 2}, [{"a": 1, "b": 2}]),
        ("garbage", []),
    ],
)
def test_as_list_normalizes_parsed_array_shapes(value, expected):
    assert as_list(value) == expected


# ---------------------------------------------------------------------------
# _build_envelope / _parse_response
# ---------------------------------------------------------------------------


def test_build_envelope_double_wraps_the_operation():
    xml_bytes = _build_envelope("getUserLogin", {"UserName": "a@b.de"})
    text = xml_bytes.decode("utf-8")
    assert "<getUserLogin " in text
    assert "<getUserLoginRequest " in text
    assert "<UserName>a@b.de</UserName>" in text
    # The inner Request element must nest inside the outer bare element.
    assert text.index("<getUserLogin ") < text.index("<getUserLoginRequest ")


def test_dict_to_xml_serializes_a_list_of_leaf_and_dict_items():
    import xml.etree.ElementTree as ET

    parent = ET.Element("Root")
    _dict_to_xml(parent, {"Item": ["leaf", {"Nested": "x"}]})
    items = parent.findall("Item")
    assert len(items) == 2
    assert items[0].text == "leaf"
    assert items[1].find("Nested").text == "x"


def test_build_envelope_serializes_nested_dicts_and_booleans():
    xml_bytes = _build_envelope(
        "getSessionFullState",
        {"DeviceData": {"AllowPushNotifications": False}, "SessionToken": None},
    )
    text = xml_bytes.decode("utf-8")
    assert "<AllowPushNotifications>false</AllowPushNotifications>" in text
    assert "<SessionToken />" in text or "<SessionToken/>" in text


def test_parse_response_unwraps_result_key():
    body = _parse_response("getUserLogin", _user_login_response())
    assert body["Ack"] is True
    assert body["SessionFullState"]["SessionToken"] == "TOKEN-2"


def test_parse_response_returns_empty_dict_for_no_body_children():
    body = _parse_response(
        "getUserLogin",
        b'<?xml version="1.0"?><soap:Envelope '
        b'xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/">'
        b"<soap:Body></soap:Body></soap:Envelope>",
    )
    assert body == {}


def test_parse_response_returns_empty_dict_when_no_body_element():
    body = _parse_response(
        "getUserLogin",
        b'<?xml version="1.0"?><soap:Envelope '
        b'xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/"></soap:Envelope>',
    )
    assert body == {}


def test_parse_response_passes_through_fault_without_result_key():
    body = _parse_response("getUserLogin", _fault_response())
    assert "faultstring" in body


def test_parse_response_returns_empty_dict_when_response_element_is_a_leaf():
    """The response's first Body child has no children of its own (just
    text) — ``_element_to_value`` then returns a scalar, not a dict."""
    body = _parse_response(
        "getUserLogin",
        _envelope_body('<getUserLoginResponse xmlns="https://cloud.dpd.com/">plain text</getUserLoginResponse>'),
    )
    assert body == {}


# ---------------------------------------------------------------------------
# _error_codes
# ---------------------------------------------------------------------------


def test_error_codes_collects_error_data_list_entries():
    body = {"ErrorDataList": {"ErrorData": [{"ErrorCode": "ERROR_A"}, {"ErrorCode": "ERROR_B"}]}}
    assert _error_codes(body) == {"ERROR_A", "ERROR_B"}


def test_error_codes_includes_top_level_error_code():
    body = {"ErrorCode": "ERROR_SESSION_NOT_VALID"}
    assert _error_codes(body) == {"ERROR_SESSION_NOT_VALID"}


def test_error_codes_empty_when_no_errors():
    assert _error_codes({"Ack": True}) == set()


# ---------------------------------------------------------------------------
# _check_clock_skew
# ---------------------------------------------------------------------------


def test_check_clock_skew_truncates_seven_digit_fraction():
    """.NET's 'o' format can emit 7 fractional digits; fromisoformat only
    accepts 6 — this must not raise."""
    _check_clock_skew("2026-08-17T16:48:41.4668799+02:00")


def test_check_clock_skew_ignores_unparseable_timestamp():
    _check_clock_skew("not-a-timestamp")


def test_check_clock_skew_ignores_none():
    _check_clock_skew(None)


def test_warn_keyphase_rotation_once_is_a_true_one_shot(caplog):
    import custom_components.dpd.countries.de.session as session_mod

    session_mod._keyphase_rotation_warned = False
    with caplog.at_level("WARNING"):
        session_mod._warn_keyphase_rotation_once()
        session_mod._warn_keyphase_rotation_once()
    assert sum("rotated the app secret" in r.message for r in caplog.records) == 1
    session_mod._keyphase_rotation_warned = False


def test_check_clock_skew_treats_naive_timestamp_as_utc(monkeypatch):
    """No explicit UTC offset on the wire — must not raise, and must compare
    against UTC rather than crashing on a naive/aware mismatch."""
    import custom_components.dpd.countries.de.session as session_mod

    session_mod._clock_skew_warned = False
    warned = MagicMock()
    monkeypatch.setattr(session_mod, "_warn_clock_skew_once", warned)
    naive_now = datetime.now(timezone.utc).replace(tzinfo=None).isoformat()
    _check_clock_skew(naive_now)
    warned.assert_not_called()


def test_check_clock_skew_warns_once_when_beyond_tolerance(monkeypatch):
    from datetime import timedelta

    import custom_components.dpd.countries.de.session as session_mod

    session_mod._clock_skew_warned = False
    warned = MagicMock()
    monkeypatch.setattr(session_mod, "_warn_clock_skew_once", warned)
    # 20 minutes away from now, well beyond the 8-minute tolerance.
    skewed = (datetime.now(timezone.utc) - timedelta(minutes=20)).isoformat()
    _check_clock_skew(skewed)
    warned.assert_called_once()
    session_mod._clock_skew_warned = False


# ---------------------------------------------------------------------------
# DpdDeSession.async_login
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_async_login_two_stage_success():
    session = DpdDeSession(
        _mock_session(
            _mock_response(200, _session_full_state_response(session_token="ANON")),
            _mock_response(
                200,
                _user_login_response(session_token="REAL", cloud_user_id=10593904),
            ),
        ),
        "user@example.de",
        "secret",
        "hw-1",
    )
    state = await session.async_login()
    assert session.session_token == "REAL"
    assert session._cloud_user_id == "10593904"
    assert state["SessionToken"] == "REAL"


@pytest.mark.asyncio
async def test_async_login_raises_when_anonymous_token_missing():
    session = DpdDeSession(
        _mock_session(_mock_response(200, _session_full_state_response(session_token=""))),
        "user@example.de",
        "secret",
        "hw-1",
    )
    with pytest.raises(DpdAuthError, match="anonymous SessionToken"):
        await session.async_login()


@pytest.mark.asyncio
async def test_async_login_raises_on_rejected_credentials():
    session = DpdDeSession(
        _mock_session(
            _mock_response(200, _session_full_state_response()),
            _mock_response(200, _login_rejected_response("ERROR_LOGIN_FAILED")),
        ),
        "user@example.de",
        "wrong",
        "hw-1",
    )
    with pytest.raises(DpdAuthError, match="ERROR_LOGIN_FAILED"):
        await session.async_login()


@pytest.mark.asyncio
async def test_async_login_raises_on_ack_false_without_error_code():
    body = _envelope_body(
        '<getUserLoginResponse xmlns="https://cloud.dpd.com/">'
        "<getUserLoginResult><Ack>false</Ack></getUserLoginResult>"
        "</getUserLoginResponse>"
    )
    session = DpdDeSession(
        _mock_session(
            _mock_response(200, _session_full_state_response()),
            _mock_response(200, body),
        ),
        "user@example.de",
        "wrong",
        "hw-1",
    )
    with pytest.raises(DpdAuthError, match="rejected"):
        await session.async_login()


@pytest.mark.asyncio
async def test_async_login_raises_when_no_session_token_returned():
    body = _envelope_body(
        '<getUserLoginResponse xmlns="https://cloud.dpd.com/">'
        "<getUserLoginResult><Ack>true</Ack></getUserLoginResult>"
        "</getUserLoginResponse>"
    )
    session = DpdDeSession(
        _mock_session(
            _mock_response(200, _session_full_state_response()),
            _mock_response(200, body),
        ),
        "user@example.de",
        "secret",
        "hw-1",
    )
    with pytest.raises(DpdAuthError, match="did not return a SessionToken"):
        await session.async_login()


@pytest.mark.asyncio
async def test_async_login_two_consecutive_keyphase_errors_warn_once():
    import custom_components.dpd.countries.de.session as session_mod

    session_mod._keyphase_rotation_warned = False
    session = DpdDeSession(
        _mock_session(
            _mock_response(200, _session_full_state_response()),
            _mock_response(200, _login_rejected_response("ERROR_KEYPHASE")),
        ),
        "user@example.de",
        "secret",
        "hw-1",
    )
    with pytest.raises(DpdAuthError):
        await session.async_login()
    assert session._keyphase_streak == 1
    session_mod._keyphase_rotation_warned = False


@pytest.mark.asyncio
async def test_async_login_second_consecutive_keyphase_error_logs_the_rotation_warning(caplog):
    """A *second* ERROR_KEYPHASE in a row (streak already at 1) must trip
    the actual one-shot warning, not just bump the counter."""
    import custom_components.dpd.countries.de.session as session_mod

    session_mod._keyphase_rotation_warned = False
    session = DpdDeSession(
        _mock_session(
            _mock_response(200, _session_full_state_response()),
            _mock_response(200, _login_rejected_response("ERROR_KEYPHASE")),
        ),
        "user@example.de",
        "secret",
        "hw-1",
    )
    session._keyphase_streak = 1
    with caplog.at_level("WARNING"), pytest.raises(DpdAuthError):
        await session.async_login()
    assert session._keyphase_streak == 2
    assert any("rotated the app secret" in r.message for r in caplog.records)
    session_mod._keyphase_rotation_warned = False


# ---------------------------------------------------------------------------
# DpdDeSession.async_get_parcels
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_async_get_parcels_logs_in_when_no_token_yet():
    session = DpdDeSession(
        _mock_session(
            _mock_response(200, _session_full_state_response(session_token="ANON")),
            _mock_response(200, _user_login_response(session_token="REAL")),
        ),
        "user@example.de",
        "secret",
        "hw-1",
    )
    state = await session.async_get_parcels()
    assert session.session_token == "REAL"
    assert state["SessionToken"] == "REAL"


@pytest.mark.asyncio
async def test_async_get_parcels_reuses_existing_token():
    session = _session()
    session._session_token = "EXISTING"
    session._cloud_user_id = "10593904"
    session._session = _mock_session(
        _mock_response(
            200,
            _session_full_state_response(session_token="RENEWED", cloud_user_id=10593904),
        )
    )
    state = await session.async_get_parcels()
    assert session.session_token == "RENEWED"
    assert state["SessionToken"] == "RENEWED"


@pytest.mark.asyncio
async def test_async_get_parcels_relogins_on_session_error():
    session = _session()
    session._session_token = "STALE"
    session._session = _mock_session(
        _mock_response(
            200, _rejected_response("getSessionFullState", "ERROR_SESSION_NOT_VALID")
        ),
        _mock_response(200, _session_full_state_response(session_token="ANON")),
        _mock_response(200, _user_login_response(session_token="FRESH")),
    )
    state = await session.async_get_parcels()
    assert session.session_token == "FRESH"
    assert state["SessionToken"] == "FRESH"


# ---------------------------------------------------------------------------
# DpdDeSession.async_call
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_async_call_logs_in_first_when_no_token():
    session = _session()
    session._session = _mock_session(
        _mock_response(200, _session_full_state_response(session_token="ANON")),
        _mock_response(200, _user_login_response(session_token="REAL")),
        _mock_response(
            200,
            _envelope_body(
                '<getTrackingScanListResponse xmlns="https://cloud.dpd.com/">'
                "<getTrackingScanListResult><Ack>true</Ack>"
                "</getTrackingScanListResult></getTrackingScanListResponse>"
            ),
        ),
    )
    body = await session.async_call("getTrackingScanList", {"ParcelNo": "X"})
    assert body["Ack"] is True
    assert session.session_token == "REAL"


@pytest.mark.asyncio
async def test_async_call_reauths_once_and_retries():
    session = _session()
    session._session_token = "STALE"
    ok_response = _envelope_body(
        '<getTrackingScanListResponse xmlns="https://cloud.dpd.com/">'
        "<getTrackingScanListResult><Ack>true</Ack>"
        "</getTrackingScanListResult></getTrackingScanListResponse>"
    )
    session._session = _mock_session(
        _mock_response(
            200, _rejected_response("getTrackingScanList", "ERROR_SESSION_NOT_VALID")
        ),
        _mock_response(200, _session_full_state_response(session_token="ANON")),
        _mock_response(200, _user_login_response(session_token="FRESH")),
        _mock_response(200, ok_response),
    )
    body = await session.async_call("getTrackingScanList", {"ParcelNo": "X"})
    assert body["Ack"] is True
    assert session.session_token == "FRESH"


@pytest.mark.asyncio
async def test_async_call_never_retries_twice():
    """A session error on the *retry* call itself must not loop again."""
    session = _session()
    session._session_token = "STALE"
    session._session = _mock_session(
        _mock_response(
            200, _rejected_response("getTrackingScanList", "ERROR_SESSION_NOT_VALID")
        ),
        _mock_response(200, _session_full_state_response(session_token="ANON")),
        _mock_response(200, _user_login_response(session_token="FRESH")),
        _mock_response(
            200, _rejected_response("getTrackingScanList", "ERROR_SESSION_NOT_VALID")
        ),
    )
    body = await session.async_call("getTrackingScanList", {"ParcelNo": "X"})
    # The second rejection is returned as-is, not chased into a third login.
    assert "ERROR_SESSION_NOT_VALID" in _error_codes(body)


# ---------------------------------------------------------------------------
# _async_raw_call — transport-level errors
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_raw_call_raises_on_non_200_500_status():
    session = _session()
    session._session = _mock_session(_mock_response(403, b""))
    with pytest.raises(DpdApiError) as exc_info:
        await session._async_raw_call("getSessionFullState", {}, cloud_user_id="0")
    assert exc_info.value.status_code == 403


@pytest.mark.asyncio
async def test_raw_call_raises_on_soap_fault():
    session = _session()
    session._session = _mock_session(_mock_response(500, _fault_response()))
    with pytest.raises(DpdApiError):
        await session._async_raw_call("getSessionFullState", {}, cloud_user_id="0")


@pytest.mark.asyncio
async def test_raw_call_raises_on_unparseable_xml():
    session = _session()
    session._session = _mock_session(_mock_response(200, b"not xml at all <<<"))
    with pytest.raises(DpdApiError):
        await session._async_raw_call("getSessionFullState", {}, cloud_user_id="0")
