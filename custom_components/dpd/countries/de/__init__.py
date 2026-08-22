"""DPD Germany: derivation-first status map, ``normalize_parcel_de``, warnings.

Built from a static APK teardown; the first real populated account (two
delivered parcels, 2026-08-18) confirmed the status map and the
``StatusDate``/weight/dimension shapes below — see ``carrier-research``'s
``dpd-de.md`` Log for the capture. Everything the capture didn't exercise
(in-transit ``StatusID`` values, non-DE-kg weight, an unmeasured parcel with
no ``…ByCustomer`` fallback either) is still derivation-first: the
``StatusID`` string vocabulary has no closed enum, but the structural fields
(``Delivered``, ``ParcelFlowTypeID``, ``DeliveryParcelShop.ParcelStatus``,
``StatusInfoContainer``) can be trusted. The WARNING helpers here are how the
vocabulary gets completed as more real accounts are polled.

The SOAP transport, ``KeyPhase`` signing and session lifecycle live in
``.session`` — this module never touches the wire itself.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

from ...const import HISTORY_MAX_EVENTS, ParcelStatus
from ..general import _augment_dimensions
from .session import DpdApiError, DpdAuthError, DpdDeSession, as_list

_LOGGER = logging.getLogger(__name__)

# StatusDate/ScanDate+Time are Germany-local, confirmed 2026-08-18 by a real
# capture — DPD DE has no other market, so Europe/Berlin is a safe constant
# rather than a per-parcel derivation.
_DE_TZ = ZoneInfo("Europe/Berlin")

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
    """One-shot: no unit is declared for ``OrderInfo.Weight`` in the schema.

    Mapped as kilograms by inferred magnitude (comma-decimal values like
    ``5,30``/``7,06`` from the 2026-08-18 capture — matching the general/NL
    path's native kg unit for the same carrier), not an API-declared unit.
    """
    global _weight_units_warned
    if _weight_units_warned:
        return
    _weight_units_warned = True
    _LOGGER.warning(
        "DPD Germany returned a weight value — mapped as kilograms by "
        "inferred magnitude, not an API-declared unit. Please confirm this "
        "matches what the app showed for this parcel. Open an issue: %s\n"
        "  OrderInfo.Weight=%r WeightText=%r",
        _NEW_ISSUE_URL,
        raw_weight,
        raw_weight_text,
    )


def _warn_dimension_units(length: Any, width: Any, height: Any) -> None:
    """One-shot: no unit is declared for ``OrderInfo.Length/Width/Height``.

    Mapped as millimetres by inferred magnitude (the 2026-08-18 capture's
    ``380/295/225`` reads as a 38x29.5x22.5cm parcel, not 3.8m) — unlike the
    general/NL path, which is already centimetres.
    """
    global _dimension_units_warned
    if _dimension_units_warned:
        return
    _dimension_units_warned = True
    _LOGGER.warning(
        "DPD Germany returned dimensions — mapped as millimetres by "
        "inferred magnitude, not an API-declared unit. Please confirm this "
        "matches what the app showed for this parcel. Open an issue: %s\n"
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


def _parse_de_status_date(value: str | None, warn_fn) -> str | None:
    """Parse a ``StatusDate``-shaped string — ``DD.MM.YYYY, HH:MM``, Europe/Berlin local.

    Confirmed 2026-08-18 by the first real captured parcels: this shape is
    *not* ISO 8601 the way ``EstimatedDeliveryDateTime{From,To}`` is, so
    :func:`_parse_de_datetime` always failed on it. Used for
    ``LastStatusInfo.StatusDate`` and each ``StatusInfoContainer`` slot's own
    ``StatusDate`` — both confirmed the same shape in that capture.
    """
    if not value:
        return None
    try:
        dt = datetime.strptime(str(value), "%d.%m.%Y, %H:%M")
    except ValueError:
        warn_fn(str(value))
        return None
    return dt.replace(tzinfo=_DE_TZ).isoformat()


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


def _pickup(raw: dict) -> tuple[bool, str | None]:
    """``DeliveryParcelShop.isParcelShopDelivery`` / ``.ParcelShop.Name`` — a display name, matching the general path's string shape."""
    shop = raw.get("DeliveryParcelShop") or {}
    is_pickup = bool(shop.get("isParcelShopDelivery"))
    pickup_point = _address_name(shop.get("ParcelShop")) if is_pickup else None
    return is_pickup, pickup_point


def _parse_de_weight(raw: dict) -> float | None:
    """``OrderInfo.Weight`` — comma-decimal, mapped as kilograms (inferred, see warning)."""
    order = raw.get("OrderInfo") or {}
    weight = order.get("Weight")
    weight_text = raw.get("WeightText")
    if not weight and not weight_text:
        return None
    _warn_weight_units(weight, weight_text)
    return _to_float(weight)


def _to_float(value: Any) -> float | None:
    """Best-effort numeric parse — comma or dot decimal, ``None`` on failure."""
    if value is None:
        return None
    try:
        return float(str(value).replace(",", "."))
    except ValueError:
        return None


def _parse_de_dimensions(raw: dict) -> dict | None:
    """``OrderInfo.Length/Width/Height``, mm, with a ``…ByCustomer`` fallback.

    The 2026-08-18 capture had one parcel with DPD's own measurement
    populated and ``…ByCustomer`` zeroed, and a second the other way round
    (DPD hadn't measured it, only the sender's declared size was present) —
    so both sources are real, not redundant. Values are parsed to ``float``
    *before* the zero-check — DPD ships the unmeasured triple as the string
    ``"0"``, which is truthy, so a bare presence check would never fall
    through to ``…ByCustomer``. Converted to the canonical centimetres
    (millimetres here, unlike the general/NL path's native cm — see the
    warning below).
    """
    order = raw.get("OrderInfo") or {}
    length = _to_float(order.get("Length"))
    width = _to_float(order.get("Width"))
    height = _to_float(order.get("Height"))
    if not any((length, width, height)):
        length = _to_float(order.get("LengthByCustomer"))
        width = _to_float(order.get("WidthByCustomer"))
        height = _to_float(order.get("HeightByCustomer"))
    if not any((length, width, height)):
        return None
    _warn_dimension_units(length, width, height)
    return _augment_dimensions(
        {
            "length": (length or 0.0) / 10,
            "width": (width or 0.0) / 10,
            "height": (height or 0.0) / 10,
        }
    )


def _delivered_at(raw: dict, *, delivered: bool, status_date: str | None) -> str | None:
    """Return the real delivery timestamp — ``LastStatusInfo.StatusDate``, not ``DeliveryDateTime``.

    ``DeliveryDateTime`` (``"18.08."`` in the 2026-08-18 capture — day.month
    only, no year, no time) never carries a usable instant; kept as a
    fallback in case some account ever returns something ISO-shaped.
    """
    if not delivered:
        return None
    return status_date or _parse_de_datetime(
        raw.get("DeliveryDateTime"), _warn_delivered_at_format
    )


def normalize_parcel_de(raw: dict, *, history: list[dict] | None = None) -> dict:
    """Return a carrier-agnostic parcel dict for a DE ``TrackingDataType``.

    ``url`` ships ``None`` — no tracking-page URL exists for this surface.
    ``sender`` is ``None`` for every incoming parcel — DE exposes the
    consignee side, not the shipper. ``weight``/``dimensions`` are
    best-effort: DPD DE declares no unit for either field, so the mapping is
    an inferred-magnitude guess (see the one-shot warnings), not a confirmed
    API contract.
    """
    _warn_unexpected_top_level_keys(raw)

    data_view_status = raw.get("DataViewStatus")
    if data_view_status and data_view_status != "Owner":
        _warn_data_view_status(data_view_status)

    last_status = raw.get("LastStatusInfo") or {}
    status_date = _parse_de_status_date(
        last_status.get("StatusDate"), _warn_status_date_format
    )

    delivered = raw.get("Delivered") is True
    delivered_at = _delivered_at(raw, delivered=delivered, status_date=status_date)
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
        "weight": _parse_de_weight(raw),
        "dimensions": _parse_de_dimensions(raw),
        "history": history,
        "raw": raw,
    }


# ---------------------------------------------------------------------------
# Inbox history — getTrackingScanList (opt-in, active parcels only) with a
# StatusInfoContainer-derived fallback (free — same response as status)
# ---------------------------------------------------------------------------


def _history_from_container(raw: dict) -> list[dict] | None:
    """Build ``history`` from the inbox's own ``StatusInfoContainer`` — no extra call.

    Five dated stages already present in the same ``getSessionFullState``
    response used for status derivation (confirmed shape 2026-08-18: each
    reached slot carries its own ``StatusID``/``StatusText_Mobile``/
    ``StatusDate``) — unlike ``getTrackingScanList``'s events, coarser
    (5 stages, not every scan) but with a real mapped ``status`` and zero
    marginal cost. Used only as a fallback when the richer scan-list history
    wasn't fetched (see ``_resolve_history_de``) — a real fetch always wins.
    """
    container = raw.get("StatusInfoContainer") or {}
    entries: list[dict] = []
    for slot, status in reversed(_CONTAINER_SLOTS):
        slot_info = container.get(slot)
        if not isinstance(slot_info, dict):
            continue
        if slot_info.get("StatusReached") not in (True, "true", "1", 1):
            continue
        timestamp = _parse_de_status_date(
            slot_info.get("StatusDate"), _warn_status_date_format
        )
        if not timestamp:
            continue
        entries.append(
            {
                "timestamp": timestamp,
                "status": status,
                "raw_status": slot_info.get("StatusText_Mobile") or slot_info.get("StatusID"),
            }
        )
    return entries or None


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


def _resolve_history_de(
    raw: dict, history_by_barcode: dict[str, list[dict] | None], *, include_history: bool
) -> list[dict] | None:
    """Return the scan-list fetch when one was made, else the free container fallback.

    ``None`` outright when ``include_history`` is off — the container
    fallback is free, but the option still gates whether ``history`` is
    exposed at all, matching every other carrier's opt-in contract.
    """
    if not include_history:
        return None
    fetched = history_by_barcode.get(raw.get("ParcelNo"))
    if fetched is not None:
        return fetched
    return _history_from_container(raw)


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
        normalize_parcel_de(
            raw,
            history=_resolve_history_de(
                raw, history_by_barcode, include_history=include_history
            ),
        )
        for raw, direction in tagged
        if direction == "incoming"
    ]
    outgoing = [
        normalize_parcel_de(
            raw,
            history=_resolve_history_de(
                raw, history_by_barcode, include_history=include_history
            ),
        )
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
