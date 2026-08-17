"""DPD Germany: derivation-first status map, ``normalize_parcel_de``, warnings.

No DE response has ever been seen on the wire, so every mapping below is
reconstructed from a static APK teardown. The status map is
derivation-first, not enum-first: the ``StatusID`` string vocabulary has no
closed enum, but the structural fields (``Delivered``, ``ParcelFlowTypeID``,
``DeliveryParcelShop.ParcelStatus``, ``StatusInfoContainer``) can be
trusted. The WARNING helpers here are how the vocabulary gets completed
once a real account is polled.

The SOAP transport, ``KeyPhase`` signing and session lifecycle live in
``.session`` — this module never touches the wire itself.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from ...const import HISTORY_MAX_EVENTS, ParcelStatus
from .session import DpdApiError, DpdAuthError, DpdDeSession, as_list

_LOGGER = logging.getLogger(__name__)

_NEW_ISSUE_URL = (
    "https://github.com/ha-parcel-integrations/ha-dpd/issues/new"
    "?template=unrecognised_status.yml"
)

# No confirmed tracking-page URL exists for this surface — DE has no
# business unit for the general backend's URL template, and the web portal
# has no known by-number deep link. Ship None rather than guess.
_TRACKING_URL: str | None = None

# ---------------------------------------------------------------------------
# Status mapping — derivation-first
# ---------------------------------------------------------------------------

# The only StatusIDs recoverable from the APK, all inferred. Anything else
# falls through to the StatusInfoContainer step and is reported via
# _warn_unmapped_status_id so this table can grow.
_STATUS_ID_MAP: dict[str, ParcelStatus] = {
    "DELIVERED": ParcelStatus.DELIVERED,
    "OUT_FOR_DELIVERY": ParcelStatus.OUT_FOR_DELIVERY,
    # A failed attempt is the signal a user wants an alert for.
    "DELIVERY_ATTEMPT": ParcelStatus.PROBLEM,
    "RETURN_TO_SENDER": ParcelStatus.RETURNING,
    # "Parcel known, no scans yet" is a real state, not an error.
    "NO_TRACKINGDATA": ParcelStatus.REGISTERED,
}

_SHOP_STATUS_MAP: dict[str, ParcelStatus] = {
    "parcel_pickedup_by_consignee": ParcelStatus.DELIVERED,
    "parcel_in_shop": ParcelStatus.AT_PICKUP_POINT,
    "newdelivery_in_progress": ParcelStatus.IN_TRANSIT,
}

# StatusInfoContainer's five fixed slots, deepest reached wins — walked top
# to bottom, first StatusReached=true decides.
_CONTAINER_SLOTS: tuple[tuple[str, ParcelStatus], ...] = (
    ("Delivered", ParcelStatus.DELIVERED),
    ("CarLoad", ParcelStatus.OUT_FOR_DELIVERY),
    ("DeliveryDepot", ParcelStatus.IN_TRANSIT),
    ("OnTheRoad", ParcelStatus.IN_TRANSIT),
    ("Start", ParcelStatus.REGISTERED),
)

_unmapped_status_ids_logged: set[str] = set()
_status_unknown_logged: set[str] = set()
_unexpected_keys_logged: set[str] = set()
_delivered_at_format_warned = False
_status_date_format_warned = False
_scan_timestamp_format_warned = False
_weight_units_warned = False
_dimension_units_warned = False
_service_codes_logged: set[Any] = set()
_data_view_status_logged: set[str] = set()
_empty_inbox_warned = False


def _warn_unmapped_status_id(status_id: str) -> None:
    """One-shot-per-value: a ``StatusID`` outside the known table."""
    if status_id in _unmapped_status_ids_logged:
        return
    _unmapped_status_ids_logged.add(status_id)
    _LOGGER.warning(
        "Unrecognised DPD Germany StatusID — help us map it. Open an issue "
        "and paste this line: %s\n  StatusID=%s",
        _NEW_ISSUE_URL,
        status_id,
    )


def _warn_status_unknown(raw_status: str | None) -> None:
    """One-shot-per-value: a parcel that resolved to ``unknown`` end to end."""
    key = repr(raw_status)
    if key in _status_unknown_logged:
        return
    _status_unknown_logged.add(key)
    _LOGGER.warning(
        "DPD Germany parcel could not be classified by any known signal — "
        "help us map it. Open an issue and paste this line: %s\n"
        "  raw_status=%s → reported as 'unknown'",
        _NEW_ISSUE_URL,
        key,
    )


_KNOWN_TOP_LEVEL_KEYS = {
    "ParcelNo",
    "ParcelNicName",
    "ParcelFlowTypeID",
    "DataViewStatus",
    "Delivered",
    "DeliveryDateTime",
    "DeliveryHeadline",
    "LastStatusInfo",
    "StatusInfoContainer",
    "ShipAddress",
    "LabelAddress",
    "OrderInfo",
    "SendParcelData",
    "ReturnData",
    "DeliveryParcelShop",
    "EntryParcelShop",
    "NewDeliveryInfo",
    "NewDeliveryHeadline",
    "LiveTracking",
    "WeightText",
    "AdditionalParcelList",
    "TrackingIconData",
    "VideoGreetingsData",
    "DriverTip",
    "PossibleActions",
    "ParcelMessage",
    "ParcelMessageID",
    "ParcelRating",
    "TrackingAdditionalScanCodes",
    "SystemReturnDepot",
    "isDelayed",
    "isEmptyParcel",
    "isSystemReturn",
    "showWarning",
    "hideDeliveryDetails",
    "NeedCaptcha",
}


def _warn_unexpected_top_level_keys(raw: dict) -> None:
    """One-shot-per-key: a field outside the known set. ``path: type`` only, never values."""
    for key, value in raw.items():
        if key in _KNOWN_TOP_LEVEL_KEYS or key in _unexpected_keys_logged:
            continue
        _unexpected_keys_logged.add(key)
        _LOGGER.warning(
            "DPD Germany response has an unrecognised top-level field — "
            "help us map it. Open an issue and paste this line: %s\n"
            "  %s: %s",
            _NEW_ISSUE_URL,
            key,
            type(value).__name__,
        )


def _describe_format_shape(value: str) -> str:
    """Describe a string's shape (length + separator characters), never its value."""
    separators = sorted({c for c in value if not c.isalnum()})
    return f"length={len(value)} separators={separators!r}"


def _warn_delivered_at_format(value: str) -> None:
    global _delivered_at_format_warned
    if _delivered_at_format_warned:
        return
    _delivered_at_format_warned = True
    _LOGGER.warning(
        "DPD Germany's DeliveryDateTime did not parse as ISO 8601 — help us "
        "learn its real format. Open an issue and paste this line: %s\n"
        "  DeliveryDateTime shape: %s",
        _NEW_ISSUE_URL,
        _describe_format_shape(value),
    )


def _warn_status_date_format(value: str) -> None:
    global _status_date_format_warned
    if _status_date_format_warned:
        return
    _status_date_format_warned = True
    _LOGGER.warning(
        "DPD Germany's StatusDate did not parse as ISO 8601 (a separate "
        "field from DeliveryDateTime). Open an issue and paste this line: "
        "%s\n  StatusDate shape: %s",
        _NEW_ISSUE_URL,
        _describe_format_shape(value),
    )


def _warn_scan_timestamp_format(value: str) -> None:
    global _scan_timestamp_format_warned
    if _scan_timestamp_format_warned:
        return
    _scan_timestamp_format_warned = True
    _LOGGER.warning(
        "DPD Germany's getTrackingScanList ScanDate/ScanTime did not parse "
        "as ISO 8601. Open an issue and paste this line: %s\n"
        "  scan timestamp shape: %s",
        _NEW_ISSUE_URL,
        _describe_format_shape(value),
    )


def _warn_weight_units(raw_weight: Any, raw_weight_text: Any) -> None:
    global _weight_units_warned
    if _weight_units_warned:
        return
    _weight_units_warned = True
    _LOGGER.warning(
        "DPD Germany returned a weight value for the first time — no unit "
        "is declared, so it is not mapped yet. Please tell us what the app "
        "showed for this parcel. Open an issue: %s\n"
        "  OrderInfo.Weight=%r WeightText=%r",
        _NEW_ISSUE_URL,
        raw_weight,
        raw_weight_text,
    )


def _warn_dimension_units(length: Any, width: Any, height: Any) -> None:
    global _dimension_units_warned
    if _dimension_units_warned:
        return
    _dimension_units_warned = True
    _LOGGER.warning(
        "DPD Germany returned dimensions for the first time — no unit is "
        "declared, so they are not mapped yet. Please tell us what the app "
        "showed for this parcel. Open an issue: %s\n"
        "  OrderInfo.Length=%r Width=%r Height=%r",
        _NEW_ISSUE_URL,
        length,
        width,
        height,
    )


def _warn_service_code_once(service_code: Any) -> None:
    """One-shot-per-value: a history ServiceCode — this is how that vocabulary gets built."""
    if service_code in _service_codes_logged:
        return
    _service_codes_logged.add(service_code)
    _LOGGER.warning(
        "DPD Germany history event with a new ServiceCode — help us map it. "
        "Open an issue and paste this line: %s\n  ServiceCode=%r",
        _NEW_ISSUE_URL,
        service_code,
    )


def _warn_data_view_status(value: str) -> None:
    """One-shot-per-value: an inbox entry not ``Owner`` — fields may be suppressed."""
    if value in _data_view_status_logged:
        return
    _data_view_status_logged.add(value)
    _LOGGER.warning(
        "DPD Germany inbox entry has DataViewStatus=%s (expected 'Owner') — "
        "some fields may be suppressed for this parcel. Open an issue: %s",
        value,
        _NEW_ISSUE_URL,
    )


def _warn_empty_inbox_once() -> None:
    """One-shot: the first poll where all three arrays are empty and login succeeded."""
    global _empty_inbox_warned
    if _empty_inbox_warned:
        return
    _empty_inbox_warned = True
    _LOGGER.warning(
        "DPD Germany login succeeded but the account inbox is completely "
        "empty (no incoming, outgoing or returning parcels). If this "
        "account should have parcels, please open an issue: %s",
        _NEW_ISSUE_URL,
    )


def map_parcel_status_de(raw: dict) -> ParcelStatus:
    """Map one DE ``TrackingDataType`` to a canonical status. Stops at the first match.

    Order matters: a returning parcel can also carry ``Delivered: true``
    (delivered back to the sender), so ``ParcelFlowTypeID`` is checked
    before ``Delivered`` — otherwise a return would fire as a delivery.
    """
    if raw.get("ParcelFlowTypeID") == "returning":
        return ParcelStatus.RETURNING
    if raw.get("Delivered") is True:
        return ParcelStatus.DELIVERED

    shop = raw.get("DeliveryParcelShop") or {}
    shop_status = shop.get("ParcelStatus")
    if shop_status in _SHOP_STATUS_MAP:
        return _SHOP_STATUS_MAP[shop_status]

    last_status = raw.get("LastStatusInfo") or {}
    status_id = last_status.get("StatusID")
    if status_id:
        mapped = _STATUS_ID_MAP.get(status_id)
        if mapped is not None:
            return mapped
        _warn_unmapped_status_id(status_id)

    container = raw.get("StatusInfoContainer") or {}
    for slot, status in _CONTAINER_SLOTS:
        slot_info = container.get(slot) or {}
        if isinstance(slot_info, dict) and slot_info.get("StatusReached") in (
            True, "true", "1", 1,
        ):
            return status

    if raw.get("isDelayed") or raw.get("showWarning"):
        return ParcelStatus.PROBLEM

    _warn_status_unknown(_raw_status_text(raw))
    return ParcelStatus.UNKNOWN


# ---------------------------------------------------------------------------
# Payload mapping
# ---------------------------------------------------------------------------


def _address_name(address: dict | None) -> str | None:
    """Best-effort display name from an ``AddressType`` block."""
    if not isinstance(address, dict):
        return None
    company = address.get("Company")
    if company:
        return company
    first = address.get("FirstName") or ""
    last = address.get("LastName") or ""
    combined = f"{first} {last}".strip()
    if combined:
        return combined
    return address.get("Name") or None


def _sender_name(raw: dict) -> str | None:
    """``SendParcelData.PickupAddress`` for outgoing parcels only; ``None`` for incoming."""
    if raw.get("ParcelFlowTypeID") != "sending":
        return None
    send_data = raw.get("SendParcelData") or {}
    return _address_name(send_data.get("PickupAddress"))


def _receiver_name(raw: dict) -> str | None:
    """``ShipAddress``, else ``LabelAddress``, else ``OrderInfo.ReceiverName``."""
    name = _address_name(raw.get("ShipAddress"))
    if name:
        return name
    name = _address_name(raw.get("LabelAddress"))
    if name:
        return name
    order = raw.get("OrderInfo") or {}
    return order.get("ReceiverName") or None


def _raw_status_text(raw: dict) -> str | None:
    """``LastStatusInfo.StatusText_Mobile``, else ``.StatusID``."""
    last_status = raw.get("LastStatusInfo") or {}
    return last_status.get("StatusText_Mobile") or last_status.get("StatusID")


def _parse_de_datetime(value: str | None, warn_fn) -> str | None:
    """Parse a DE date/time string defensively — ``None`` (+ one-shot warn) on failure.

    Format is declared nowhere in the schema; ``fromisoformat`` either
    parses correctly or raises, so a failure always yields ``None`` rather
    than a wrong instant.
    """
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        warn_fn(str(value))
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.isoformat()


def _planned_window(raw: dict) -> tuple[str | None, str | None]:
    """``OrderInfo.EstimatedDeliveryDateTime{From,To}``, gated + redirect-aware."""
    order = raw.get("OrderInfo") or {}
    if not order.get("EstimatedDeliveryDateTimeSpecified"):
        planned_from: str | None = None
        planned_to: str | None = None
    else:
        planned_from = _parse_de_datetime(
            order.get("EstimatedDeliveryDateTimeFrom"), _warn_delivered_at_format
        )
        planned_to = _parse_de_datetime(
            order.get("EstimatedDeliveryDateTimeTo"), _warn_delivered_at_format
        )

    new_delivery = raw.get("NewDeliveryInfo") or {}
    if new_delivery.get("DateChanged") and new_delivery.get("PlannedDeliveryDate"):
        redirected = _parse_de_datetime(
            new_delivery.get("PlannedDeliveryDate"), _warn_delivered_at_format
        )
        if redirected:
            planned_to = redirected
    return planned_from, planned_to


def _pickup(raw: dict) -> tuple[bool, dict | None]:
    """``DeliveryParcelShop.isParcelShopDelivery`` / ``.ParcelShop`` — the full shop object."""
    shop = raw.get("DeliveryParcelShop") or {}
    is_pickup = bool(shop.get("isParcelShopDelivery"))
    pickup_point = shop.get("ParcelShop") if is_pickup else None
    return is_pickup, pickup_point


def _weight_and_dimensions(raw: dict) -> None:
    """Fire the weight/dimension unit warnings — both fields ship ``None`` regardless."""
    order = raw.get("OrderInfo") or {}
    weight = order.get("Weight")
    weight_text = raw.get("WeightText")
    if weight or weight_text:
        _warn_weight_units(weight, weight_text)
    length = order.get("Length")
    width = order.get("Width")
    height = order.get("Height")
    if any((length, width, height)):
        _warn_dimension_units(length, width, height)


def normalize_parcel_de(raw: dict, *, history: list[dict] | None = None) -> dict:
    """Return a carrier-agnostic parcel dict for a DE ``TrackingDataType``.

    ``weight``/``dimensions``/``url`` ship ``None`` — no unit is declared
    for the first two, and no tracking-page URL exists for this surface.
    ``sender`` is ``None`` for every incoming parcel — DE exposes the
    consignee side, not the shipper.
    """
    _warn_unexpected_top_level_keys(raw)

    data_view_status = raw.get("DataViewStatus")
    if data_view_status and data_view_status != "Owner":
        _warn_data_view_status(data_view_status)

    last_status = raw.get("LastStatusInfo") or {}
    status_date = last_status.get("StatusDate")
    if status_date:
        _parse_de_datetime(status_date, _warn_status_date_format)

    _weight_and_dimensions(raw)

    delivered = raw.get("Delivered") is True
    delivered_at = _parse_de_datetime(
        raw.get("DeliveryDateTime"), _warn_delivered_at_format
    )
    planned_from, planned_to = (None, None) if delivered else _planned_window(raw)
    is_pickup, pickup_point = _pickup(raw)

    return {
        "carrier": "DPD",
        "barcode": raw.get("ParcelNo"),
        "sender": _sender_name(raw),
        "receiver": _receiver_name(raw),
        "status": map_parcel_status_de(raw),
        "raw_status": _raw_status_text(raw),
        "delivered": delivered,
        "delivered_at": delivered_at,
        "planned_from": planned_from,
        "planned_to": planned_to,
        "pickup": is_pickup,
        "pickup_point": pickup_point,
        "url": _TRACKING_URL,
        "weight": None,
        "dimensions": None,
        "history": history,
        "raw": raw,
    }


# ---------------------------------------------------------------------------
# Inbox history — getTrackingScanList, opt-in, active parcels only
# ---------------------------------------------------------------------------


def _build_history_de(
    scans: list[dict], *, max_events: int = HISTORY_MAX_EVENTS
) -> list[dict]:
    """Build the canonical ``history`` from ``ArrayOfTrackingScanType``.

    ``TrackingScanType`` has no status id — ``status`` is always
    ``ParcelStatus.UNKNOWN`` here; only ``ServiceCode`` hints at a real
    status and that vocabulary is entirely unmapped, so it is only logged,
    never guessed at from ``StatusText`` prose.
    """
    entries: list[dict] = []
    for scan in scans:
        service_code = scan.get("ServiceCode")
        if service_code is not None:
            _warn_service_code_once(service_code)
        date = scan.get("ScanDate")
        time = scan.get("ScanTime")
        if not date or not time:
            continue
        timestamp = f"{date}T{time}"
        _parse_de_datetime(timestamp, _warn_scan_timestamp_format)
        entries.append(
            {
                "timestamp": timestamp,
                "status": ParcelStatus.UNKNOWN,
                "raw_status": scan.get("StatusText"),
            }
        )
    return entries[-max_events:]


def _delivery_zip_code(raw: dict) -> str | None:
    ship = raw.get("ShipAddress") or {}
    zip_code = ship.get("ZipCode")
    if zip_code:
        return zip_code
    label = raw.get("LabelAddress") or {}
    return label.get("ZipCode") or None


async def _async_fetch_history_de(
    de_session: DpdDeSession, raw: dict
) -> list[dict] | None:
    """``getTrackingScanList`` for one parcel — best-effort, ``None`` on any failure."""
    barcode = raw.get("ParcelNo")
    zip_code = _delivery_zip_code(raw)
    if not barcode or not zip_code:
        return None
    try:
        body = await de_session.async_call(
            "getTrackingScanList",
            {"ParcelNo": barcode, "DeliveryZipCode": zip_code},
        )
    except (DpdApiError, DpdAuthError):
        return None
    scans = as_list(body.get("TrackingScanList"))
    return _build_history_de(scans)


async def async_get_all_parcels_de(
    de_session: DpdDeSession, *, include_history: bool = False
) -> tuple[list[dict], list[dict]]:
    """Fetch DE's account inbox and return ``(normalized_incoming, normalized_outgoing)``.

    One ``getSessionFullState`` call covers all three directions
    (``ReceiveTrackingDataList`` -> incoming; ``SendTrackingDataList`` +
    ``ReturnTrackingDataList`` -> outgoing, mirroring how a return lands in
    the general backend's ``sendingShipments``). History costs one extra
    call per active parcel and only when ``include_history`` is set.
    """
    session_state = await de_session.async_get_parcels()

    receiving = as_list(session_state.get("ReceiveTrackingDataList"))
    sending = as_list(session_state.get("SendTrackingDataList"))
    returning = as_list(session_state.get("ReturnTrackingDataList"))

    if not receiving and not sending and not returning:
        _warn_empty_inbox_once()

    tagged: list[tuple[dict, str]] = [(p, "incoming") for p in receiving]
    tagged += [(p, "outgoing") for p in sending]
    tagged += [(p, "outgoing") for p in returning]

    history_by_barcode: dict[str, list[dict] | None] = {}
    if include_history:
        for raw, _direction in tagged:
            if raw.get("Delivered"):
                continue
            barcode = raw.get("ParcelNo")
            if not barcode or barcode in history_by_barcode:
                continue
            history_by_barcode[barcode] = await _async_fetch_history_de(
                de_session, raw
            )

    incoming = [
        normalize_parcel_de(raw, history=history_by_barcode.get(raw.get("ParcelNo")))
        for raw, direction in tagged
        if direction == "incoming"
    ]
    outgoing = [
        normalize_parcel_de(raw, history=history_by_barcode.get(raw.get("ParcelNo")))
        for raw, direction in tagged
        if direction == "outgoing"
    ]
    return incoming, outgoing


__all__ = [
    "DpdDeSession",
    "async_get_all_parcels_de",
    "map_parcel_status_de",
    "normalize_parcel_de",
]
