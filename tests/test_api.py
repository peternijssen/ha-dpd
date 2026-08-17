"""Tests for the main DpdApiClient auth + parcels paths.

FMP-specific tests live in test_api_fmp.py; this file covers the
three-step login flow (Keycloak → mobile-app guest token → consignee
SSO), the parcels fetch, and the one-shot reauth-on-401/403 retry.
"""
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock

import aiohttp
import pytest

from custom_components.dpd.api import DpdApiClient, DpdApiError, DpdAuthError

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _mock_response(status: int, payload: dict | None) -> MagicMock:
    response = MagicMock()
    response.status = status
    response.json = AsyncMock(return_value=payload)
    return response


def _mock_session(*responses: MagicMock) -> MagicMock:
    """Return a session whose `.post` / `.get` yield ``responses`` in order."""
    queue = list(responses)

    @asynccontextmanager
    async def _ctx(*_args, **_kwargs):
        yield queue.pop(0)

    session = MagicMock()
    session.post = MagicMock(side_effect=_ctx)
    session.get = MagicMock(side_effect=_ctx)
    return session


def _client(session: MagicMock) -> DpdApiClient:
    return DpdApiClient(
        email="user@example.com",
        password="secret",
        session=session,
    )


# ---------------------------------------------------------------------------
# access_token property
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_access_token_is_none_before_login():
    client = _client(_mock_session())
    assert client.access_token is None


def test_bu_property_reflects_configured_business_unit():
    client = DpdApiClient(
        email="user@example.com",
        password="secret",
        session=_mock_session(),
        bu="DPD-CH",
    )
    assert client.bu == "DPD-CH"


# ---------------------------------------------------------------------------
# Keycloak step
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_keycloak_login_returns_access_token():
    session = _mock_session(
        _mock_response(200, {"access_token": "kc-token"}),
        _mock_response(200, {"access_token": "guest-token"}),
        _mock_response(200, {"access_token": "dpd-token"}),
    )
    client = _client(session)
    assert await client.async_login() == "dpd-token"
    assert client.access_token == "dpd-token"


@pytest.mark.asyncio
async def test_keycloak_login_raises_when_response_has_error_description():
    session = _mock_session(
        _mock_response(401, {"error": "invalid_grant",
                             "error_description": "Wrong password"})
    )
    client = _client(session)
    with pytest.raises(DpdAuthError, match="Wrong password"):
        await client.async_login()


@pytest.mark.asyncio
async def test_keycloak_login_raises_with_error_when_no_description():
    session = _mock_session(_mock_response(401, {"error": "invalid_grant"}))
    client = _client(session)
    with pytest.raises(DpdAuthError, match="invalid_grant"):
        await client.async_login()


@pytest.mark.asyncio
async def test_keycloak_login_raises_with_unknown_when_no_error_info():
    session = _mock_session(_mock_response(401, {}))
    client = _client(session)
    with pytest.raises(DpdAuthError, match="unknown"):
        await client.async_login()


@pytest.mark.asyncio
async def test_keycloak_login_raises_api_error_on_5xx_without_parsing_body():
    """5xx during login = DPD auth service is down. Surface as DpdApiError
    so __init__.py maps it to ConfigEntryNotReady (HA retries) instead of
    DpdAuthError → ConfigEntryAuthFailed (would push the user into reauth).
    The response body is HTML during DPD outages, so we must not try to
    parse it as JSON (that produced the orjson "unexpected character" error).
    """
    bad = MagicMock()
    bad.status = 503
    bad.json = AsyncMock(side_effect=AssertionError("must not parse body on 5xx"))
    session = _mock_session(bad)
    client = _client(session)
    with pytest.raises(DpdApiError) as exc_info:
        await client.async_login()
    assert exc_info.value.status_code == 503


# ---------------------------------------------------------------------------
# Guest-token step
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_guest_token_step_raises_when_token_missing():
    session = _mock_session(
        _mock_response(200, {"access_token": "kc-token"}),
        _mock_response(200, {}),  # no access_token in guest response
    )
    client = _client(session)
    with pytest.raises(DpdAuthError, match="guest token"):
        await client.async_login()


# ---------------------------------------------------------------------------
# Consignee-SSO step
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_consignee_sso_raises_when_token_missing():
    session = _mock_session(
        _mock_response(200, {"access_token": "kc-token"}),
        _mock_response(200, {"access_token": "guest-token"}),
        _mock_response(200, {}),  # consignee-sso returns no token
    )
    client = _client(session)
    with pytest.raises(DpdAuthError, match="consignee-sso"):
        await client.async_login()


# ---------------------------------------------------------------------------
# async_get_parcels happy path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_parcels_returns_payload_on_200():
    payload = {"incomingShipments": [{"parcelNumber": "A"}], "sendingShipments": []}
    session = _mock_session(_mock_response(200, payload))
    client = _client(session)
    client._token = "main-token"  # skip login

    assert await client.async_get_parcels() == payload


@pytest.mark.asyncio
async def test_get_parcels_raises_api_error_on_non_200():
    session = _mock_session(_mock_response(500, None))
    client = _client(session)
    client._token = "main-token"

    with pytest.raises(DpdApiError) as exc_info:
        await client.async_get_parcels()
    assert exc_info.value.status_code == 500


# ---------------------------------------------------------------------------
# _async_call_with_reauth
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_parcels_reauths_once_on_401():
    payload = {"incomingShipments": [], "sendingShipments": []}
    session = _mock_session(
        _mock_response(401, None),                              # first parcels call
        _mock_response(200, {"access_token": "kc-token"}),      # reauth: keycloak
        _mock_response(200, {"access_token": "guest-token"}),   # reauth: guest
        _mock_response(200, {"access_token": "new-dpd-token"}), # reauth: consignee
        _mock_response(200, payload),                           # parcels retry
    )
    client = _client(session)
    client._token = "expired-token"

    assert await client.async_get_parcels() == payload
    assert client.access_token == "new-dpd-token"


@pytest.mark.asyncio
async def test_get_parcels_reauths_once_on_403():
    payload = {"incomingShipments": [], "sendingShipments": []}
    session = _mock_session(
        _mock_response(403, None),
        _mock_response(200, {"access_token": "kc"}),
        _mock_response(200, {"access_token": "guest"}),
        _mock_response(200, {"access_token": "fresh"}),
        _mock_response(200, payload),
    )
    client = _client(session)
    client._token = "expired"

    assert await client.async_get_parcels() == payload


@pytest.mark.asyncio
async def test_get_parcels_does_not_reauth_on_500():
    """5xx is a server-side problem, not a session-expiry signal."""
    session = _mock_session(_mock_response(500, None))
    client = _client(session)
    client._token = "main-token"

    with pytest.raises(DpdApiError):
        await client.async_get_parcels()


# ---------------------------------------------------------------------------
# Per-parcel detail endpoint
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_parcel_detail_returns_body_on_200():
    payload = {"parcelNumber": "01XXX", "receiver": {"name": "Jane Doe"}}
    session = _mock_session(_mock_response(200, payload))
    client = _client(session)
    client._token = "main-token"
    result = await client.async_get_parcel_detail("01XXX", shipment_bu_code="021")
    assert result == payload


@pytest.mark.asyncio
async def test_get_parcel_detail_sends_the_configured_bu_unmodified():
    """The businessUnit param must equal the account's own bu value

    (e.g. ``DPD-DE``), not a re-prefixed/re-cased variant — a mismatch here
    silently breaks receiver/weight/dimensions enrichment for that account
    since detail-call failures are swallowed and never surface to the user.
    """
    session = _mock_session(_mock_response(200, {}))
    client = DpdApiClient(
        email="user@example.com",
        password="secret",
        session=session,
        bu="DPD-DE",
    )
    client._token = "main-token"
    await client.async_get_parcel_detail("01XXX")
    _, kwargs = session.post.call_args
    assert kwargs["params"]["businessUnit"] == "DPD-DE"


@pytest.mark.asyncio
async def test_get_parcel_detail_remaps_uk_bu_to_nl_for_the_request():
    """DPD-UK has no business-unit code of its own on the shared backend

    (see BU_API_OVERRIDES / discussions/14) — the wire-level ``businessUnit``
    must be the remapped ``DPD-NL``, while ``client.bu`` keeps ``DPD-UK`` for
    tracking-url derivation and the config entry's unique_id.
    """
    session = _mock_session(_mock_response(200, {}))
    client = DpdApiClient(
        email="user@example.com",
        password="secret",
        session=session,
        bu="DPD-UK",
    )
    client._token = "main-token"
    await client.async_get_parcel_detail("01XXX")
    _, kwargs = session.post.call_args
    assert kwargs["params"]["businessUnit"] == "DPD-NL"
    assert client.bu == "DPD-UK"


@pytest.mark.asyncio
async def test_get_parcel_detail_returns_none_on_non_200_so_main_poll_is_unaffected():
    """Detail is best-effort enrichment — any non-200 must surface as None
    rather than an exception that would crash the main parcels refresh."""
    session = _mock_session(_mock_response(404, None))
    client = _client(session)
    client._token = "main-token"
    assert await client.async_get_parcel_detail("01XXX") is None


@pytest.mark.asyncio
async def test_get_parcel_detail_returns_none_on_network_error():
    @asynccontextmanager
    async def _raise(*_a, **_k):
        raise aiohttp.ClientError("boom")
        yield  # pragma: no cover - never reached

    session = MagicMock()
    session.post = MagicMock(side_effect=_raise)
    client = _client(session)
    client._token = "main-token"
    assert await client.async_get_parcel_detail("01XXX") is None
