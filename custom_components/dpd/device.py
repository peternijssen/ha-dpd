"""The device every entity of this integration belongs to.

One place, because sensors, the button and the calendar must all land on the
*same* device entry. It used to be defined three times — once per platform —
with the button and calendar docstrings noting that they mirrored the sensor's
copy, which is exactly the kind of duplication that drifts.
"""
from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers.device_registry import DeviceEntryType
from homeassistant.helpers.entity import DeviceInfo

from .const import DOMAIN

ATTRIBUTION = "Data provided by DPD"


def build_device_info(entry: ConfigEntry) -> DeviceInfo:
    """Return a DeviceInfo dict shared by all sensors for this account.

    Device name is ``"DPD (<email>)"`` so the auto-prefixed entity
    friendly names read as ``"DPD (account@example.com) Incoming
    parcels"``. Including the account in the device name disambiguates
    users with multiple DPD accounts and matches mainstream HA style
    for cloud-account integrations.
    """
    email = entry.title or ""
    device_name = f"DPD ({email})" if email else "DPD"
    return DeviceInfo(
        identifiers={(DOMAIN, entry.entry_id)},
        name=device_name,
        manufacturer="DPD",
        entry_type=DeviceEntryType.SERVICE,
        configuration_url="https://www.dpdgroup.com",
    )
