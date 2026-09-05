"""Config flow for the DPD integration."""
from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any
from uuid import uuid4

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
    CONF_BU,
    CONF_COUNTRY,
    CONF_DE_HARDWARE_ID,
    CONF_DELIVERED_FILTER_AMOUNT,
    CONF_DELIVERED_FILTER_TYPE,
    CONF_INCLUDE_HISTORY,
    CONF_REFRESH_INTERVAL,
    COUNTRY_DE,
    COUNTRY_GENERAL,
    COUNTRY_OPTIONS,
    DEFAULT_BU,
    DEFAULT_DELIVERED_FILTER_AMOUNT,
    DEFAULT_DELIVERED_FILTER_TYPE,
    DEFAULT_INCLUDE_HISTORY,
    DEFAULT_NEW_REFRESH_INTERVAL,
    DEFAULT_REFRESH_INTERVAL,
    DOMAIN,
    NEW_COUNTRY_ISSUE_URL,
    REFRESH_INTERVAL_AUTO,
    REFRESH_INTERVAL_OPTIONS,
)
from .countries.de.session import DpdDeSession

_LOGGER = logging.getLogger(__name__)

# The one selected value that routes to DPD Germany instead of the general
# backend — never added to BUSINESS_UNITS itself (see COUNTRY_OPTIONS).
_DE_BU_VALUE = "DPD-DE"

_BU_SELECTOR = selector.SelectSelector(
    selector.SelectSelectorConfig(
        # Option values double as translation keys (hassfest requires
        # lower-case, no upper-case BU codes like ``DPD-DE``) — the
        # upper-case value HA stores/sends is recovered via ``.upper()``
        # right after the form submits (see async_step_user).
        options=[bu["value"].lower() for bu in COUNTRY_OPTIONS],
        translation_key=CONF_BU,
        mode=selector.SelectSelectorMode.DROPDOWN,
        sort=True,
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
        # No default: the list now includes Germany's wholly separate
        # backend, so silently pre-selecting NL risked a DE account's
        # credentials being validated against the wrong backend.
        vol.Required(CONF_BU): _BU_SELECTOR,
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
        self._country: str = COUNTRY_GENERAL.upper()
        self._email: str = ""
        self._password: str = ""
        self._bu: str = DEFAULT_BU
        self._de_hardware_id: str = ""

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> DpdOptionsFlowHandler:
        """Return the options flow handler."""
        return DpdOptionsFlowHandler()

    async def _validate_general_credentials(
        self, email: str, password: str, bu: str
    ) -> None:
        """Validate credentials against the live general/NL auth flow."""
        session = async_get_clientsession(self.hass)
        client = DpdApiClient(email, password, session, bu=bu)
        await client.async_login()

    async def _validate_de_credentials(
        self, email: str, password: str, hardware_id: str
    ) -> None:
        """Validate credentials against the live DE login."""
        session = async_get_clientsession(self.hass)
        de_session = DpdDeSession(session, email, password, hardware_id)
        await de_session.async_login()

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show the credential + country/BU form and validate on submit.

        One dropdown for both: DPD Germany's separate SOAP backend sits
        alongside NL and the other business units (``COUNTRY_OPTIONS``) —
        picking it branches this same submit into the DE validation path
        instead of asking "which backend" as a question of its own.
        """
        errors: dict[str, str] = {}

        if user_input is not None:
            email = user_input[CONF_EMAIL]
            password = user_input[CONF_PASSWORD]
            selected = user_input[CONF_BU].upper()

            if selected == _DE_BU_VALUE:
                hardware_id = str(uuid4())
                try:
                    await self._validate_de_credentials(email, password, hardware_id)
                except DpdAuthError:
                    errors["base"] = "invalid_auth"
                except (DpdApiError, aiohttp.ClientError):
                    errors["base"] = "cannot_connect"
                else:
                    await self.async_set_unique_id(f"DE:{email}")
                    self._abort_if_unique_id_configured()
                    self._email = email
                    self._password = password
                    self._country = COUNTRY_DE.upper()
                    self._de_hardware_id = hardware_id
                    return await self.async_step_delivered()
            else:
                try:
                    await self._validate_general_credentials(
                        email, password, selected
                    )
                except DpdAuthError:
                    errors["base"] = "invalid_auth"
                except (DpdApiError, aiohttp.ClientError):
                    errors["base"] = "cannot_connect"
                else:
                    await self.async_set_unique_id(f"{selected}:{email}")
                    self._abort_if_unique_id_configured()
                    self._email = email
                    self._password = password
                    self._bu = selected
                    self._country = COUNTRY_GENERAL.upper()
                    return await self.async_step_delivered()

        schema = (
            self.add_suggested_values_to_schema(_USER_SCHEMA, user_input)
            if user_input is not None
            else _USER_SCHEMA
        )
        return self.async_show_form(
            step_id="user",
            data_schema=schema,
            errors=errors,
            description_placeholders={"issue_url": NEW_COUNTRY_ISSUE_URL},
        )

    async def async_step_delivered(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show the delivered-parcels filter form."""
        if user_input is not None:
            data: dict[str, Any] = {
                CONF_EMAIL: self._email,
                CONF_PASSWORD: self._password,
                CONF_COUNTRY: self._country,
            }
            if self._country == COUNTRY_GENERAL.upper():
                data[CONF_BU] = self._bu
            else:
                data[CONF_DE_HARDWARE_ID] = self._de_hardware_id
            return self.async_create_entry(
                title=self._email,
                data=data,
                options={
                    CONF_DELIVERED_FILTER_TYPE: user_input[CONF_DELIVERED_FILTER_TYPE],
                    CONF_DELIVERED_FILTER_AMOUNT: int(
                        user_input[CONF_DELIVERED_FILTER_AMOUNT]
                    ),
                    # New installs default to dynamic polling; an entry set
                    # up before this option existed keeps reading
                    # DEFAULT_REFRESH_INTERVAL via the coordinator's .get()
                    # fallback instead (Section 5.2).
                    CONF_REFRESH_INTERVAL: DEFAULT_NEW_REFRESH_INTERVAL,
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
        country = reauth_entry.data.get(CONF_COUNTRY, COUNTRY_GENERAL.upper())
        bu = reauth_entry.data.get(CONF_BU, DEFAULT_BU)

        if user_input is not None:
            email = user_input[CONF_EMAIL]
            password = user_input[CONF_PASSWORD]

            try:
                if country == COUNTRY_DE.upper():
                    hardware_id = reauth_entry.data.get(CONF_DE_HARDWARE_ID) or str(
                        uuid4()
                    )
                    await self._validate_de_credentials(email, password, hardware_id)
                else:
                    await self._validate_general_credentials(email, password, bu)
            except DpdAuthError:
                errors["base"] = "invalid_auth"
            except (DpdApiError, aiohttp.ClientError):
                errors["base"] = "cannot_connect"
            else:
                # Guard against re-authenticating with a *different* DPD
                # account — the entry (and all its entities) belong to the
                # original account's unique_id.
                unique_id = (
                    f"DE:{email}" if country == COUNTRY_DE.upper() else f"{bu}:{email}"
                )
                await self.async_set_unique_id(unique_id)
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
                    CONF_REFRESH_INTERVAL: (
                        REFRESH_INTERVAL_AUTO
                        if polling[CONF_REFRESH_INTERVAL] == REFRESH_INTERVAL_AUTO
                        else int(polling[CONF_REFRESH_INTERVAL])
                    ),
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
                                        options=[REFRESH_INTERVAL_AUTO]
                                        + [str(m) for m in REFRESH_INTERVAL_OPTIONS],
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
