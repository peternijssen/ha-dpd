"""Sensor platform for the DPD integration."""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import DpdConfigEntry
from .const import DOMAIN, ParcelStatus
from .coordinator import DpdCoordinator
from .device import ATTRIBUTION, build_device_info

_LOGGER = logging.getLogger(__name__)

# The DataUpdateCoordinator handles fan-out; HA's per-entity throttling adds nothing.
PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: DpdConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up DPD sensor entities from a config entry."""
    # The coordinator is already refreshed by __init__.py before platforms are
    # forwarded, so ConfigEntryNotReady is raised from the entry setup rather
    # than (too late) from this forwarded platform.
    coordinator = entry.runtime_data.coordinator

    current_parcels: list[dict] = (coordinator.data or {}).get("incoming_active", [])
    current_numbers: set[str] = {
        p.get("barcode", "") for p in current_parcels if p.get("barcode")
    }

    # Drop per-parcel entries from the registry that are no longer active —
    # handles parcels that were delivered between HA restarts.
    registry = er.async_get(hass)
    entry_id = entry.entry_id
    non_parcel_unique_ids = {
        f"{entry_id}_incoming_parcels",
        f"{entry_id}_outgoing_parcels",
        f"{entry_id}_outgoing_delivered_parcels",
        f"{entry_id}_delivered_parcels",
        f"{entry_id}_next_delivery",
        f"{entry_id}_en_route_to_parcel_shop",
        f"{entry_id}_awaiting_pickup",
        f"{entry_id}_last_update",
    }
    for entity_entry in er.async_entries_for_config_entry(registry, entry.entry_id):
        # Only per-parcel *sensors* are managed here; skip other platforms
        # (e.g. the refresh button) whose unique_id also starts with entry_id_.
        if (
            entity_entry.domain == "sensor"
            and entity_entry.unique_id.startswith(f"{entry_id}_")
            and entity_entry.unique_id not in non_parcel_unique_ids
        ):
            parcel_number = entity_entry.unique_id[len(f"{entry_id}_"):]
            if parcel_number not in current_numbers:
                registry.async_remove(entity_entry.entity_id)

    entities: list[SensorEntity] = [
        DpdIncomingParcelsSensor(
            coordinator, entry, async_add_entities, current_numbers
        ),
        DpdOutgoingParcelsSensor(coordinator, entry),
        DpdOutgoingDeliveredParcelsSensor(coordinator, entry),
        DpdDeliveredParcelsSensor(coordinator, entry),
        DpdNextDeliverySensor(coordinator, entry),
        DpdEnRouteToParcelShopSensor(coordinator, entry),
        DpdAwaitingPickupSensor(coordinator, entry),
        DpdLastUpdateSensor(coordinator, entry),
    ]
    for parcel in current_parcels:
        barcode = parcel.get("barcode", "")
        if barcode:
            entities.append(DpdParcelSensor(coordinator, entry, barcode))

    async_add_entities(entities)




class DpdIncomingParcelsSensor(CoordinatorEntity[DpdCoordinator], SensorEntity):
    """Summary sensor reporting the count of active incoming DPD parcels.

    Spawns a per-parcel :class:`DpdParcelSensor` whenever a new parcel
    number appears, and removes the per-parcel sensor from the entity
    registry when its number drops out of the coordinator data. Doing the
    removal here (synchronously, via the registry) instead of having the
    per-parcel sensor self-remove from inside its own
    ``_handle_coordinator_update`` avoids the race where
    ``async_remove(force_remove=True)`` competes with the coordinator-
    listener cleanup and leaves a ghost entity behind.
    """

    _attr_has_entity_name = True
    _attr_translation_key = "incoming_parcels"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_attribution = ATTRIBUTION
    _unrecorded_attributes = frozenset({"parcels"})

    def __init__(
        self,
        coordinator: DpdCoordinator,
        entry: ConfigEntry,
        async_add_entities: AddEntitiesCallback,
        known_parcel_numbers: set[str] | None = None,
    ) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator)
        self._entry = entry
        self._async_add_entities = async_add_entities
        self._known_parcel_numbers: set[str] = known_parcel_numbers or set()
        self._attr_unique_id = f"{entry.entry_id}_incoming_parcels"
        self._attr_device_info = build_device_info(entry)

    @property
    def _parcels(self) -> list[dict]:
        return (self.coordinator.data or {}).get("incoming_active", [])

    @property
    def native_value(self) -> int:
        """Return the native value of the sensor."""
        return len(self._parcels)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return the extra state attributes."""
        return {"parcels": self._parcels}

    def _handle_coordinator_update(self) -> None:
        current_numbers = {
            p.get("barcode", "")
            for p in self._parcels
            if p.get("barcode")
        }

        new_numbers = current_numbers - self._known_parcel_numbers
        if new_numbers:
            self._async_add_entities(
                DpdParcelSensor(self.coordinator, self._entry, n)
                for n in new_numbers
            )

        removed_numbers = self._known_parcel_numbers - current_numbers
        if removed_numbers:
            registry = er.async_get(self.hass)
            for barcode in removed_numbers:
                entity_id = registry.async_get_entity_id(
                    "sensor", DOMAIN, f"{self._entry.entry_id}_{barcode}"
                )
                if entity_id:
                    registry.async_remove(entity_id)

        self._known_parcel_numbers = current_numbers
        super()._handle_coordinator_update()


class DpdParcelSensor(CoordinatorEntity[DpdCoordinator], SensorEntity):
    """Per-parcel sensor reporting the canonical ParcelStatus of a single active incoming shipment."""

    _attr_has_entity_name = True
    _attr_translation_key = "parcel"
    _attr_attribution = ATTRIBUTION
    _unrecorded_attributes = frozenset({"raw", "history"})

    def __init__(
        self,
        coordinator: DpdCoordinator,
        entry: ConfigEntry,
        barcode: str,
    ) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator)
        self._barcode = barcode
        self._attr_unique_id = f"{entry.entry_id}_{barcode}"
        self._attr_translation_placeholders = {"barcode": barcode}
        self._attr_device_info = build_device_info(entry)

    def _get_parcel(self) -> dict[str, Any] | None:
        for parcel in (self.coordinator.data or {}).get("incoming_active", []):
            if parcel.get("barcode") == self._barcode:
                return parcel
        return None

    @property
    def native_value(self) -> str | None:
        """Return the native value of the sensor."""
        parcel = self._get_parcel()
        if not parcel:
            return None
        return parcel.get("status")

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return the extra state attributes."""
        parcel = self._get_parcel()
        return dict(parcel) if parcel else {}



class DpdOutgoingParcelsSensor(CoordinatorEntity[DpdCoordinator], SensorEntity):
    """Summary sensor reporting the count of active outgoing DPD shipments."""

    _attr_has_entity_name = True
    _attr_translation_key = "outgoing_parcels"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_attribution = ATTRIBUTION
    _unrecorded_attributes = frozenset({"parcels"})

    def __init__(self, coordinator: DpdCoordinator, entry: ConfigEntry) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_outgoing_parcels"
        self._attr_device_info = build_device_info(entry)

    @property
    def _shipments(self) -> list[dict]:
        return (self.coordinator.data or {}).get("outgoing_active", [])

    @property
    def native_value(self) -> int:
        """Return the native value of the sensor."""
        return len(self._shipments)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return the extra state attributes."""
        return {"parcels": self._shipments}


class DpdOutgoingDeliveredParcelsSensor(CoordinatorEntity[DpdCoordinator], SensorEntity):
    """Sensor reporting recently delivered outgoing DPD shipments."""

    _attr_has_entity_name = True
    _attr_translation_key = "outgoing_delivered_parcels"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_attribution = ATTRIBUTION
    _unrecorded_attributes = frozenset({"parcels"})

    def __init__(self, coordinator: DpdCoordinator, entry: ConfigEntry) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_outgoing_delivered_parcels"
        self._attr_device_info = build_device_info(entry)

    @property
    def _shipments(self) -> list[dict]:
        return (self.coordinator.data or {}).get("outgoing_delivered", [])

    @property
    def native_value(self) -> int:
        """Return the native value of the sensor."""
        return len(self._shipments)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return the extra state attributes."""
        return {"parcels": self._shipments}


class DpdDeliveredParcelsSensor(CoordinatorEntity[DpdCoordinator], SensorEntity):
    """Sensor reporting recently delivered incoming DPD parcels."""

    _attr_has_entity_name = True
    _attr_translation_key = "delivered_parcels"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_attribution = ATTRIBUTION
    _unrecorded_attributes = frozenset({"parcels"})

    def __init__(self, coordinator: DpdCoordinator, entry: ConfigEntry) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_delivered_parcels"
        self._attr_device_info = build_device_info(entry)

    @property
    def _parcels(self) -> list[dict]:
        return (self.coordinator.data or {}).get("incoming_delivered", [])

    @property
    def native_value(self) -> int:
        """Return the native value of the sensor."""
        return len(self._parcels)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return the extra state attributes."""
        return {"parcels": self._parcels}


class DpdNextDeliverySensor(CoordinatorEntity[DpdCoordinator], SensorEntity):
    """Earliest expected delivery datetime across all active incoming DPD parcels.

    Reads each parcel's ``planned_from`` (set by the coordinator's
    normalisation step from the Follow My Parcel window when available
    and the calendar-day midnight otherwise) and picks the earliest.
    """

    _attr_has_entity_name = True
    _attr_translation_key = "next_delivery"
    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_attribution = ATTRIBUTION

    def __init__(self, coordinator: DpdCoordinator, entry: ConfigEntry) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_next_delivery"
        self._attr_device_info = build_device_info(entry)

    def _delivery_moments(self) -> list[tuple[datetime, dict]]:
        result: list[tuple[datetime, dict]] = []
        for parcel in (self.coordinator.data or {}).get("incoming_active", []):
            iso = parcel.get("planned_from")
            if not iso:
                continue
            try:
                dt = datetime.fromisoformat(iso)
            except ValueError:
                continue
            result.append((dt, parcel))
        return result

    @property
    def native_value(self) -> datetime | None:
        """Return the native value of the sensor."""
        moments = self._delivery_moments()
        return min(dt for dt, _ in moments) if moments else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return the extra state attributes."""
        moments = self._delivery_moments()
        if not moments:
            return {}
        _, earliest = min(moments, key=lambda x: x[0])
        return {
            "barcode": earliest.get("barcode"),
            "sender": earliest.get("sender"),
            "receiver": earliest.get("receiver"),
        }


class DpdEnRouteToParcelShopSensor(CoordinatorEntity[DpdCoordinator], SensorEntity):
    """Active incoming DPD parcels still in transit to a ParcelShop pickup point.

    Counts non-delivered parcels with ``status.deliveryType == "PARCELSHOP"``
    that have **not yet arrived** at the shop — parcels that are ready for
    collection (``status == at_pickup_point``, the ``AVAILABLE_FOR_COLLECTION``
    description) are counted by :class:`DpdAwaitingPickupSensor` instead.
    """

    _attr_has_entity_name = True
    _attr_translation_key = "en_route_to_parcel_shop"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_attribution = ATTRIBUTION
    _unrecorded_attributes = frozenset({"parcels"})

    def __init__(self, coordinator: DpdCoordinator, entry: ConfigEntry) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_en_route_to_parcel_shop"
        self._attr_device_info = build_device_info(entry)

    def _get_parcelshop_parcels(self) -> list[dict]:
        return [
            p for p in (self.coordinator.data or {}).get("incoming_active", [])
            if p.get("pickup")
            and p.get("status") != ParcelStatus.AT_PICKUP_POINT
        ]

    @property
    def native_value(self) -> int:
        """Return the native value of the sensor."""
        return len(self._get_parcelshop_parcels())

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return the extra state attributes."""
        return {"parcels": self._get_parcelshop_parcels()}


class DpdAwaitingPickupSensor(CoordinatorEntity[DpdCoordinator], SensorEntity):
    """Incoming DPD parcels that have arrived at a ParcelShop and are ready to collect.

    A parcel is counted when it is destined for a ParcelShop (``pickup``) and
    its status is ``at_pickup_point`` — DPD's ``AVAILABLE_FOR_COLLECTION``
    description. Mirrors the awaiting-pickup sensor on the DHL and PostNL
    integrations.
    """

    _attr_has_entity_name = True
    _attr_translation_key = "awaiting_pickup"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_attribution = ATTRIBUTION
    _unrecorded_attributes = frozenset({"parcels"})

    def __init__(self, coordinator: DpdCoordinator, entry: ConfigEntry) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_awaiting_pickup"
        self._attr_device_info = build_device_info(entry)

    def _get_awaiting_parcels(self) -> list[dict]:
        return [
            p for p in (self.coordinator.data or {}).get("incoming_active", [])
            if p.get("pickup")
            and p.get("status") == ParcelStatus.AT_PICKUP_POINT
        ]

    @property
    def native_value(self) -> int:
        """Return the native value of the sensor."""
        return len(self._get_awaiting_parcels())

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return the extra state attributes."""
        return {"parcels": self._get_awaiting_parcels()}


class DpdLastUpdateSensor(CoordinatorEntity[DpdCoordinator], SensorEntity):
    """Diagnostic sensor reporting when DPD was last polled successfully.

    Updates on every successful coordinator refresh, even when no parcel
    value changes — so users can alert on a silently-stale integration
    (e.g. expired auth) that the count sensors would not reveal.
    """

    _attr_has_entity_name = True
    _attr_translation_key = "last_update"
    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_attribution = ATTRIBUTION

    def __init__(self, coordinator: DpdCoordinator, entry: ConfigEntry) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_last_update"
        self._attr_device_info = build_device_info(entry)

    @property
    def native_value(self) -> datetime | None:
        """Return the timestamp of the last successful poll."""
        return self.coordinator.last_success_time
