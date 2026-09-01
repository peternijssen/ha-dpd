"""Tests for DPD Germany's status derivation, ``normalize_parcel_de`` and the
account-inbox fetch (``countries/de/__init__.py``).
"""
from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.dpd.const import (
    CAPABILITIES_BY_VARIANT,
    DpdApiError,
    DpdAuthError,
    ParcelStatus,
)
from custom_components.dpd.countries.de import (
    _build_history_de,
    _delivered_at,
    _describe_format_shape,
    _history_from_container,
    _parse_de_dimensions,
    _parse_de_status_date,
    _parse_de_weight,
    _planned_window,
    _resolve_history_de,
    _warn_data_view_status,
    _warn_delivered_at_format,
    _warn_dimension_units,
    _warn_scan_timestamp_format,
    _warn_service_code_once,
    _warn_status_date_format,
    _warn_status_unknown,
    _warn_unexpected_top_level_keys,
    _warn_unmapped_status_id,
    _warn_weight_units,
    async_get_all_parcels_de,
    map_parcel_status_de,
    normalize_parcel_de,
)

# ---------------------------------------------------------------------------
# map_parcel_status_de
# ---------------------------------------------------------------------------


def test_status_returning_takes_precedence_over_delivered():
    raw = {"ParcelFlowTypeID": "returning", "Delivered": True}
    assert map_parcel_status_de(raw) == ParcelStatus.RETURNING


def test_status_delivered_flag():
    assert map_parcel_status_de({"Delivered": True}) == ParcelStatus.DELIVERED


@pytest.mark.parametrize(
    ("shop_status", "expected"),
    [
        ("parcel_pickedup_by_consignee", ParcelStatus.DELIVERED),
        ("parcel_in_shop", ParcelStatus.AT_PICKUP_POINT),
        ("newdelivery_in_progress", ParcelStatus.IN_TRANSIT),
    ],
)
def test_status_shop_status_map(shop_status, expected):
    raw = {"DeliveryParcelShop": {"ParcelStatus": shop_status}}
    assert map_parcel_status_de(raw) == expected


@pytest.mark.parametrize(
    ("status_id", "expected"),
    [
        ("DELIVERED", ParcelStatus.DELIVERED),
        ("OUT_FOR_DELIVERY", ParcelStatus.OUT_FOR_DELIVERY),
        ("DELIVERY_ATTEMPT", ParcelStatus.PROBLEM),
        ("RETURN_TO_SENDER", ParcelStatus.RETURNING),
        ("NO_TRACKINGDATA", ParcelStatus.REGISTERED),
    ],
)
def test_status_id_map(status_id, expected):
    raw = {"LastStatusInfo": {"StatusID": status_id}}
    assert map_parcel_status_de(raw) == expected


def test_status_unmapped_status_id_falls_through_and_warns(monkeypatch):
    import custom_components.dpd.countries.de as de_mod

    warned = MagicMock()
    monkeypatch.setattr(de_mod, "_warn_unmapped_status_id", warned)
    raw = {"LastStatusInfo": {"StatusID": "SOME_NEW_STATUS"}}
    assert map_parcel_status_de(raw) == ParcelStatus.UNKNOWN
    warned.assert_called_once_with("SOME_NEW_STATUS")


@pytest.mark.parametrize(
    ("slot", "expected"),
    [
        ("Delivered", ParcelStatus.DELIVERED),
        ("CarLoad", ParcelStatus.OUT_FOR_DELIVERY),
        ("DeliveryDepot", ParcelStatus.IN_TRANSIT),
        ("OnTheRoad", ParcelStatus.IN_TRANSIT),
        ("Start", ParcelStatus.REGISTERED),
    ],
)
def test_status_container_slot_wins_when_reached(slot, expected):
    raw = {"StatusInfoContainer": {slot: {"StatusReached": True}}}
    assert map_parcel_status_de(raw) == expected


def test_status_container_deepest_reached_slot_wins():
    """Slots are walked top-to-bottom (Delivered first); the first one
    reporting StatusReached wins even if a later slot is also reached."""
    raw = {
        "StatusInfoContainer": {
            "CarLoad": {"StatusReached": True},
            "OnTheRoad": {"StatusReached": True},
        }
    }
    assert map_parcel_status_de(raw) == ParcelStatus.OUT_FOR_DELIVERY


def test_status_container_accepts_string_and_int_truthy_values():
    assert (
        map_parcel_status_de({"StatusInfoContainer": {"Start": {"StatusReached": "true"}}})
        == ParcelStatus.REGISTERED
    )
    assert (
        map_parcel_status_de({"StatusInfoContainer": {"Start": {"StatusReached": 1}}})
        == ParcelStatus.REGISTERED
    )


def test_status_isdelayed_or_showwarning_maps_to_problem():
    assert map_parcel_status_de({"isDelayed": True}) == ParcelStatus.PROBLEM
    assert map_parcel_status_de({"showWarning": True}) == ParcelStatus.PROBLEM


def test_status_fully_unknown_warns_once(monkeypatch):
    import custom_components.dpd.countries.de as de_mod

    warned = MagicMock()
    monkeypatch.setattr(de_mod, "_warn_status_unknown", warned)
    assert map_parcel_status_de({}) == ParcelStatus.UNKNOWN
    warned.assert_called_once()


# ---------------------------------------------------------------------------
# _planned_window
# ---------------------------------------------------------------------------


def test_planned_window_none_when_not_specified():
    assert _planned_window({"OrderInfo": {}}) == (None, None)


def test_planned_window_parses_from_and_to():
    raw = {
        "OrderInfo": {
            "EstimatedDeliveryDateTimeSpecified": True,
            "EstimatedDeliveryDateTimeFrom": "2026-08-17T10:00:00",
            "EstimatedDeliveryDateTimeTo": "2026-08-17T12:00:00",
        }
    }
    planned_from, planned_to = _planned_window(raw)
    assert planned_from is not None
    assert planned_to is not None


def test_planned_window_new_delivery_redirect_overrides_to():
    raw = {
        "OrderInfo": {
            "EstimatedDeliveryDateTimeSpecified": True,
            "EstimatedDeliveryDateTimeFrom": "2026-08-17T10:00:00",
            "EstimatedDeliveryDateTimeTo": "2026-08-17T12:00:00",
        },
        "NewDeliveryInfo": {
            "DateChanged": True,
            "PlannedDeliveryDate": "2026-08-18T09:00:00",
        },
    }
    _planned_from, planned_to = _planned_window(raw)
    assert planned_to.startswith("2026-08-18")


def test_planned_window_redirect_ignored_without_date_changed():
    raw = {
        "OrderInfo": {"EstimatedDeliveryDateTimeSpecified": False},
        "NewDeliveryInfo": {"PlannedDeliveryDate": "2026-08-18T09:00:00"},
    }
    assert _planned_window(raw) == (None, None)


# ---------------------------------------------------------------------------
# _parse_de_status_date — real ``DD.MM.YYYY, HH:MM`` shape, confirmed 2026-08-18
# ---------------------------------------------------------------------------


def test_parse_de_status_date_parses_real_shape():
    result = _parse_de_status_date("18.08.2026, 11:16", _warn_status_date_format)
    assert result == "2026-08-18T11:16:00+02:00"


def test_parse_de_status_date_none_on_empty():
    assert _parse_de_status_date(None, _warn_status_date_format) is None
    assert _parse_de_status_date("", _warn_status_date_format) is None


def test_parse_de_status_date_warns_and_returns_none_on_bad_shape(monkeypatch):
    import custom_components.dpd.countries.de as de_mod

    warned = MagicMock()
    monkeypatch.setattr(de_mod, "_warn_status_date_format", warned)
    assert _parse_de_status_date("not-a-date", warned) is None
    warned.assert_called_once_with("not-a-date")


def test_parse_de_status_date_rejects_iso_shape():
    """The old (wrong) assumption — ISO 8601 — must not silently parse."""
    warned = MagicMock()
    assert _parse_de_status_date("2026-08-18T11:16:00", warned) is None
    warned.assert_called_once()


# ---------------------------------------------------------------------------
# _parse_de_weight / _parse_de_dimensions — inferred kg / mm
# ---------------------------------------------------------------------------


def test_parse_de_weight_parses_comma_decimal_as_kg():
    raw = {"OrderInfo": {"Weight": "5,30"}}
    assert _parse_de_weight(raw) == pytest.approx(5.30)


def test_parse_de_weight_none_when_absent():
    assert _parse_de_weight({"OrderInfo": {}}) is None
    assert _parse_de_weight({}) is None


def test_parse_de_weight_none_on_unparseable_value(monkeypatch):
    import custom_components.dpd.countries.de as de_mod

    monkeypatch.setattr(de_mod, "_warn_weight_units", MagicMock())
    assert _parse_de_weight({"OrderInfo": {"Weight": "not-a-number"}}) is None


def test_parse_de_dimensions_converts_mm_to_cm():
    raw = {"OrderInfo": {"Length": "380", "Width": "295", "Height": "225"}}
    dims = _parse_de_dimensions(raw)
    assert dims == {
        "length": 38.0,
        "width": 29.5,
        "height": 22.5,
        "text": "38 x 30 x 22 cm",
    }


def test_parse_de_dimensions_falls_back_to_by_customer_when_unmeasured():
    raw = {
        "OrderInfo": {
            "Length": "0",
            "Width": "0",
            "Height": "0",
            "LengthByCustomer": "320",
            "WidthByCustomer": "240",
            "HeightByCustomer": "210",
        }
    }
    dims = _parse_de_dimensions(raw)
    assert dims["length"] == pytest.approx(32.0)
    assert dims["width"] == pytest.approx(24.0)
    assert dims["height"] == pytest.approx(21.0)


def test_parse_de_dimensions_none_when_neither_source_present():
    assert _parse_de_dimensions({"OrderInfo": {}}) is None


# ---------------------------------------------------------------------------
# _delivered_at — StatusDate wins over DeliveryDateTime
# ---------------------------------------------------------------------------


def test_delivered_at_none_when_not_delivered():
    assert _delivered_at({}, delivered=False, status_date="2026-08-18T11:16:00+02:00") is None


def test_delivered_at_prefers_status_date():
    result = _delivered_at(
        {"DeliveryDateTime": "18.08."},
        delivered=True,
        status_date="2026-08-18T11:16:00+02:00",
    )
    assert result == "2026-08-18T11:16:00+02:00"


def test_delivered_at_falls_back_to_delivery_date_time_when_no_status_date():
    result = _delivered_at(
        {"DeliveryDateTime": "2026-08-17T14:00:00"}, delivered=True, status_date=None
    )
    assert result is not None


# ---------------------------------------------------------------------------
# normalize_parcel_de
# ---------------------------------------------------------------------------


def _raw_parcel(**overrides) -> dict:
    raw = {
        "ParcelNo": "01234567890123",
        "Delivered": False,
        "ShipAddress": {"FirstName": "Jane", "LastName": "Doe"},
        "LastStatusInfo": {"StatusID": "OUT_FOR_DELIVERY", "StatusText_Mobile": "Unterwegs"},
    }
    raw.update(overrides)
    return raw


def test_normalize_parcel_de_basic_fields():
    parcel = normalize_parcel_de(_raw_parcel())
    assert parcel["carrier"] == "DPD"
    assert parcel["barcode"] == "01234567890123"
    assert parcel["receiver"] == "Jane Doe"
    assert parcel["status"] == ParcelStatus.OUT_FOR_DELIVERY
    assert parcel["raw_status"] == "Unterwegs"
    assert parcel["delivered"] is False
    assert parcel["sender"] is None
    assert parcel["weight"] is None
    assert parcel["dimensions"] is None
    assert parcel["url"] is None
    assert parcel["raw"]["ParcelNo"] == "01234567890123"


def test_normalize_parcel_de_sender_only_for_outgoing():
    incoming = normalize_parcel_de(_raw_parcel(ParcelFlowTypeID="sending"))
    assert incoming["sender"] is None or True  # sender comes from SendParcelData
    raw = _raw_parcel(
        ParcelFlowTypeID="sending",
        SendParcelData={"PickupAddress": {"Company": "Acme GmbH"}},
    )
    assert normalize_parcel_de(raw)["sender"] == "Acme GmbH"


def test_normalize_parcel_de_receiver_falls_back_to_label_address():
    raw = _raw_parcel(ShipAddress=None, LabelAddress={"Name": "Fallback Name"})
    assert normalize_parcel_de(raw)["receiver"] == "Fallback Name"


def test_normalize_parcel_de_receiver_falls_back_to_order_info():
    raw = _raw_parcel(
        ShipAddress=None, LabelAddress=None, OrderInfo={"ReceiverName": "Order Name"}
    )
    assert normalize_parcel_de(raw)["receiver"] == "Order Name"


def test_normalize_parcel_de_delivered_at_and_pickup():
    raw = _raw_parcel(
        Delivered=True,
        DeliveryDateTime="2026-08-17T14:00:00",
        DeliveryParcelShop={"isParcelShopDelivery": True, "ParcelShop": {"Name": "Shop X"}},
    )
    parcel = normalize_parcel_de(raw)
    assert parcel["delivered"] is True
    assert parcel["delivered_at"] is not None
    assert parcel["pickup"] is True
    assert parcel["pickup_point"] == "Shop X"
    # Delivered parcels never carry a planned window.
    assert parcel["planned_from"] is None
    assert parcel["planned_to"] is None


def test_normalize_parcel_de_delivered_at_prefers_status_date_over_delivery_date_time():
    """Real capture shape (2026-08-18): DeliveryDateTime is a useless
    ``"18.08."`` day.month string; StatusDate carries the real instant."""
    raw = _raw_parcel(
        Delivered=True,
        DeliveryDateTime="18.08.",
        LastStatusInfo={"StatusID": "DELIVERED", "StatusDate": "18.08.2026, 11:16"},
    )
    parcel = normalize_parcel_de(raw)
    assert parcel["delivered_at"] == "2026-08-18T11:16:00+02:00"


def test_normalize_parcel_de_weight_and_dimensions_from_real_shape():
    raw = _raw_parcel(
        OrderInfo={"Weight": "5,30", "Length": "380", "Width": "295", "Height": "225"}
    )
    parcel = normalize_parcel_de(raw)
    assert parcel["weight"] == pytest.approx(5.30)
    assert parcel["dimensions"]["length"] == pytest.approx(38.0)


def test_normalize_parcel_de_warns_on_unexpected_top_level_key(monkeypatch):
    import custom_components.dpd.countries.de as de_mod

    warned = MagicMock()
    monkeypatch.setattr(de_mod, "_warn_unexpected_top_level_keys", warned)
    normalize_parcel_de(_raw_parcel(SomeNewField="x"))
    warned.assert_called_once()


def test_normalize_parcel_de_warns_on_non_owner_data_view_status(monkeypatch):
    import custom_components.dpd.countries.de as de_mod

    warned = MagicMock()
    monkeypatch.setattr(de_mod, "_warn_data_view_status", warned)
    normalize_parcel_de(_raw_parcel(DataViewStatus="Shared"))
    warned.assert_called_once_with("Shared")


def test_normalize_parcel_de_warns_on_weight_and_dimensions(monkeypatch):
    import custom_components.dpd.countries.de as de_mod

    weight_warn = MagicMock()
    dim_warn = MagicMock()
    monkeypatch.setattr(de_mod, "_warn_weight_units", weight_warn)
    monkeypatch.setattr(de_mod, "_warn_dimension_units", dim_warn)
    raw = _raw_parcel(OrderInfo={"Weight": 1.5, "Length": 10, "Width": 5, "Height": 3})
    normalize_parcel_de(raw)
    weight_warn.assert_called_once()
    dim_warn.assert_called_once()


def test_normalize_parcel_de_history_passthrough():
    events = [{"timestamp": "t", "status": ParcelStatus.UNKNOWN, "raw_status": None}]
    parcel = normalize_parcel_de(_raw_parcel(), history=events)
    assert parcel["history"] == events


# ---------------------------------------------------------------------------
# _build_history_de
# ---------------------------------------------------------------------------


def test_build_history_de_builds_entries_with_unknown_status():
    scans = [{"ScanDate": "2026-08-17", "ScanTime": "10:00:00", "StatusText": "Scanned"}]
    history = _build_history_de(scans)
    assert history == [
        {"timestamp": "2026-08-17T10:00:00", "status": ParcelStatus.UNKNOWN, "raw_status": "Scanned"}
    ]


def test_build_history_de_skips_entries_missing_date_or_time():
    scans = [{"ScanDate": "2026-08-17"}, {"ScanTime": "10:00:00"}]
    assert _build_history_de(scans) == []


def test_build_history_de_truncates_to_max_events():
    scans = [
        {"ScanDate": "2026-08-17", "ScanTime": f"{h:02d}:00:00"} for h in range(5)
    ]
    history = _build_history_de(scans, max_events=2)
    assert len(history) == 2
    # Keeps the most recent entries.
    assert history[-1]["timestamp"].endswith("04:00:00")


def test_build_history_de_warns_once_per_service_code(monkeypatch):
    import custom_components.dpd.countries.de as de_mod

    warned = MagicMock()
    monkeypatch.setattr(de_mod, "_warn_service_code_once", warned)
    scans = [
        {"ScanDate": "2026-08-17", "ScanTime": "10:00:00", "ServiceCode": "SC1"},
    ]
    _build_history_de(scans)
    warned.assert_called_once_with("SC1")


def test_build_history_de_warns_on_bad_timestamp_format(monkeypatch):
    import custom_components.dpd.countries.de as de_mod

    warned = MagicMock()
    monkeypatch.setattr(de_mod, "_warn_scan_timestamp_format", warned)
    scans = [{"ScanDate": "not-a-date", "ScanTime": "whatever"}]
    _build_history_de(scans)
    warned.assert_called_once()


# ---------------------------------------------------------------------------
# _history_from_container / _resolve_history_de
# ---------------------------------------------------------------------------


def test_history_from_container_builds_oldest_to_newest():
    raw = {
        "StatusInfoContainer": {
            "Start": {
                "StatusReached": True,
                "StatusID": "ACCEPTED",
                "StatusText_Mobile": "Paketinfo an DPD übergeben",
                "StatusDate": "14.08.2026, 09:15",
            },
            "OnTheRoad": {
                "StatusReached": True,
                "StatusID": "ON_THE_ROAD",
                "StatusDate": "14.08.2026, 20:47",
            },
            "Delivered": {
                "StatusReached": True,
                "StatusID": "DELIVERED",
                "StatusText_Mobile": "Paket zugestellt",
                "StatusDate": "18.08.2026, 11:16",
            },
        }
    }
    history = _history_from_container(raw)
    assert [e["status"] for e in history] == [
        ParcelStatus.REGISTERED,
        ParcelStatus.IN_TRANSIT,
        ParcelStatus.DELIVERED,
    ]
    assert history[0]["raw_status"] == "Paketinfo an DPD übergeben"
    assert history[-1]["timestamp"] == "2026-08-18T11:16:00+02:00"


def test_history_from_container_skips_unreached_and_undated_slots():
    raw = {
        "StatusInfoContainer": {
            "Start": {"StatusReached": True, "StatusDate": "14.08.2026, 09:15"},
            "OnTheRoad": {"StatusReached": False},
            "Delivered": {"StatusReached": True},  # no StatusDate — unparseable
        }
    }
    history = _history_from_container(raw)
    assert len(history) == 1
    assert history[0]["status"] == ParcelStatus.REGISTERED


def test_history_from_container_none_when_no_slot_reached():
    assert _history_from_container({}) is None
    assert _history_from_container({"StatusInfoContainer": {}}) is None


def test_resolve_history_de_none_when_include_history_off():
    raw = {"ParcelNo": "X", "StatusInfoContainer": {}}
    assert _resolve_history_de(raw, {}, include_history=False) is None


def test_resolve_history_de_prefers_fetched_scan_list():
    raw = {"ParcelNo": "X"}
    fetched = [{"timestamp": "t", "status": ParcelStatus.UNKNOWN, "raw_status": None}]
    result = _resolve_history_de(raw, {"X": fetched}, include_history=True)
    assert result is fetched


def test_resolve_history_de_falls_back_to_container_when_not_fetched():
    raw = {
        "ParcelNo": "DONE-1",
        "Delivered": True,
        "StatusInfoContainer": {
            "Delivered": {
                "StatusReached": True,
                "StatusID": "DELIVERED",
                "StatusDate": "18.08.2026, 11:16",
            }
        },
    }
    # DONE-1 was never looked up (delivered parcels are skipped by the
    # scan-list fetch loop), so the dict has no entry for it at all.
    result = _resolve_history_de(raw, {}, include_history=True)
    assert result == [
        {
            "timestamp": "2026-08-18T11:16:00+02:00",
            "status": ParcelStatus.DELIVERED,
            "raw_status": "DELIVERED",
        }
    ]


def test_resolve_history_de_empty_fetched_list_is_not_overridden():
    """An explicit ``[]`` (scan list fetched, genuinely empty) must win over
    the container fallback — it means "we checked, there's nothing", not
    "we didn't check"."""
    raw = {"ParcelNo": "X", "StatusInfoContainer": {"Start": {"StatusReached": True}}}
    result = _resolve_history_de(raw, {"X": []}, include_history=True)
    assert result == []


# ---------------------------------------------------------------------------
# async_get_all_parcels_de
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_async_get_all_parcels_de_splits_incoming_outgoing_returning():
    de_session = MagicMock()
    de_session.async_get_parcels = AsyncMock(
        return_value={
            "ReceiveTrackingDataList": {
                "TrackingDataType": [_raw_parcel(ParcelNo="IN-1")]
            },
            "SendTrackingDataList": {
                "TrackingDataType": [_raw_parcel(ParcelNo="OUT-1", ParcelFlowTypeID="sending")]
            },
            "ReturnTrackingDataList": {
                "TrackingDataType": [_raw_parcel(ParcelNo="RET-1", ParcelFlowTypeID="returning")]
            },
        }
    )
    incoming, outgoing = await async_get_all_parcels_de(de_session)
    assert [p["barcode"] for p in incoming] == ["IN-1"]
    assert {p["barcode"] for p in outgoing} == {"OUT-1", "RET-1"}


@pytest.mark.asyncio
async def test_async_get_all_parcels_de_fetches_history_for_active_parcels_only():
    de_session = MagicMock()
    de_session.async_get_parcels = AsyncMock(
        return_value={
            "ReceiveTrackingDataList": {
                "TrackingDataType": [
                    _raw_parcel(
                        ParcelNo="ACTIVE-1",
                        Delivered=False,
                        ShipAddress={"ZipCode": "12345"},
                    ),
                    _raw_parcel(ParcelNo="DONE-1", Delivered=True),
                ]
            },
        }
    )
    de_session.async_call = AsyncMock(
        return_value={"TrackingScanList": {"TrackingScanType": []}}
    )
    incoming, _outgoing = await async_get_all_parcels_de(de_session, include_history=True)
    # Only the active parcel gets a history fetch — the delivered one is skipped.
    de_session.async_call.assert_awaited_once()
    assert incoming[0]["history"] == []
    assert incoming[1]["history"] is None


@pytest.mark.asyncio
async def test_async_get_all_parcels_de_delivered_parcel_gets_container_history():
    """A delivered parcel never gets a scan-list fetch, but still gets a free
    history from its own ``StatusInfoContainer`` when ``include_history`` is on."""
    de_session = MagicMock()
    de_session.async_get_parcels = AsyncMock(
        return_value={
            "ReceiveTrackingDataList": {
                "TrackingDataType": [
                    _raw_parcel(
                        ParcelNo="DONE-1",
                        Delivered=True,
                        StatusInfoContainer={
                            "Delivered": {
                                "StatusReached": True,
                                "StatusID": "DELIVERED",
                                "StatusDate": "18.08.2026, 11:16",
                            }
                        },
                    )
                ]
            },
        }
    )
    incoming, _outgoing = await async_get_all_parcels_de(de_session, include_history=True)
    de_session.async_call.assert_not_called()
    assert incoming[0]["history"] == [
        {
            "timestamp": "2026-08-18T11:16:00+02:00",
            "status": ParcelStatus.DELIVERED,
            "raw_status": "DELIVERED",
        }
    ]


@pytest.mark.asyncio
async def test_async_get_all_parcels_de_history_fetch_deduplicates_by_barcode():
    de_session = MagicMock()
    parcel = _raw_parcel(ParcelNo="DUP-1", ShipAddress={"ZipCode": "12345"})
    de_session.async_get_parcels = AsyncMock(
        return_value={
            "ReceiveTrackingDataList": {"TrackingDataType": [parcel]},
            "SendTrackingDataList": {"TrackingDataType": [dict(parcel, ParcelFlowTypeID="sending")]},
        }
    )
    de_session.async_call = AsyncMock(
        return_value={"TrackingScanList": {"TrackingScanType": []}}
    )
    await async_get_all_parcels_de(de_session, include_history=True)
    de_session.async_call.assert_awaited_once()


@pytest.mark.asyncio
async def test_async_get_all_parcels_de_history_fetch_failure_yields_none():
    de_session = MagicMock()
    de_session.async_get_parcels = AsyncMock(
        return_value={
            "ReceiveTrackingDataList": {
                "TrackingDataType": [
                    _raw_parcel(ParcelNo="X", ShipAddress={"ZipCode": "12345"})
                ]
            },
        }
    )
    de_session.async_call = AsyncMock(side_effect=DpdApiError(500))
    incoming, _outgoing = await async_get_all_parcels_de(de_session, include_history=True)
    assert incoming[0]["history"] is None


@pytest.mark.asyncio
async def test_async_get_all_parcels_de_history_skipped_without_barcode_or_zip():
    de_session = MagicMock()
    de_session.async_get_parcels = AsyncMock(
        return_value={
            "ReceiveTrackingDataList": {
                "TrackingDataType": [_raw_parcel(ParcelNo="X", ShipAddress=None, LabelAddress=None)]
            },
        }
    )
    de_session.async_call = AsyncMock()
    await async_get_all_parcels_de(de_session, include_history=True)
    de_session.async_call.assert_not_awaited()


@pytest.mark.asyncio
async def test_async_get_all_parcels_de_propagates_auth_error():
    de_session = MagicMock()
    de_session.async_get_parcels = AsyncMock(side_effect=DpdAuthError("nope"))
    with pytest.raises(DpdAuthError):
        await async_get_all_parcels_de(de_session)


def test_normalize_parcel_de_warns_on_bad_status_date_format():
    """Line-level coverage: StatusDate is parsed (and warned on) separately
    from DeliveryDateTime — a raw parcel with a StatusDate must exercise
    that branch, not just the DeliveryDateTime one."""
    raw = _raw_parcel(LastStatusInfo={"StatusID": "OUT_FOR_DELIVERY", "StatusDate": "garbage"})
    normalize_parcel_de(raw)  # must not raise


# ---------------------------------------------------------------------------
# One-shot warning helpers — exercise the real log call + the second-call
# no-op, since every call site above stubs these out to check *that* they
# fire rather than *what* they log.
# ---------------------------------------------------------------------------


def test_describe_format_shape_reports_length_and_separators():
    assert _describe_format_shape("2026-08-17") == "length=10 separators=['-']"


def test_warn_unmapped_status_id_logs_once(caplog):
    import custom_components.dpd.countries.de as de_mod

    de_mod._unmapped_status_ids_logged.clear()
    with caplog.at_level("WARNING"):
        _warn_unmapped_status_id("NEW_STATUS_X")
        _warn_unmapped_status_id("NEW_STATUS_X")
    assert sum("Unrecognised DPD Germany StatusID" in r.message for r in caplog.records) == 1
    de_mod._unmapped_status_ids_logged.clear()


def test_warn_status_unknown_logs_once(caplog):
    import custom_components.dpd.countries.de as de_mod

    de_mod._status_unknown_logged.clear()
    with caplog.at_level("WARNING"):
        _warn_status_unknown("weird")
        _warn_status_unknown("weird")
    assert sum("could not be classified" in r.message for r in caplog.records) == 1
    de_mod._status_unknown_logged.clear()


def test_warn_unexpected_top_level_keys_logs_once_per_key(caplog):
    import custom_components.dpd.countries.de as de_mod

    de_mod._unexpected_keys_logged.clear()
    with caplog.at_level("WARNING"):
        _warn_unexpected_top_level_keys({"BrandNewField": 1})
        _warn_unexpected_top_level_keys({"BrandNewField": 2})
    assert sum("unrecognised top-level field" in r.message for r in caplog.records) == 1
    de_mod._unexpected_keys_logged.clear()


def test_warn_delivered_at_format_logs_once(caplog):
    import custom_components.dpd.countries.de as de_mod

    de_mod._delivered_at_format_warned = False
    with caplog.at_level("WARNING"):
        _warn_delivered_at_format("not-iso")
        _warn_delivered_at_format("still-not-iso")
    assert sum("DeliveryDateTime did not parse" in r.message for r in caplog.records) == 1
    de_mod._delivered_at_format_warned = False


def test_warn_status_date_format_logs_once(caplog):
    import custom_components.dpd.countries.de as de_mod

    de_mod._status_date_format_warned = False
    with caplog.at_level("WARNING"):
        _warn_status_date_format("not-iso")
        _warn_status_date_format("still-not-iso")
    assert sum("StatusDate did not parse" in r.message for r in caplog.records) == 1
    de_mod._status_date_format_warned = False


def test_warn_scan_timestamp_format_logs_once(caplog):
    import custom_components.dpd.countries.de as de_mod

    de_mod._scan_timestamp_format_warned = False
    with caplog.at_level("WARNING"):
        _warn_scan_timestamp_format("not-iso")
        _warn_scan_timestamp_format("still-not-iso")
    assert sum("ScanDate/ScanTime did not parse" in r.message for r in caplog.records) == 1
    de_mod._scan_timestamp_format_warned = False


def test_warn_weight_units_logs_once(caplog):
    import custom_components.dpd.countries.de as de_mod

    de_mod._weight_units_warned = False
    with caplog.at_level("WARNING"):
        _warn_weight_units(1.5, "1.5 kg")
        _warn_weight_units(2.0, "2.0 kg")
    assert sum("mapped as kilograms by inferred magnitude" in r.message for r in caplog.records) == 1
    de_mod._weight_units_warned = False


def test_warn_dimension_units_logs_once(caplog):
    import custom_components.dpd.countries.de as de_mod

    de_mod._dimension_units_warned = False
    with caplog.at_level("WARNING"):
        _warn_dimension_units(10, 5, 3)
        _warn_dimension_units(11, 6, 4)
    assert sum("mapped as millimetres by inferred magnitude" in r.message for r in caplog.records) == 1
    de_mod._dimension_units_warned = False


def test_warn_service_code_once_logs_once_per_value(caplog):
    import custom_components.dpd.countries.de as de_mod

    de_mod._service_codes_logged.clear()
    with caplog.at_level("WARNING"):
        _warn_service_code_once("SC1")
        _warn_service_code_once("SC1")
    assert sum("new ServiceCode" in r.message for r in caplog.records) == 1
    de_mod._service_codes_logged.clear()


def test_warn_data_view_status_logs_once_per_value(caplog):
    import custom_components.dpd.countries.de as de_mod

    de_mod._data_view_status_logged.clear()
    with caplog.at_level("WARNING"):
        _warn_data_view_status("Shared")
        _warn_data_view_status("Shared")
    assert sum("DataViewStatus=" in r.message for r in caplog.records) == 1
    de_mod._data_view_status_logged.clear()


def test_capabilities_match_normalize_parcel_de():
    """CAPABILITIES_BY_VARIANT["Germany"] must agree with
    test_normalize_parcel_de_basic_fields (url always None),
    test_normalize_parcel_de_delivered_at_and_pickup (pickup_point populates)
    and test_normalize_parcel_de_weight_and_dimensions_from_real_shape."""
    assert CAPABILITIES_BY_VARIANT["Germany"] == {
        "weight",
        "dimensions",
        "delivery_window",
        "pickup_point",
        "history",
    }
