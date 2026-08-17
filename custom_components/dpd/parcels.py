"""Per-country ``normalize_parcel``/``map_parcel_status`` dispatcher.

The general/NL+14-BU status map, event-history and ``normalize_parcel``
logic moved to ``countries/general/``; DPD Germany's equivalents live in
``countries/de/``. This module re-exports the general implementation under
its original names, so existing imports keep working.

``sort_parcels_by_ts`` and ``_apply_delivered_filter`` stay here: they
operate on already-normalized parcel dicts, the same regardless of country.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from homeassistant.config_entries import ConfigEntry

from .const import (
    CONF_DELIVERED_FILTER_AMOUNT,
    CONF_DELIVERED_FILTER_TYPE,
    DEFAULT_DELIVERED_FILTER_AMOUNT,
    DEFAULT_DELIVERED_FILTER_TYPE,
)
from .countries.general import (
    _augment_dimensions,
    _description,
    _tracking_url,
    _unknown_descriptions_logged,
    build_history,
    filter_active_shipments,
    filter_delivered_shipments,
    fmp_hashcode,
    log_unknown_descriptions,
    map_event_status,
    map_parcel_status,
    normalize_parcel,
    shipment_delivery_dt,
    shipment_planned_dt,
    shipment_planned_window,
)

__all__ = [
    "_augment_dimensions",
    "_description",
    "_tracking_url",
    "_unknown_descriptions_logged",
    "build_history",
    "filter_active_shipments",
    "filter_delivered_shipments",
    "fmp_hashcode",
    "log_unknown_descriptions",
    "map_event_status",
    "map_parcel_status",
    "normalize_parcel",
    "shipment_delivery_dt",
    "shipment_planned_dt",
    "shipment_planned_window",
    "sort_parcels_by_ts",
    "_apply_delivered_filter",
    "_apply_delivered_filter_canonical",
]


def sort_parcels_by_ts(
    parcels: list[dict], key_field: str, *, descending: bool = False
) -> list[dict]:
    """Return normalized parcels sorted by the ISO timestamp at ``key_field``.

    Parcels whose value is missing or unparseable always sort to the end,
    regardless of ``descending`` — so freshly registered parcels without
    an ETA stay visible at the bottom instead of jumping to the top.
    """
    with_ts: list[tuple[datetime, dict]] = []
    without_ts: list[dict] = []
    for parcel in parcels:
        value = parcel.get(key_field)
        if not isinstance(value, str) or not value:
            without_ts.append(parcel)
            continue
        try:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            without_ts.append(parcel)
            continue
        with_ts.append((dt, parcel))
    with_ts.sort(key=lambda item: item[0], reverse=descending)
    return [p for _, p in with_ts] + without_ts


def _apply_delivered_filter(shipments: list[dict], entry: ConfigEntry) -> list[dict]:
    """Trim the delivered list according to the configured options.

    Operates on raw general-backend shipments (via ``shipment_delivery_dt``)
    — the general/NL path filters before normalizing. DE uses
    ``_apply_delivered_filter_canonical`` instead.
    """
    options = entry.options
    filter_type = options.get(
        CONF_DELIVERED_FILTER_TYPE, DEFAULT_DELIVERED_FILTER_TYPE
    )
    filter_amount = int(
        options.get(CONF_DELIVERED_FILTER_AMOUNT, DEFAULT_DELIVERED_FILTER_AMOUNT)
    )

    if filter_type == "days":
        cutoff = datetime.now(timezone.utc) - timedelta(days=filter_amount)
        return [
            s for s in shipments
            if (dt := shipment_delivery_dt(s)) is None or dt >= cutoff
        ]

    return shipments[:filter_amount]


def _apply_delivered_filter_canonical(
    parcels: list[dict], entry: ConfigEntry
) -> list[dict]:
    """Apply the same delivered-retention filter, reading an already-normalized parcel.

    Reads ``delivered_at`` directly instead of DPD's raw status shape — DE's
    coordinator path normalizes before filtering, unlike the general path.
    """
    options = entry.options
    filter_type = options.get(
        CONF_DELIVERED_FILTER_TYPE, DEFAULT_DELIVERED_FILTER_TYPE
    )
    filter_amount = int(
        options.get(CONF_DELIVERED_FILTER_AMOUNT, DEFAULT_DELIVERED_FILTER_AMOUNT)
    )

    def _delivered_at(parcel: dict) -> datetime | None:
        value = parcel.get("delivered_at")
        if not value:
            return None
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            return None

    if filter_type == "days":
        cutoff = datetime.now(timezone.utc) - timedelta(days=filter_amount)
        return [
            p for p in parcels
            if (dt := _delivered_at(p)) is None or dt >= cutoff
        ]

    return parcels[:filter_amount]
