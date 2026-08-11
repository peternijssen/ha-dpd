"""Pure parcel mapping, normalization and list helpers for DPD.

No I/O and no Home Assistant objects beyond the config entry's options: this is
the carrier-specific status mapping and canonical-shape logic, kept apart from
the coordinator (fetching, caching, events) so it stays trivially unit-testable.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from homeassistant.config_entries import ConfigEntry

from .const import (
    CONF_DELIVERED_FILTER_AMOUNT,
    CONF_DELIVERED_FILTER_TYPE,
    DEFAULT_BU,
    DEFAULT_DELIVERED_FILTER_AMOUNT,
    DEFAULT_DELIVERED_FILTER_TYPE,
    DELIVERED_DESCRIPTION,
    HISTORY_MAX_EVENTS,
    KNOWN_DESCRIPTIONS,
    STATUS_AT_DELIVERY_CENTER,
    STATUS_AVAILABLE_FOR_COLLECTION,
    STATUS_DELIVERED,
    STATUS_IN_TRANSIT,
    STATUS_ORDER_CREATED,
    STATUS_PARCEL_HANDED,
    STATUS_PARCEL_OUT_FOR_DELIVERY,
    STATUS_RETURN_TO_SENDER,
    STATUS_UNSUCCESSFUL_DELIVERY,
    ParcelStatus,
)

_LOGGER = logging.getLogger(__name__)


def _augment_dimensions(dims: dict | None) -> dict | None:
    """Return a copy of ``dims`` with a ``text`` field added (``L x W x H cm``).

    Suite-wide format: integer values, lowercase ``x`` separator, length
    first per the L × W × H shipping convention. ``text`` is ``None`` when
    any of the three required fields is missing so callers can still rely
    on the key being present.
    """
    if not dims:
        return None
    length = dims.get("length")
    width = dims.get("width")
    height = dims.get("height")
    if length is None or width is None or height is None:
        text: str | None = None
    else:
        text = f"{int(round(length))} x {int(round(width))} x {int(round(height))} cm"
    return {**dims, "text": text}

# DPD status.description → canonical ParcelStatus. Every value in
# KNOWN_DESCRIPTIONS is mapped here; anything else falls back to
# ParcelStatus.UNKNOWN and triggers a one-shot info log via
# log_unknown_descriptions.
_DESCRIPTION_MAP: dict[str, ParcelStatus] = {
    STATUS_ORDER_CREATED: ParcelStatus.REGISTERED,
    STATUS_PARCEL_HANDED: ParcelStatus.IN_TRANSIT,
    STATUS_IN_TRANSIT: ParcelStatus.IN_TRANSIT,
    STATUS_AT_DELIVERY_CENTER: ParcelStatus.IN_TRANSIT,
    STATUS_PARCEL_OUT_FOR_DELIVERY: ParcelStatus.OUT_FOR_DELIVERY,
    STATUS_AVAILABLE_FOR_COLLECTION: ParcelStatus.AT_PICKUP_POINT,
    STATUS_UNSUCCESSFUL_DELIVERY: ParcelStatus.IN_TRANSIT,
    STATUS_RETURN_TO_SENDER: ParcelStatus.RETURNING,
    STATUS_DELIVERED: ParcelStatus.DELIVERED,
}

# DPD parcelEvents ``eventType`` → canonical ParcelStatus (history timeline).
# History maps from the stable ``eventType`` code, not the ``eventTypeText``
# (which is ``lang``-dependent and brittle). Unmapped codes resolve to
# ``None`` (+ a one-shot warning, see feature B).
#
# Codes are DPD "Geo Event codes" from their GSMT matrix (2025-02-24); the
# full 68-code reference lives in ``docs/api/parcels.md``. We deliberately map
# only the subset a consumer parcel realistically emits (the ``CC*`` customs,
# ``PK*``/``CR*`` sender-side, ``MT*``/``QR*``/``MI*`` contact codes are left
# unmapped on purpose — feature B will surface any that actually appear).
_EVENT_TYPE_MAP: dict[str, ParcelStatus] = {
    # --- Data / registration ---
    "ENA": ParcelStatus.REGISTERED,        # Data received and integrated
    # --- In the network (origin, hub, sorting, destination depot) ---
    "ORI": ParcelStatus.IN_TRANSIT,        # Origin depot – Inbound
    "ORW": ParcelStatus.IN_TRANSIT,        # Origin depot – Held
    "HUI": ParcelStatus.IN_TRANSIT,        # Hub – Inbound
    "HUS": ParcelStatus.IN_TRANSIT,        # Hub – Sorted
    "HUW": ParcelStatus.IN_TRANSIT,        # Hub – Held
    "HUZ": ParcelStatus.IN_TRANSIT,        # Hub – Scan
    "SPE": ParcelStatus.IN_TRANSIT,        # Status parcel – Information (benign)
    "SPL": ParcelStatus.IN_TRANSIT,        # Status parcel – Loaded
    "SPS": ParcelStatus.IN_TRANSIT,        # Status parcel – Sorted
    "SPV": ParcelStatus.IN_TRANSIT,        # Status parcel – Control
    "SPW": ParcelStatus.IN_TRANSIT,        # Status parcel – Held (benign)
    "SPZ": ParcelStatus.IN_TRANSIT,        # Status parcel – Scan
    "DLI": ParcelStatus.IN_TRANSIT,        # Destination depot – Inbound
    "DLS": ParcelStatus.IN_TRANSIT,        # Destination depot – Sorted
    "DLW": ParcelStatus.IN_TRANSIT,        # Destination depot – Held
    "DLZ": ParcelStatus.IN_TRANSIT,        # Destination depot – Scan
    "DLR": ParcelStatus.IN_TRANSIT,        # Destination depot – Driver's return (failed attempt, back to depot)
    "MSDLO": ParcelStatus.IN_TRANSIT,      # Message Notification — a "delivery coming" heads-up, not the physical out-for-delivery scan (that's DLO)
    # --- Out for delivery ---
    "DLO": ParcelStatus.OUT_FOR_DELIVERY,  # Destination depot – Out for delivery
    # --- Parcelshop / PUDO flow (not yet confirmed in consumer parcelEvents) ---
    "DEHD": ParcelStatus.IN_TRANSIT,       # Handover by the driver to the PUDO (arriving)
    "DEHDY": ParcelStatus.IN_TRANSIT,      # Proof of handover by the driver to the PUDO
    "DOMSDLO": ParcelStatus.IN_TRANSIT,    # PUDO – Notification sent: available in PUDO
    "DOPKY": ParcelStatus.IN_TRANSIT,      # PUDO – Drop Off (sender drop-off)
    "DODEI": ParcelStatus.AT_PICKUP_POINT, # PUDO – Received and available for consignee collection
    # --- Delivered / collected ---
    "DEY": ParcelStatus.DELIVERED,         # Delivery – Delivered
    "DEYY": ParcelStatus.DELIVERED,        # Delivery – Proof of delivery
    "DODEY": ParcelStatus.DELIVERED,       # PUDO – Collected by the consignee
    "DODEYY": ParcelStatus.DELIVERED,      # PUDO – Proof of delivery, signature in PUDO
    # --- Returning to sender ---
    "SPR": ParcelStatus.RETURNING,         # Status parcel – Return
    "DEN": ParcelStatus.RETURNING,         # Delivery – Refusal
    "DODEN": ParcelStatus.RETURNING,       # PUDO – Not collected by the consignee
    "DODEH": ParcelStatus.RETURNING,       # PUDO – Handed back from PUDO to the driver
    # --- Exceptions / anomalies (problem) ---
    "ENX": ParcelStatus.PROBLEM,           # Data exchange – Exception
    "ORX": ParcelStatus.PROBLEM,           # Origin depot – Exception
    "HUX": ParcelStatus.PROBLEM,           # Hub – Exception
    "SPX": ParcelStatus.PROBLEM,           # Status parcel – Exception
    "DLX": ParcelStatus.PROBLEM,           # Destination depot – Exception
    "DEX": ParcelStatus.PROBLEM,           # Delivery – Delivery Exception
    "DODEX": ParcelStatus.PROBLEM,         # PUDO – Collection anomaly
}

# New-issue link surfaced in the unknown-status warnings so users can paste a
# ready-made line into a bug report.
# Points at the pre-filled issue template rather than a blank form, so a
# user following this link from their log lands somewhere that already
# asks the right questions.
_NEW_ISSUE_URL = (
    "https://github.com/ha-parcel-integrations/ha-dpd/issues/new"
    "?template=unrecognised_status.yml"
)

# Values we have already warned about once in this HA session, so repeated
# polls do not flood the log with the same "new status" message.
_unknown_descriptions_logged: set[str] = set()
_unknown_event_types_logged: set[str] = set()


def _description(shipment: dict) -> str | None:
    return (shipment.get("status") or {}).get("description")


def map_parcel_status(parcel: dict) -> ParcelStatus:
    """Map a raw DPD parcel to a canonical :class:`ParcelStatus`.

    Reads ``status.description`` and looks it up in ``_DESCRIPTION_MAP``;
    unknown values (or a missing status field) fall back to
    ``ParcelStatus.UNKNOWN``. New raw values are surfaced separately via
    :func:`log_unknown_descriptions` so the map can be extended.
    """
    description = _description(parcel)
    return _DESCRIPTION_MAP.get(description or "", ParcelStatus.UNKNOWN)


def log_unknown_descriptions(shipments: list[dict]) -> None:
    """Warn about any status.description we have not mapped yet, once per value.

    Lets us extend ``_DESCRIPTION_MAP`` as new lifecycle stages surface
    in real accounts without spamming the log on every poll. Anything
    not in the map is reported as ``ParcelStatus.UNKNOWN`` until it is.
    The message carries a copy-paste issue link (feature B).
    """
    for shipment in shipments:
        description = _description(shipment)
        if (
            description
            and description not in KNOWN_DESCRIPTIONS
            and description not in _unknown_descriptions_logged
        ):
            _unknown_descriptions_logged.add(description)
            code = (shipment.get("status") or {}).get("status")
            _LOGGER.warning(
                "Unrecognised DPD status — help us map it. Open an issue and "
                "paste this line: %s\n  [parcel] status.description=%s (code %s) "
                "→ reported as 'unknown'",
                _NEW_ISSUE_URL,
                description,
                code,
            )


def map_event_status(
    event_type: str | None, event_type_text: str | None = None
) -> ParcelStatus | None:
    """Map a DPD parcelEvents ``eventType`` to a canonical status.

    Returns ``None`` for an unmapped (or absent) code — history entries keep
    ``status: null`` rather than guessing — and surfaces a one-shot warning
    with copy-paste issue text so users can help extend the map.
    """
    if not event_type:
        return None
    status = _EVENT_TYPE_MAP.get(event_type)
    if status is not None:
        return status

    if event_type not in _unknown_event_types_logged:
        _unknown_event_types_logged.add(event_type)
        _LOGGER.warning(
            "Unrecognised DPD status — help us map it. Open an issue and "
            "paste this line: %s\n  [history] eventType=%s text=%r "
            "→ reported as 'unknown'",
            _NEW_ISSUE_URL,
            event_type,
            event_type_text,
        )
    return None


def _parse_iso(value: str | None) -> datetime | None:
    """Parse an ISO 8601 string to a datetime, or ``None`` on failure.

    DPD event timestamps are naive (no offset); a naive value is treated as
    UTC so a list always sorts without crashing on a mixed set.
    """
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def build_history(
    events: list[dict] | None, *, max_events: int = HISTORY_MAX_EVENTS
) -> list[dict]:
    """Build the canonical ``history`` list from DPD ``parcelEvents``.

    Each entry is ``{timestamp, status, raw_status}`` — identical across all
    suite carriers. ``timestamp`` combines the event ``date`` + ``time``;
    ``status`` maps from the stable ``eventType``; ``raw_status`` is DPD's
    own ``eventTypeText``. Sorted oldest → newest (unparseable timestamps keep
    their original order, after the parseable ones) and capped to the most
    recent ``max_events``.
    """
    parseable: list[tuple[datetime, dict]] = []
    unparseable: list[dict] = []
    for event in events or []:
        date = event.get("date")
        time = event.get("time")
        if not date or not time:
            continue
        timestamp = f"{date}T{time}"
        text = event.get("eventTypeText")
        entry = {
            "timestamp": timestamp,
            "status": map_event_status(event.get("eventType"), text),
            "raw_status": text,
        }
        dt = _parse_iso(timestamp)
        if dt is None:
            unparseable.append(entry)
        else:
            parseable.append((dt, entry))
    parseable.sort(key=lambda item: item[0])
    ordered = [entry for _, entry in parseable] + unparseable
    return ordered[-max_events:]


def filter_active_shipments(shipments: list[dict]) -> list[dict]:
    """Return shipments that have not yet reached the DELIVERED state."""
    return [s for s in shipments if _description(s) != DELIVERED_DESCRIPTION]


def filter_delivered_shipments(shipments: list[dict]) -> list[dict]:
    """Return shipments in the DELIVERED state."""
    return [s for s in shipments if _description(s) == DELIVERED_DESCRIPTION]


def _tracking_url(parcel: dict, bu: str = DEFAULT_BU) -> str | None:
    """Build the DPD tracking URL for a parcel, or ``None`` when no parcelNumber.

    The country segment is derived from the business unit (``DPD-DE`` ->
    ``de``) rather than hardcoded, so it follows whichever BU the account
    was set up with.
    """
    parcel_number = parcel.get("parcelNumber")
    if not parcel_number:
        return None
    country = bu.removeprefix("DPD-").lower()
    return (
        f"https://www.dpdgroup.com/{country}/mydpd/my-parcels/search?"
        f"parcelNumber={parcel_number}"
    )


def normalize_parcel(
    parcel: dict,
    *,
    receiver: str | None = None,
    weight: float | None = None,
    dimensions: dict | None = None,
    history: list[dict] | None = None,
    bu: str = DEFAULT_BU,
) -> dict:
    """Return a carrier-agnostic parcel dict with the DPD payload under ``raw``.

    Mirrors the shape every other carrier integration (DHL, PostNL)
    publishes, so the parcel aggregator and cross-carrier dashboards
    can read parcels the same way regardless of source. The original
    DPD shipment object stays available under ``raw``.

    ``planned_from`` / ``planned_to`` are derived from
    :func:`shipment_planned_window` (FMP window first, then the top-level
    ``deliveryTime{From,To}`` pair, finally the all-day fallback), and
    cleared for delivered parcels where ``delivered_at`` carries the
    actual moment instead. The raw DPD payload is passed through under
    ``raw`` without modification.

    ``receiver``, ``weight`` and ``dimensions`` come from the per-parcel
    detail endpoint — the list endpoint doesn't carry them, so the
    coordinator fetches them lazily and passes them in. DPD's native
    units (kg + cm) already match the canonical contract, so no
    conversion is needed here.

    ``history`` is the optional per-parcel status timeline (opt-in option,
    default off → ``None``). It is also detail-endpoint sourced and stays
    top-level so it survives the aggregator's ``strip_raw()``.

    ``bu`` is the account's business unit (e.g. ``DPD-DE``), used only to
    build the country segment of the tracking URL.
    """
    description = _description(parcel)
    delivered = description == DELIVERED_DESCRIPTION
    delivered_at: str | None = None
    if delivered:
        dt = shipment_delivery_dt(parcel)
        delivered_at = dt.isoformat() if dt else None
    planned_from: str | None = None
    planned_to: str | None = None
    if not delivered:
        start, end = shipment_planned_window(parcel)
        planned_from = start.isoformat() if start else None
        planned_to = end.isoformat() if end else None
    is_pickup = (parcel.get("status") or {}).get("deliveryType") == "PARCELSHOP"
    return {
        "carrier": "DPD",
        "barcode": parcel.get("parcelNumber"),
        "sender": parcel.get("senderName"),
        "receiver": receiver,
        "status": map_parcel_status(parcel),
        "raw_status": description,
        "delivered": delivered,
        "delivered_at": delivered_at,
        "planned_from": planned_from,
        "planned_to": planned_to,
        "pickup": is_pickup,
        "pickup_point": None,
        "url": _tracking_url(parcel, bu),
        "weight": weight,
        "dimensions": dimensions,
        "history": history,
        "raw": parcel,
    }


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


def shipment_planned_window(shipment: dict) -> tuple[datetime | None, datetime | None]:
    """Return the planned ``(from, to)`` delivery window for a shipment.

    Resolution order:

    1. The nested Follow My Parcel block (``fmpDeliveryDateAndTime``),
       which gives a precise hour range like ``(10:34, 11:34)`` on the
       day of delivery.
    2. The top-level ``deliveryTimeFrom`` / ``deliveryTimeTo`` pair
       (combined with ``deliveryDate``), which DPD attaches once a
       parcel is out for delivery.
    3. A whole-day window in the parcel's local timezone, when only
       ``deliveryDate`` is known.

    Returns ``(None, None)`` when even the date is missing or
    unparseable.
    """
    tz_id = (shipment.get("status") or {}).get("eventDateAndTimeZoneId")
    tz: timezone | ZoneInfo = timezone.utc
    if tz_id:
        try:
            tz = ZoneInfo(tz_id)
        except Exception:  # noqa: BLE001 - bad tz string from API
            tz = timezone.utc

    fmp = shipment.get("fmpDeliveryDateAndTime") or {}
    fmp_date = fmp.get("deliveryDate")
    time_range = fmp.get("timeRange") or {}
    fmp_from = time_range.get("from")
    fmp_to = time_range.get("to")
    if fmp_date and fmp_from and fmp_to:
        try:
            return (
                datetime.fromisoformat(f"{fmp_date}T{fmp_from}").replace(tzinfo=tz),
                datetime.fromisoformat(f"{fmp_date}T{fmp_to}").replace(tzinfo=tz),
            )
        except ValueError:
            pass

    date_str = shipment.get("deliveryDate")
    if not date_str:
        return (None, None)

    top_from = shipment.get("deliveryTimeFrom")
    top_to = shipment.get("deliveryTimeTo")
    if top_from and top_to:
        try:
            return (
                datetime.fromisoformat(f"{date_str}T{top_from}").replace(tzinfo=tz),
                datetime.fromisoformat(f"{date_str}T{top_to}").replace(tzinfo=tz),
            )
        except ValueError:
            pass

    try:
        d = datetime.fromisoformat(date_str)
    except ValueError:
        return (None, None)
    start = d.replace(tzinfo=tz)
    end = d.replace(hour=23, minute=59, second=59, tzinfo=tz)
    return (start, end)


def shipment_planned_dt(shipment: dict) -> datetime | None:
    """Return the start of the planned delivery window, or ``None``.

    Thin wrapper around :func:`shipment_planned_window` that keeps the
    "start time" semantics callers (e.g. the next-delivery sensor) rely
    on for sorting.
    """
    return shipment_planned_window(shipment)[0]


def fmp_hashcode(shipment: dict) -> str | None:
    """Pluck the Follow My Parcel hashcode off a shipment, when present.

    DPD lists ``availableActions.FOLLOW_MY_PARCEL`` as an array with at
    most one entry; the hashcode is what the FMP authenticate endpoint
    expects as credentials. Returns ``None`` for shipments that have not
    yet been scheduled into the FMP system (typically anything before
    the day of delivery).
    """
    actions = shipment.get("availableActions") or {}
    fmp_actions = actions.get("FOLLOW_MY_PARCEL") or []
    if not fmp_actions:
        return None
    hashcode = fmp_actions[0].get("hashcode")
    return hashcode if isinstance(hashcode, str) and hashcode else None


def shipment_delivery_dt(shipment: dict) -> datetime | None:
    """Return the delivery datetime of a shipment, or ``None`` if unknown.

    Prefers ``status.eventDateAndTime`` (naive ISO 8601) combined with
    ``status.eventDateAndTimeZoneId`` (IANA timezone). Falls back to the
    plain ``deliveryDate`` (date) at start-of-day UTC.
    """
    status = shipment.get("status") or {}
    moment = status.get("eventDateAndTime")
    if moment:
        try:
            dt = datetime.fromisoformat(moment.replace("Z", "+00:00"))
        except ValueError:
            dt = None
        if dt is not None:
            if dt.tzinfo is None:
                tz_id = status.get("eventDateAndTimeZoneId")
                tz: timezone | ZoneInfo = timezone.utc
                if tz_id:
                    try:
                        tz = ZoneInfo(tz_id)
                    except Exception:  # noqa: BLE001 - bad tz string from API
                        tz = timezone.utc
                dt = dt.replace(tzinfo=tz)
            return dt

    date_str = shipment.get("deliveryDate")
    if not date_str:
        return None
    try:
        d = datetime.fromisoformat(date_str)
    except ValueError:
        return None
    return d.replace(tzinfo=timezone.utc)


def _apply_delivered_filter(shipments: list[dict], entry: ConfigEntry) -> list[dict]:
    """Trim the delivered list according to the configured options."""
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
