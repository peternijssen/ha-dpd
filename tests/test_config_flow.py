"""Tests for the DPD config flow."""
from unittest.mock import AsyncMock, patch

import aiohttp
import pytest
from homeassistant.config_entries import SOURCE_USER
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.data_entry_flow import FlowResultType

from custom_components.dpd.api import DpdAuthError
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

_USER_INPUT = {
    CONF_EMAIL: "user@example.com",
    CONF_PASSWORD: "secret",
    CONF_BU: DEFAULT_BU,
}
# The BU selector only accepts lower-case option values (hassfest
# translation-key rule); the stored/internal value stays upper-case.
_USER_FORM_INPUT = {**_USER_INPUT, CONF_BU: DEFAULT_BU.lower()}
_DELIVERED_INPUT = {
    CONF_DELIVERED_FILTER_TYPE: "days",
    CONF_DELIVERED_FILTER_AMOUNT: 14,
}


@pytest.mark.asyncio
async def test_user_flow_creates_entry(hass):
    """Happy path: credentials accepted, then delivered-filter form, then entry."""
    with patch(
        "custom_components.dpd.config_flow.DpdApiClient.async_login",
        new=AsyncMock(return_value=None),
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": SOURCE_USER}
        )
        assert result["type"] is FlowResultType.FORM
        assert result["step_id"] == "user"

        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], user_input=_USER_FORM_INPUT
        )
        assert result["type"] is FlowResultType.FORM
        assert result["step_id"] == "delivered"

        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], user_input=_DELIVERED_INPUT
        )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == _USER_INPUT[CONF_EMAIL]
    assert result["data"][CONF_EMAIL] == _USER_INPUT[CONF_EMAIL]
    assert result["data"][CONF_BU] == DEFAULT_BU
    assert result["options"][CONF_DELIVERED_FILTER_AMOUNT] == 14


@pytest.mark.asyncio
async def test_user_flow_invalid_auth(hass):
    with patch(
        "custom_components.dpd.config_flow.DpdApiClient.async_login",
        new=AsyncMock(side_effect=DpdAuthError("nope")),
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": SOURCE_USER}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], user_input=_USER_FORM_INPUT
        )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "invalid_auth"}


@pytest.mark.asyncio
async def test_user_flow_cannot_connect(hass):
    with patch(
        "custom_components.dpd.config_flow.DpdApiClient.async_login",
        new=AsyncMock(side_effect=aiohttp.ClientError("boom")),
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": SOURCE_USER}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], user_input=_USER_FORM_INPUT
        )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "cannot_connect"}


@pytest.mark.asyncio
async def test_user_flow_aborts_when_already_configured(hass):
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    MockConfigEntry(
        domain=DOMAIN,
        unique_id=f"{DEFAULT_BU}:{_USER_INPUT[CONF_EMAIL]}",
        data=_USER_INPUT,
    ).add_to_hass(hass)

    with patch(
        "custom_components.dpd.config_flow.DpdApiClient.async_login",
        new=AsyncMock(return_value=None),
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": SOURCE_USER}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], user_input=_USER_FORM_INPUT
        )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


@pytest.mark.asyncio
async def test_options_flow_updates_filter_and_refresh_interval(hass):
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=f"{DEFAULT_BU}:{_USER_INPUT[CONF_EMAIL]}",
        data=_USER_INPUT,
        options={
            CONF_DELIVERED_FILTER_TYPE: "days",
            CONF_DELIVERED_FILTER_AMOUNT: 7,
        },
    )
    entry.add_to_hass(hass)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.FORM

    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        user_input={
            "delivered": {
                CONF_DELIVERED_FILTER_TYPE: "parcels",
                CONF_DELIVERED_FILTER_AMOUNT: 20,
            },
            "history": {
                CONF_INCLUDE_HISTORY: True,
            },
            "polling": {
                CONF_REFRESH_INTERVAL: "60",
            },
        },
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_DELIVERED_FILTER_TYPE] == "parcels"
    assert result["data"][CONF_DELIVERED_FILTER_AMOUNT] == 20
    assert result["data"][CONF_INCLUDE_HISTORY] is True
    assert result["data"][CONF_REFRESH_INTERVAL] == 60


@pytest.mark.asyncio
async def test_options_flow_refresh_interval_default_is_string(hass):
    """Regression: the refresh-interval default must be a string so a stored
    int doesn't trip the SelectSelector's 'expected str' validation when the
    polling section is submitted without an explicit value."""
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=f"{DEFAULT_BU}:{_USER_INPUT[CONF_EMAIL]}",
        data=_USER_INPUT,
        # A config previously saved by this integration stores an int.
        options={
            CONF_DELIVERED_FILTER_TYPE: "days",
            CONF_DELIVERED_FILTER_AMOUNT: 7,
            CONF_REFRESH_INTERVAL: 30,
            CONF_INCLUDE_HISTORY: False,
        },
    )
    entry.add_to_hass(hass)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        user_input={
            "delivered": {
                CONF_DELIVERED_FILTER_TYPE: "parcels",
                CONF_DELIVERED_FILTER_AMOUNT: 20,
            },
            "history": {CONF_INCLUDE_HISTORY: True},
            "polling": {},  # omitted → default applied; must validate
        },
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_REFRESH_INTERVAL] == DEFAULT_REFRESH_INTERVAL


@pytest.mark.asyncio
async def test_reauth_flow_updates_credentials_and_reloads(hass):
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=f"{DEFAULT_BU}:{_USER_INPUT[CONF_EMAIL]}",
        data=_USER_INPUT,
    )
    entry.add_to_hass(hass)

    with (
        patch(
            "custom_components.dpd.config_flow.DpdApiClient.async_login",
            new=AsyncMock(return_value=None),
        ),
        patch(
            "homeassistant.config_entries.ConfigEntries.async_reload",
            new=AsyncMock(return_value=True),
        ) as mock_reload,
    ):
        result = await entry.start_reauth_flow(hass)
        assert result["type"] is FlowResultType.FORM
        assert result["step_id"] == "reauth_confirm"

        new_creds = {
            CONF_EMAIL: _USER_INPUT[CONF_EMAIL],
            CONF_PASSWORD: "new-secret",
        }
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], user_input=new_creds
        )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert entry.data[CONF_PASSWORD] == "new-secret"
    # BU stays the same after reauth
    assert entry.data[CONF_BU] == DEFAULT_BU
    mock_reload.assert_awaited_once_with(entry.entry_id)


@pytest.mark.asyncio
async def test_reauth_flow_surfaces_invalid_auth(hass):
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=f"{DEFAULT_BU}:{_USER_INPUT[CONF_EMAIL]}",
        data=_USER_INPUT,
    )
    entry.add_to_hass(hass)

    with patch(
        "custom_components.dpd.config_flow.DpdApiClient.async_login",
        new=AsyncMock(side_effect=DpdAuthError("nope")),
    ):
        result = await entry.start_reauth_flow(hass)
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            user_input={CONF_EMAIL: _USER_INPUT[CONF_EMAIL], CONF_PASSWORD: "wrong"},
        )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "invalid_auth"}
    assert entry.data[CONF_PASSWORD] == _USER_INPUT[CONF_PASSWORD]


@pytest.mark.asyncio
async def test_reauth_flow_aborts_on_different_account(hass):
    """Reauthenticating with another account's credentials aborts the flow."""
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=f"{DEFAULT_BU}:{_USER_INPUT[CONF_EMAIL]}",
        data=_USER_INPUT,
    )
    entry.add_to_hass(hass)

    with patch(
        "custom_components.dpd.config_flow.DpdApiClient.async_login",
        new=AsyncMock(return_value=None),
    ):
        result = await entry.start_reauth_flow(hass)
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            user_input={CONF_EMAIL: "other@example.com", CONF_PASSWORD: "pw"},
        )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "unique_id_mismatch"
    assert entry.data[CONF_EMAIL] == _USER_INPUT[CONF_EMAIL]


# ---------------------------------------------------------------------------
# DPD Germany — separate SOAP backend, branched off the same BU dropdown
# ---------------------------------------------------------------------------

_DE_EMAIL = "user@example.de"
_DE_FORM_INPUT = {
    CONF_EMAIL: _DE_EMAIL,
    CONF_PASSWORD: "secret",
    CONF_BU: "dpd-de",
}


@pytest.mark.asyncio
async def test_user_flow_de_creates_entry_with_hardware_id(hass):
    with patch(
        "custom_components.dpd.config_flow.DpdDeSession.async_login",
        new=AsyncMock(return_value=None),
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": SOURCE_USER}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], user_input=_DE_FORM_INPUT
        )
        assert result["type"] is FlowResultType.FORM
        assert result["step_id"] == "delivered"

        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], user_input=_DELIVERED_INPUT
        )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_EMAIL] == _DE_EMAIL
    assert result["data"][CONF_COUNTRY] == COUNTRY_DE.upper()
    assert result["data"][CONF_DE_HARDWARE_ID]
    assert CONF_BU not in result["data"]


@pytest.mark.asyncio
async def test_user_flow_de_invalid_auth(hass):
    with patch(
        "custom_components.dpd.config_flow.DpdDeSession.async_login",
        new=AsyncMock(side_effect=DpdAuthError("nope")),
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": SOURCE_USER}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], user_input=_DE_FORM_INPUT
        )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "invalid_auth"}


@pytest.mark.asyncio
async def test_user_flow_de_cannot_connect(hass):
    with patch(
        "custom_components.dpd.config_flow.DpdDeSession.async_login",
        new=AsyncMock(side_effect=aiohttp.ClientError("boom")),
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": SOURCE_USER}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], user_input=_DE_FORM_INPUT
        )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "cannot_connect"}


@pytest.mark.asyncio
async def test_user_flow_de_aborts_when_already_configured(hass):
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    MockConfigEntry(
        domain=DOMAIN,
        unique_id=f"DE:{_DE_EMAIL}",
        data={
            CONF_EMAIL: _DE_EMAIL,
            CONF_PASSWORD: "secret",
            CONF_COUNTRY: COUNTRY_DE.upper(),
            CONF_DE_HARDWARE_ID: "existing-hw-id",
        },
    ).add_to_hass(hass)

    with patch(
        "custom_components.dpd.config_flow.DpdDeSession.async_login",
        new=AsyncMock(return_value=None),
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": SOURCE_USER}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], user_input=_DE_FORM_INPUT
        )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


@pytest.mark.asyncio
async def test_reauth_flow_de_updates_credentials_and_reloads(hass):
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=f"DE:{_DE_EMAIL}",
        data={
            CONF_EMAIL: _DE_EMAIL,
            CONF_PASSWORD: "secret",
            CONF_COUNTRY: COUNTRY_DE.upper(),
            CONF_DE_HARDWARE_ID: "existing-hw-id",
        },
    )
    entry.add_to_hass(hass)

    with (
        patch(
            "custom_components.dpd.config_flow.DpdDeSession.async_login",
            new=AsyncMock(return_value=None),
        ),
        patch(
            "homeassistant.config_entries.ConfigEntries.async_reload",
            new=AsyncMock(return_value=True),
        ),
    ):
        result = await entry.start_reauth_flow(hass)
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            user_input={CONF_EMAIL: _DE_EMAIL, CONF_PASSWORD: "new-secret"},
        )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert entry.data[CONF_PASSWORD] == "new-secret"
    # The device identity (hardware id) survives a reauth unchanged.
    assert entry.data[CONF_DE_HARDWARE_ID] == "existing-hw-id"


@pytest.mark.asyncio
async def test_reauth_flow_de_surfaces_invalid_auth(hass):
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=f"DE:{_DE_EMAIL}",
        data={
            CONF_EMAIL: _DE_EMAIL,
            CONF_PASSWORD: "secret",
            CONF_COUNTRY: COUNTRY_DE.upper(),
            CONF_DE_HARDWARE_ID: "existing-hw-id",
        },
    )
    entry.add_to_hass(hass)

    with patch(
        "custom_components.dpd.config_flow.DpdDeSession.async_login",
        new=AsyncMock(side_effect=DpdAuthError("nope")),
    ):
        result = await entry.start_reauth_flow(hass)
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            user_input={CONF_EMAIL: _DE_EMAIL, CONF_PASSWORD: "wrong"},
        )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "invalid_auth"}
