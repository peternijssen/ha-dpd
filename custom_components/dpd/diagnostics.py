"""Diagnostics support for the DPD integration."""
from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.core import HomeAssistant

from . import DpdConfigEntry
from .const import CONF_DE_HARDWARE_ID

TO_REDACT = {
    CONF_EMAIL,
    CONF_PASSWORD,
    CONF_DE_HARDWARE_ID,
    "email",
    "parcelNumber",
    "senderName",
    "recipientName",
    "postalCode",
    "street",
    "houseNumber",
    "city",
    "phoneNumber",
    # DPD Germany field names (different casing/shape from the general
    # backend above) — the session token and every address/contact field
    # an AddressType can carry.
    "SessionToken",
    "HardwareID",
    "ParcelNo",
    "Street",
    "HouseNo",
    "ZipCode",
    "City",
    "Phone",
    "Mail",
    "FirstName",
    "LastName",
    "Name",
    "Company",
    "ReceiverName",
}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: DpdConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for a DPD config entry."""
    coordinator = entry.runtime_data.coordinator
    data = coordinator.data or {}

    return {
        "entry_data": async_redact_data(dict(entry.data), TO_REDACT),
        "entry_options": dict(entry.options),
        "last_update_success": coordinator.last_update_success,
        "counts": {
            "incoming_active": len(data.get("incoming_active", [])),
            "incoming_delivered": len(data.get("incoming_delivered", [])),
            "outgoing_active": len(data.get("outgoing_active", [])),
            "outgoing_delivered": len(data.get("outgoing_delivered", [])),
        },
        "polling": {
            "current_tier_minutes": coordinator.current_tier_minutes,
            "update_interval_seconds": (
                coordinator.update_interval.total_seconds()
                if coordinator.update_interval
                else None
            ),
        },
        "incoming_active": async_redact_data(data.get("incoming_active", []), TO_REDACT),
        "incoming_delivered": async_redact_data(
            data.get("incoming_delivered", []), TO_REDACT
        ),
        "outgoing_active": async_redact_data(data.get("outgoing_active", []), TO_REDACT),
        "outgoing_delivered": async_redact_data(
            data.get("outgoing_delivered", []), TO_REDACT
        ),
    }
