"""Config flow for the DPD integration."""
from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

import aiohttp
import voluptuous as vol
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.core import callback
from homeassistant.data_entry_flow import section
from homeassistant.helpers import selector
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import DpdApiClient, DpdApiError, DpdAuthError
from .const import (
    BUSINESS_UNITS,
    CONF_BU,
    CONF_DELIVERED_FILTER_AMOUNT,
    CONF_DELIVERED_FILTER_TYPE,
    CONF_INCLUDE_HISTORY,
    CONF_REFRESH_INTERVAL,
    DEFAULT_BU,
    DEFAULT_DELIVERED_FILTER_AMOUNT,
    DEFAULT_DELIVERED_FILTER_TYPE,
    DEFAULT_INCLUDE_HISTORY,
    DEFAULT_REFRESH_INTERVAL,
    DOMAIN,
    NEW_COUNTRY_ISSUE_URL,
    REFRESH_INTERVAL_OPTIONS,
)

_LOGGER = logging.getLogger(__name__)

_BU_SELECTOR = selector.SelectSelector(
    selector.SelectSelectorConfig(
        # Option values double as translation keys (hassfest requires
        # lower-case, no upper-case BU codes like ``DPD-DE``) — the
        # upper-case value HA stores/sends to the API is recovered via
        # ``.upper()`` right after the form submits (see async_step_user).
        options=[bu["value"].lower() for bu in BUSINESS_UNITS],
        translation_key=CONF_BU,
        mode=selector.SelectSelectorMode.DROPDOWN,
    )
)

_FILTER_TYPE_SELECTOR = selector.SelectSelector(
    selector.SelectSelectorConfig(
        options=["days", "parcels"],
        translation_key=CONF_DELIVERED_FILTER_TYPE,
        mode=selector.SelectSelectorMode.LIST,
    )
)

_FILTER_AMOUNT_SELECTOR = selector.NumberSelector(
    selector.NumberSelectorConfig(
        min=1,
        max=365,
        step=1,
        mode=selector.NumberSelectorMode.BOX,
    )
)

_USER_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_EMAIL): str,
        vol.Required(CONF_PASSWORD): str,
        vol.Required(CONF_BU, default=DEFAULT_BU): _BU_SELECTOR,
    }
)

_REAUTH_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_EMAIL): str,
        vol.Required(CONF_PASSWORD): str,
    }
)

_DELIVERED_SCHEMA = vol.Schema(
    {
        vol.Required(
            CONF_DELIVERED_FILTER_TYPE, default=DEFAULT_DELIVERED_FILTER_TYPE
        ): _FILTER_TYPE_SELECTOR,
        vol.Required(
            CONF_DELIVERED_FILTER_AMOUNT, default=DEFAULT_DELIVERED_FILTER_AMOUNT
        ): _FILTER_AMOUNT_SELECTOR,
    }
)


class DpdConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle the UI-driven configuration flow for the DPD integration."""

    VERSION = 1

    def __init__(self) -> None:
        """Initialize the config flow."""
        self._email: str = ""
        self._password: str = ""
        self._bu: str = DEFAULT_BU

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> DpdOptionsFlowHandler:
        """Return the options flow handler."""
        return DpdOptionsFlowHandler()

    async def _validate_credentials(self, email: str, password: str, bu: str) -> None:
        """Validate credentials against the live DPD auth flow."""
        session = async_get_clientsession(self.hass)
        client = DpdApiClient(email, password, session, bu=bu)
        await client.async_login()

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show the credential form and validate on submit."""
        errors: dict[str, str] = {}

        if user_input is not None:
            email = user_input[CONF_EMAIL]
            password = user_input[CONF_PASSWORD]
            bu = user_input[CONF_BU].upper()

            try:
                await self._validate_credentials(email, password, bu)
            except DpdAuthError:
                errors["base"] = "invalid_auth"
            except (DpdApiError, aiohttp.ClientError):
                errors["base"] = "cannot_connect"
            else:
                await self.async_set_unique_id(f"{bu}:{email}")
                self._abort_if_unique_id_configured()
                self._email = email
                self._password = password
                self._bu = bu
                return await self.async_step_delivered()

        return self.async_show_form(
            step_id="user",
            data_schema=_USER_SCHEMA,
            errors=errors,
            description_placeholders={"issue_url": NEW_COUNTRY_ISSUE_URL},
        )

    async def async_step_delivered(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show the delivered-parcels filter form."""
        if user_input is not None:
            return self.async_create_entry(
                title=self._email,
                data={
                    CONF_EMAIL: self._email,
                    CONF_PASSWORD: self._password,
                    CONF_BU: self._bu,
                },
                options={
                    CONF_DELIVERED_FILTER_TYPE: user_input[CONF_DELIVERED_FILTER_TYPE],
                    CONF_DELIVERED_FILTER_AMOUNT: int(
                        user_input[CONF_DELIVERED_FILTER_AMOUNT]
                    ),
                },
            )

        return self.async_show_form(
            step_id="delivered",
            data_schema=_DELIVERED_SCHEMA,
        )

    async def async_step_reauth(
        self, entry_data: Mapping[str, Any]
    ) -> ConfigFlowResult:
        """Initiate re-authentication for an existing config entry."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show the re-auth credential form and update the existing entry on success."""
        errors: dict[str, str] = {}
        reauth_entry = self._get_reauth_entry()
        bu = reauth_entry.data.get(CONF_BU, DEFAULT_BU)

        if user_input is not None:
            email = user_input[CONF_EMAIL]
            password = user_input[CONF_PASSWORD]

            try:
                await self._validate_credentials(email, password, bu)
            except DpdAuthError:
                errors["base"] = "invalid_auth"
            except (DpdApiError, aiohttp.ClientError):
                errors["base"] = "cannot_connect"
            else:
                # Guard against re-authenticating with a *different* DPD
                # account — the entry (and all its entities) belong to the
                # original account's unique_id.
                await self.async_set_unique_id(f"{bu}:{email}")
                self._abort_if_unique_id_mismatch()
                return self.async_update_reload_and_abort(
                    reauth_entry,
                    data_updates={
                        CONF_EMAIL: email,
                        CONF_PASSWORD: password,
                    },
                )

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=_REAUTH_SCHEMA,
            errors=errors,
        )


class DpdOptionsFlowHandler(OptionsFlow):
    """Handle DPD options — delivered-parcels filter plus polling cadence.

    The form is rendered with two collapsible sections (``delivered`` and
    ``polling``) so the unrelated knobs don't compete for attention. HA
    returns the user input nested by section name; we flatten it before
    storing on the config entry so the coordinator can keep reading the
    flat keys directly.
    """

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show the options form."""
        if user_input is not None:
            delivered = user_input.get("delivered", {})
            history = user_input.get("history", {})
            polling = user_input.get("polling", {})
            self.hass.config_entries.async_schedule_reload(self.config_entry.entry_id)
            return self.async_create_entry(
                title="",
                data={
                    CONF_DELIVERED_FILTER_TYPE: delivered[CONF_DELIVERED_FILTER_TYPE],
                    CONF_DELIVERED_FILTER_AMOUNT: int(
                        delivered[CONF_DELIVERED_FILTER_AMOUNT]
                    ),
                    CONF_INCLUDE_HISTORY: bool(history[CONF_INCLUDE_HISTORY]),
                    CONF_REFRESH_INTERVAL: int(polling[CONF_REFRESH_INTERVAL]),
                },
            )

        current = self.config_entry.options
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Required("delivered"): section(
                        vol.Schema(
                            {
                                vol.Required(
                                    CONF_DELIVERED_FILTER_TYPE,
                                    default=current.get(
                                        CONF_DELIVERED_FILTER_TYPE,
                                        DEFAULT_DELIVERED_FILTER_TYPE,
                                    ),
                                ): _FILTER_TYPE_SELECTOR,
                                vol.Required(
                                    CONF_DELIVERED_FILTER_AMOUNT,
                                    default=current.get(
                                        CONF_DELIVERED_FILTER_AMOUNT,
                                        DEFAULT_DELIVERED_FILTER_AMOUNT,
                                    ),
                                ): _FILTER_AMOUNT_SELECTOR,
                            }
                        ),
                        {"collapsed": False},
                    ),
                    vol.Required("history"): section(
                        vol.Schema(
                            {
                                vol.Required(
                                    CONF_INCLUDE_HISTORY,
                                    default=current.get(
                                        CONF_INCLUDE_HISTORY,
                                        DEFAULT_INCLUDE_HISTORY,
                                    ),
                                ): selector.BooleanSelector(),
                            }
                        ),
                        {"collapsed": True},
                    ),
                    vol.Required("polling"): section(
                        vol.Schema(
                            {
                                vol.Required(
                                    CONF_REFRESH_INTERVAL,
                                    # str(): the selector's option values are
                                    # strings, so the default must be a string
                                    # too — a stored int won't match and trips
                                    # "expected str" validation on submit.
                                    default=str(current.get(
                                        CONF_REFRESH_INTERVAL,
                                        DEFAULT_REFRESH_INTERVAL,
                                    )),
                                ): selector.SelectSelector(
                                    selector.SelectSelectorConfig(
                                        options=[str(m) for m in REFRESH_INTERVAL_OPTIONS],
                                        translation_key=CONF_REFRESH_INTERVAL,
                                        mode=selector.SelectSelectorMode.DROPDOWN,
                                    )
                                ),
                            }
                        ),
                        {"collapsed": True},
                    ),
                }
            ),
        )
