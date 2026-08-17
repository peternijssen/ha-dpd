"""Tests for the DPD integration setup/unload entry points."""
from unittest.mock import AsyncMock, patch

import aiohttp
import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD

from custom_components.dpd import DpdData
from custom_components.dpd.api import DpdApiError, DpdAuthError
from custom_components.dpd.const import (
    CONF_BU,
    CONF_COUNTRY,
    CONF_DE_HARDWARE_ID,
    CONF_DELIVERED_FILTER_AMOUNT,
    CONF_DELIVERED_FILTER_TYPE,
    CONF_INCLUDE_HISTORY,
    CONF_REFRESH_INTERVAL,
    COUNTRY_DE,
    DEFAULT_BU,
    DEFAULT_REFRESH_INTERVAL,
    DOMAIN,
)

from .payloads import envelope

_ENTRY_DATA = {
    CONF_EMAIL: "user@example.com",
    CONF_PASSWORD: "secret",
    CONF_BU: DEFAULT_BU,
}


def _add_entry(hass):
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=f"{DEFAULT_BU}:{_ENTRY_DATA[CONF_EMAIL]}",
        data=_ENTRY_DATA,
        options={
            CONF_DELIVERED_FILTER_TYPE: "days",
            CONF_DELIVERED_FILTER_AMOUNT: 7,
        },
    )
    entry.add_to_hass(hass)
    return entry


@pytest.mark.asyncio
async def test_setup_entry_succeeds_and_stores_runtime_data(hass):
    entry = _add_entry(hass)
    with (
        patch(
            "custom_components.dpd.DpdApiClient.async_login",
            new=AsyncMock(return_value=None),
        ),
        patch(
            "custom_components.dpd.DpdApiClient.async_get_parcels",
            new=AsyncMock(return_value=envelope()),
        ),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.LOADED
    assert isinstance(entry.runtime_data, DpdData)


@pytest.mark.asyncio
async def test_setup_entry_auth_failure_triggers_reauth(hass):
    entry = _add_entry(hass)
    with patch(
        "custom_components.dpd.DpdApiClient.async_login",
        new=AsyncMock(side_effect=DpdAuthError("nope")),
    ):
        assert not await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.SETUP_ERROR


@pytest.mark.asyncio
async def test_setup_entry_retries_on_connection_error(hass):
    entry = _add_entry(hass)
    with patch(
        "custom_components.dpd.DpdApiClient.async_login",
        new=AsyncMock(side_effect=aiohttp.ClientError("boom")),
    ):
        assert not await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.SETUP_RETRY


@pytest.mark.asyncio
async def test_setup_entry_retries_when_first_refresh_fails(hass):
    """Login succeeds but the first data fetch fails.

    The first refresh runs in __init__.py before platforms are forwarded, so a
    failure raises ConfigEntryNotReady from the entry setup (SETUP_RETRY) rather
    than — too late — from a forwarded platform.
    """
    entry = _add_entry(hass)
    with (
        patch(
            "custom_components.dpd.DpdApiClient.async_login",
            new=AsyncMock(return_value=None),
        ),
        patch(
            "custom_components.dpd.DpdApiClient.async_get_parcels",
            new=AsyncMock(side_effect=DpdApiError(429)),
        ),
    ):
        assert not await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.SETUP_RETRY


@pytest.mark.asyncio
async def test_setup_entry_retries_when_dpd_auth_service_is_down(hass):
    """A 5xx during login (DPD's auth tier outage) must surface as SETUP_RETRY
    so HA backs off and tries again, not as a reauth-prompting auth failure.
    """
    entry = _add_entry(hass)
    with patch(
        "custom_components.dpd.DpdApiClient.async_login",
        new=AsyncMock(side_effect=DpdApiError(503)),
    ):
        assert not await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.SETUP_RETRY


@pytest.mark.asyncio
async def test_unload_entry_succeeds(hass):
    entry = _add_entry(hass)
    with (
        patch(
            "custom_components.dpd.DpdApiClient.async_login",
            new=AsyncMock(return_value=None),
        ),
        patch(
            "custom_components.dpd.DpdApiClient.async_get_parcels",
            new=AsyncMock(return_value=envelope()),
        ),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        assert await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.NOT_LOADED


# ---------------------------------------------------------------------------
# DPD Germany — separate SOAP session, dispatched by CONF_COUNTRY
# ---------------------------------------------------------------------------

_DE_EMAIL = "user@example.de"
_DE_ENTRY_DATA = {
    CONF_EMAIL: _DE_EMAIL,
    CONF_PASSWORD: "secret",
    CONF_COUNTRY: COUNTRY_DE.upper(),
    CONF_DE_HARDWARE_ID: "existing-hw-id",
}


def _add_de_entry(hass):
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=f"DE:{_DE_EMAIL}",
        data=_DE_ENTRY_DATA,
        options={
            CONF_DELIVERED_FILTER_TYPE: "days",
            CONF_DELIVERED_FILTER_AMOUNT: 7,
        },
    )
    entry.add_to_hass(hass)
    return entry


@pytest.mark.asyncio
async def test_setup_entry_de_succeeds_and_stores_de_session(hass):
    entry = _add_de_entry(hass)
    with (
        patch(
            "custom_components.dpd.DpdDeSession.async_login",
            new=AsyncMock(return_value=None),
        ),
        patch(
            "custom_components.dpd.coordinator.async_get_all_parcels_de",
            new=AsyncMock(return_value=([], [])),
        ),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.LOADED
    assert isinstance(entry.runtime_data, DpdData)
    assert entry.runtime_data.client is None
    assert entry.runtime_data.de_session is not None


@pytest.mark.asyncio
async def test_setup_entry_de_reuses_the_stored_hardware_id(hass):
    """The device identity must survive a restart — a fresh uuid every setup
    would look like a different device to DPD Germany on every reload."""
    entry = _add_de_entry(hass)
    with (
        patch(
            "custom_components.dpd.DpdDeSession.async_login",
            new=AsyncMock(return_value=None),
        ),
        patch(
            "custom_components.dpd.coordinator.async_get_all_parcels_de",
            new=AsyncMock(return_value=([], [])),
        ),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    assert entry.runtime_data.de_session._hardware_id == "existing-hw-id"


@pytest.mark.asyncio
async def test_setup_entry_de_auth_failure_triggers_reauth(hass):
    entry = _add_de_entry(hass)
    with patch(
        "custom_components.dpd.DpdDeSession.async_login",
        new=AsyncMock(side_effect=DpdAuthError("nope")),
    ):
        assert not await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.SETUP_ERROR


@pytest.mark.asyncio
async def test_setup_entry_de_retries_when_first_refresh_fails(hass):
    entry = _add_de_entry(hass)
    with (
        patch(
            "custom_components.dpd.DpdDeSession.async_login",
            new=AsyncMock(return_value=None),
        ),
        patch(
            "custom_components.dpd.coordinator.async_get_all_parcels_de",
            new=AsyncMock(side_effect=DpdApiError(500)),
        ),
    ):
        assert not await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.SETUP_RETRY


@pytest.mark.asyncio
async def test_unload_entry_de_succeeds(hass):
    entry = _add_de_entry(hass)
    with (
        patch(
            "custom_components.dpd.DpdDeSession.async_login",
            new=AsyncMock(return_value=None),
        ),
        patch(
            "custom_components.dpd.coordinator.async_get_all_parcels_de",
            new=AsyncMock(return_value=([], [])),
        ),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        assert await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.NOT_LOADED


@pytest.mark.asyncio
async def test_options_flow_schedules_reload(hass):
    """Submitting the options form schedules a reload of the config entry."""
    entry = _add_entry(hass)
    with (
        patch(
            "custom_components.dpd.DpdApiClient.async_login",
            new=AsyncMock(return_value=None),
        ),
        patch(
            "custom_components.dpd.DpdApiClient.async_get_parcels",
            new=AsyncMock(return_value=envelope()),
        ) as mock_get,
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

        baseline = mock_get.await_count

        result = await hass.config_entries.options.async_init(entry.entry_id)
        await hass.config_entries.options.async_configure(
            result["flow_id"],
            user_input={
                "delivered": {
                    CONF_DELIVERED_FILTER_TYPE: "parcels",
                    CONF_DELIVERED_FILTER_AMOUNT: 14,
                },
                "history": {
                    CONF_INCLUDE_HISTORY: False,
                },
                "polling": {
                    CONF_REFRESH_INTERVAL: str(DEFAULT_REFRESH_INTERVAL),
                },
            },
        )
        await hass.async_block_till_done()

    assert mock_get.await_count > baseline
