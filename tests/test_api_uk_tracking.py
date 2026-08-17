"""Tests for DpdApiClient.async_get_uk_tracking_code.

apis.track.dpd.co.uk/v1/reference is a wholly separate, keyless host from
the myDPD backend the rest of api.py talks to — no token, no session. See
const.py's DPD_UK_REFERENCE_URL comment for how this was confirmed live.
"""
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock

import aiohttp
import pytest

from custom_components.dpd.api import DpdApiClient


def _mock_response(status: int, payload: dict | None) -> MagicMock:
    response = MagicMock()
    response.status = status
    response.json = AsyncMock(return_value=payload)
    return response


def _mock_session(*responses: MagicMock) -> MagicMock:
    queue = list(responses)

    @asynccontextmanager
    async def _ctx(*_args, **_kwargs):
        yield queue.pop(0)

    session = MagicMock()
    session.get = MagicMock(side_effect=_ctx)
    return session


def _client(session: MagicMock) -> DpdApiClient:
    return DpdApiClient(
        email="user@example.com",
        password="secret",
        session=session,
        bu="DPD-UK",
    )


@pytest.mark.asyncio
async def test_uk_tracking_code_returns_parcel_code_on_match():
    session = _mock_session(_mock_response(200, {
        "data": [{
            "parcelStatus": "Your parcel is waiting for you at home",
            "parcelNumber": "0123 4567 890 123 4",
            "parcelCode": "01234567890123*99999",
            "consignmentNumber": "0123456789",
        }],
    }))
    client = _client(session)

    result = await client.async_get_uk_tracking_code("01234567890123")

    assert result == "01234567890123*99999"
    _, kwargs = session.get.call_args
    assert kwargs["params"]["referenceNumber"] == "01234567890123"
    assert kwargs["params"]["postcode"] == ""
    assert kwargs["params"]["origin"] == "PRTK"
    # Keyless: no Bearer/session on this call.
    assert "Authorization" not in kwargs["headers"]


@pytest.mark.asyncio
async def test_uk_tracking_code_returns_none_on_404():
    session = _mock_session(_mock_response(404, {
        "error": {"statusCode": 404, "message": "Your reference number could not be found"},
    }))
    client = _client(session)

    assert await client.async_get_uk_tracking_code("00000000000000") is None


@pytest.mark.asyncio
async def test_uk_tracking_code_returns_none_on_empty_match_list():
    session = _mock_session(_mock_response(200, {"data": []}))
    client = _client(session)

    assert await client.async_get_uk_tracking_code("01234567890123") is None


@pytest.mark.asyncio
async def test_uk_tracking_code_returns_none_on_client_error():
    @asynccontextmanager
    async def _raise(*_a, **_k):
        raise aiohttp.ClientError("boom")
        yield  # pragma: no cover - never reached

    session = MagicMock()
    session.get = MagicMock(side_effect=_raise)
    client = _client(session)

    assert await client.async_get_uk_tracking_code("01234567890123") is None
