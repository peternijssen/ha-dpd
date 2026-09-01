"""Tests for the DPD diagnostics handler."""
from datetime import timedelta
from unittest.mock import MagicMock

import pytest

from custom_components.dpd import DpdData
from custom_components.dpd.diagnostics import (
    TO_REDACT,
    async_get_config_entry_diagnostics,
)

REDACTED = "**REDACTED**"


def _entry_with_runtime_data(
    *,
    incoming_active: list[dict] | None = None,
    incoming_delivered: list[dict] | None = None,
    outgoing_active: list[dict] | None = None,
    outgoing_delivered: list[dict] | None = None,
    last_update_success: bool = True,
    current_tier_minutes: int | None = 45,
    update_interval: timedelta | None = timedelta(minutes=45),
) -> MagicMock:
    coordinator = MagicMock()
    coordinator.data = {
        "incoming_active": incoming_active or [],
        "incoming_delivered": incoming_delivered or [],
        "outgoing_active": outgoing_active or [],
        "outgoing_delivered": outgoing_delivered or [],
    }
    coordinator.last_update_success = last_update_success
    # Explicit, not left as an unconfigured MagicMock attribute, or the
    # "polling" block below would carry a MagicMock instead of a real value.
    coordinator.current_tier_minutes = current_tier_minutes
    coordinator.update_interval = update_interval

    entry = MagicMock()
    entry.data = {"email": "user@example.com", "password": "secret", "bu": "DPD-NL"}
    entry.options = {"delivered_filter_type": "days", "delivered_filter_amount": 7}
    entry.runtime_data = DpdData(client=MagicMock(), coordinator=coordinator)
    return entry


@pytest.mark.asyncio
async def test_diagnostics_redacts_credentials():
    entry = _entry_with_runtime_data()
    result = await async_get_config_entry_diagnostics(MagicMock(), entry)
    assert result["entry_data"]["email"] == REDACTED
    assert result["entry_data"]["password"] == REDACTED


@pytest.mark.asyncio
async def test_diagnostics_passes_through_options_and_bu():
    entry = _entry_with_runtime_data()
    result = await async_get_config_entry_diagnostics(MagicMock(), entry)
    assert result["entry_data"]["bu"] == "DPD-NL"
    assert result["entry_options"]["delivered_filter_type"] == "days"


@pytest.mark.asyncio
async def test_diagnostics_redacts_parcel_pii():
    entry = _entry_with_runtime_data(
        incoming_active=[{
            "parcelNumber": "01668235086385",
            "senderName": "Online Retailer",
            "status": {"description": "IN_TRANSIT"},
            "recipientName": "Peter",
        }],
    )
    result = await async_get_config_entry_diagnostics(MagicMock(), entry)
    parcel = result["incoming_active"][0]
    assert parcel["parcelNumber"] == REDACTED
    assert parcel["senderName"] == REDACTED
    assert parcel["recipientName"] == REDACTED
    # Status is not PII
    assert parcel["status"]["description"] == "IN_TRANSIT"


@pytest.mark.asyncio
async def test_diagnostics_reports_counts_and_update_success():
    entry = _entry_with_runtime_data(
        incoming_active=[{"parcelNumber": "A"}, {"parcelNumber": "B"}],
        incoming_delivered=[{"parcelNumber": "C"}],
        outgoing_active=[{"parcelNumber": "D"}],
        outgoing_delivered=[{"parcelNumber": "E"}],
        last_update_success=False,
    )
    result = await async_get_config_entry_diagnostics(MagicMock(), entry)
    assert result["counts"] == {
        "incoming_active": 2,
        "incoming_delivered": 1,
        "outgoing_active": 1,
        "outgoing_delivered": 1,
    }
    assert result["outgoing_delivered"][0]["parcelNumber"] == "**REDACTED**"
    assert result["last_update_success"] is False


@pytest.mark.asyncio
async def test_diagnostics_surfaces_polling_state():
    entry = _entry_with_runtime_data(
        current_tier_minutes=15, update_interval=timedelta(minutes=15)
    )
    result = await async_get_config_entry_diagnostics(MagicMock(), entry)
    assert result["polling"] == {
        "current_tier_minutes": 15,
        "update_interval_seconds": 15 * 60,
    }


@pytest.mark.asyncio
async def test_diagnostics_polling_handles_suspended_interval():
    entry = _entry_with_runtime_data(current_tier_minutes=None, update_interval=None)
    result = await async_get_config_entry_diagnostics(MagicMock(), entry)
    assert result["polling"] == {
        "current_tier_minutes": None,
        "update_interval_seconds": None,
    }


def test_to_redact_includes_pii_keys():
    for key in ("email", "password", "parcelNumber", "senderName", "postalCode"):
        assert key in TO_REDACT
