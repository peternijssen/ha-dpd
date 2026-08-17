"""DPD custom component for Home Assistant."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from uuid import uuid4

import aiohttp
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import DpdApiClient, DpdApiError, DpdAuthError
from .const import (
    CONF_BU,
    CONF_COUNTRY,
    CONF_DE_HARDWARE_ID,
    COUNTRY_DE,
    COUNTRY_GENERAL,
    DEFAULT_BU,
    PLATFORMS,
)
from .coordinator import DpdCoordinator
from .countries.de.session import DpdDeSession

_LOGGER = logging.getLogger(__name__)


@dataclass
class DpdData:
    """Runtime data attached to a DPD config entry."""

    client: DpdApiClient | None
    coordinator: DpdCoordinator
    de_session: DpdDeSession | None = None


type DpdConfigEntry = ConfigEntry[DpdData]


async def async_setup_entry(hass: HomeAssistant, entry: DpdConfigEntry) -> bool:
    """Set up DPD from a config entry."""
    session = async_get_clientsession(hass)
    country = entry.data.get(CONF_COUNTRY, COUNTRY_GENERAL.upper())

    client: DpdApiClient | None = None
    de_session: DpdDeSession | None = None

    try:
        if country == COUNTRY_DE.upper():
            hardware_id = entry.data.get(CONF_DE_HARDWARE_ID) or str(uuid4())
            de_session = DpdDeSession(
                session,
                entry.data[CONF_EMAIL],
                entry.data[CONF_PASSWORD],
                hardware_id,
            )
            await de_session.async_login()
        else:
            client = DpdApiClient(
                entry.data[CONF_EMAIL],
                entry.data[CONF_PASSWORD],
                session,
                bu=entry.data.get(CONF_BU, DEFAULT_BU),
            )
            await client.async_login()
    except DpdAuthError as exc:
        raise ConfigEntryAuthFailed("DPD authentication failed") from exc
    except DpdApiError as exc:
        # Non-success HTTP status during the auth flow — almost always a 5xx
        # from DPD's auth tier. Surface it as a transient setup failure so HA
        # retries with backoff instead of pushing the user into reauth.
        raise ConfigEntryNotReady(
            f"DPD authentication service returned HTTP {exc.status_code}"
        ) from exc
    except aiohttp.ClientError as exc:
        raise ConfigEntryNotReady("Unable to connect to DPD") from exc

    coordinator = DpdCoordinator(hass, client, entry, de_session=de_session)

    # Fetch initial data here, before forwarding to platforms. Raising
    # ConfigEntryNotReady from a forwarded platform is too late for HA to catch
    # cleanly (it logs a warning and half-sets-up the entry); doing the first
    # refresh here lets a transient failure fail the whole entry so HA retries
    # it with backoff.
    await coordinator.async_config_entry_first_refresh()

    entry.runtime_data = DpdData(client=client, coordinator=coordinator, de_session=de_session)

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: DpdConfigEntry) -> bool:
    """Unload a DPD config entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
