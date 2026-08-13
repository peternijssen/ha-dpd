"""Tests for the DPD coordinator filter functions, data shape and error handling."""
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.dpd.api import DpdApiError, DpdAuthError
from custom_components.dpd.const import (
    CAPABILITIES,
    CONF_DELIVERED_FILTER_AMOUNT,
    CONF_DELIVERED_FILTER_TYPE,
    CONF_INCLUDE_HISTORY,
    KNOWN_CAPABILITIES,
    ParcelStatus,
)
from custom_components.dpd.coordinator import (
    DpdCoordinator,
    _refresh_interval,
)
from custom_components.dpd.parcels import (
    _augment_dimensions,
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
    sort_parcels_by_ts,
)

from .payloads import shipment_sample


def _mock_entry(
    filter_type: str = "days",
    filter_amount: int = 7,
    *,
    include_history: bool = False,
) -> MagicMock:
    entry = MagicMock()
    entry.options = {
        CONF_DELIVERED_FILTER_TYPE: filter_type,
        CONF_DELIVERED_FILTER_AMOUNT: filter_amount,
        CONF_INCLUDE_HISTORY: include_history,
    }
    return entry


# ---------------------------------------------------------------------------
# filter_active_shipments / filter_delivered_shipments
# ---------------------------------------------------------------------------


def test_active_filter_excludes_delivered():
    assert filter_active_shipments([shipment_sample("DELIVERED")]) == []


def test_active_filter_includes_order_created():
    assert filter_active_shipments([shipment_sample("ORDER_CREATED")]) != []


def test_active_filter_includes_unknown_status():
    assert filter_active_shipments([shipment_sample("OUT_FOR_DELIVERY")]) != []


def test_active_filter_handles_missing_status():
    assert filter_active_shipments([{"parcelNumber": "X"}]) != []


def test_delivered_filter_only_includes_delivered():
    shipments = [shipment_sample("DELIVERED"), shipment_sample("ORDER_CREATED")]
    result = filter_delivered_shipments(shipments)
    assert len(result) == 1
    assert (result[0]["status"] or {}).get("description") == "DELIVERED"


# ---------------------------------------------------------------------------
# shipment_delivery_dt
# ---------------------------------------------------------------------------


def test_delivery_dt_parses_event_datetime_with_tz():
    dt = shipment_delivery_dt(
        shipment_sample(event_dt="2026-06-05T21:50:30", tz_id="Europe/Amsterdam")
    )
    assert dt is not None
    assert dt.year == 2026 and dt.month == 6 and dt.day == 5
    assert dt.tzinfo is not None


def test_delivery_dt_falls_back_to_delivery_date():
    dt = shipment_delivery_dt(
        shipment_sample(event_dt=None, tz_id=None, delivery_date="2026-06-05")
    )
    assert dt == datetime(2026, 6, 5, tzinfo=timezone.utc)


def test_delivery_dt_returns_none_when_unknown():
    assert shipment_delivery_dt(shipment_sample(event_dt=None, tz_id=None)) is None


def test_delivery_dt_invalid_tz_falls_back_to_utc():
    dt = shipment_delivery_dt(
        shipment_sample(event_dt="2026-06-05T21:50:30", tz_id="Not/A/Zone")
    )
    assert dt is not None
    assert dt.tzinfo is not None


# ---------------------------------------------------------------------------
# shipment_planned_dt
# ---------------------------------------------------------------------------


def test_planned_dt_parses_delivery_date_at_local_midnight():
    dt = shipment_planned_dt(
        shipment_sample(event_dt=None, tz_id="Europe/Amsterdam", delivery_date="2026-06-17")
    )
    assert dt is not None
    assert (dt.year, dt.month, dt.day) == (2026, 6, 17)
    assert (dt.hour, dt.minute) == (0, 0)
    assert dt.tzinfo is not None
    # 00:00 Europe/Amsterdam is offset from UTC
    assert dt.utcoffset() is not None


def test_planned_dt_returns_none_when_no_date():
    assert shipment_planned_dt(shipment_sample(event_dt=None, tz_id=None)) is None


def test_planned_dt_returns_none_for_garbage_date():
    assert shipment_planned_dt({"deliveryDate": "not a date"}) is None


def test_planned_dt_falls_back_to_utc_for_bad_tz():
    dt = shipment_planned_dt(
        shipment_sample(event_dt=None, tz_id="Not/A/Zone", delivery_date="2026-06-17")
    )
    assert dt is not None
    assert dt.tzinfo is not None


# ---------------------------------------------------------------------------
# DpdCoordinator._apply_delivered_filter
# ---------------------------------------------------------------------------


async def test_delivered_filter_days_excludes_old_parcels(hass):
    recent_date = (datetime.now(timezone.utc) - timedelta(days=2)).date().isoformat()
    old_date = (datetime.now(timezone.utc) - timedelta(days=30)).date().isoformat()
    shipments = [
        shipment_sample(event_dt=None, tz_id=None, delivery_date=recent_date),
        shipment_sample(event_dt=None, tz_id=None, delivery_date=old_date),
    ]

    coordinator = DpdCoordinator(hass, MagicMock(), _mock_entry("days", 7))
    result = coordinator._apply_delivered_filter(shipments)
    assert len(result) == 1
    assert result[0]["deliveryDate"] == recent_date


async def test_delivered_filter_days_includes_parcel_without_date(hass):
    shipments = [shipment_sample(event_dt=None, tz_id=None)]
    coordinator = DpdCoordinator(hass, MagicMock(), _mock_entry("days", 7))
    assert len(coordinator._apply_delivered_filter(shipments)) == 1


async def test_delivered_filter_parcels_limits_count(hass):
    shipments = [shipment_sample(parcel_number=f"P{i}") for i in range(10)]
    coordinator = DpdCoordinator(hass, MagicMock(), _mock_entry("parcels", 3))
    result = coordinator._apply_delivered_filter(shipments)
    assert len(result) == 3


# ---------------------------------------------------------------------------
# DpdCoordinator._async_update_data
# ---------------------------------------------------------------------------


async def test_coordinator_splits_active_and_delivered(hass):
    client = MagicMock()
    client.async_get_parcel_detail = AsyncMock(return_value=None)
    client.async_get_parcels = AsyncMock(return_value={
        "incomingShipments": [
            shipment_sample("ORDER_CREATED", parcel_number="A"),
            shipment_sample("DELIVERED", parcel_number="B"),
        ],
        "sendingShipments": [shipment_sample("DELIVERED", parcel_number="C")],
    })

    coordinator = DpdCoordinator(hass, client, _mock_entry("days", 30))
    result = await coordinator._async_update_data()

    assert [s["barcode"] for s in result["incoming_active"]] == ["A"]
    assert [s["barcode"] for s in result["incoming_delivered"]] == ["B"]
    assert result["outgoing_active"] == []
    # Delivered outgoing shipments now land in their own bucket.
    assert [s["barcode"] for s in result["outgoing_delivered"]] == ["C"]


async def test_coordinator_applies_delivered_filter_to_outgoing(hass):
    recent = (datetime.now(timezone.utc) - timedelta(days=2)).date().isoformat()
    old = (datetime.now(timezone.utc) - timedelta(days=30)).date().isoformat()
    client = MagicMock()
    client.async_get_parcel_detail = AsyncMock(return_value=None)
    client.async_get_parcels = AsyncMock(return_value={
        "incomingShipments": [],
        "sendingShipments": [
            shipment_sample("DELIVERED", parcel_number="new", event_dt=None, tz_id=None, delivery_date=recent),
            shipment_sample("DELIVERED", parcel_number="old", event_dt=None, tz_id=None, delivery_date=old),
        ],
    })

    coordinator = DpdCoordinator(hass, client, _mock_entry("days", 7))
    result = await coordinator._async_update_data()

    assert [s["barcode"] for s in result["outgoing_delivered"]] == ["new"]


async def test_coordinator_handles_empty_response(hass):
    client = MagicMock()
    client.async_get_parcel_detail = AsyncMock(return_value=None)
    client.async_get_parcels = AsyncMock(return_value={
        "incomingShipments": [],
        "sendingShipments": [],
    })

    coordinator = DpdCoordinator(hass, client, _mock_entry())
    assert await coordinator._async_update_data() == {
        "incoming_active": [],
        "incoming_delivered": [],
        "outgoing_active": [],
        "outgoing_delivered": [],
    }


async def test_coordinator_raises_config_entry_auth_failed_on_auth_error(hass):
    from homeassistant.exceptions import ConfigEntryAuthFailed

    client = MagicMock()
    client.async_get_parcel_detail = AsyncMock(return_value=None)
    client.async_get_parcels = AsyncMock(side_effect=DpdAuthError("bad creds"))

    coordinator = DpdCoordinator(hass, client, _mock_entry())

    with pytest.raises(ConfigEntryAuthFailed):
        await coordinator._async_update_data()


async def test_coordinator_raises_update_failed_on_api_error(hass):
    from homeassistant.helpers.update_coordinator import UpdateFailed

    client = MagicMock()
    client.async_get_parcel_detail = AsyncMock(return_value=None)
    client.async_get_parcels = AsyncMock(side_effect=DpdApiError(500))

    coordinator = DpdCoordinator(hass, client, _mock_entry())

    with pytest.raises(UpdateFailed):
        await coordinator._async_update_data()


# ---------------------------------------------------------------------------
# log_unknown_descriptions
# ---------------------------------------------------------------------------


def test_known_descriptions_are_not_logged(caplog):
    _unknown_descriptions_logged.clear()
    caplog.set_level("INFO", logger="custom_components.dpd.coordinator")
    log_unknown_descriptions([
        shipment_sample("ORDER_CREATED"),
        shipment_sample("PARCEL_OUT_FOR_DELIVERY"),
        shipment_sample("DELIVERED"),
    ])
    assert "not yet catalogued" not in caplog.text


def test_unknown_description_is_logged_once(caplog):
    _unknown_descriptions_logged.clear()
    caplog.set_level("INFO", logger="custom_components.dpd.coordinator")
    log_unknown_descriptions([shipment_sample("TIME_TRAVELLING")])
    log_unknown_descriptions([shipment_sample("TIME_TRAVELLING")])
    log_unknown_descriptions([shipment_sample("TIME_TRAVELLING")])
    assert caplog.text.count("TIME_TRAVELLING") == 1


def test_unknown_descriptions_each_logged_once(caplog):
    _unknown_descriptions_logged.clear()
    caplog.set_level("INFO", logger="custom_components.dpd.coordinator")
    log_unknown_descriptions([
        shipment_sample("MYSTERY_ONE"),
        shipment_sample("MYSTERY_TWO"),
        shipment_sample("MYSTERY_ONE"),  # repeat — should not log again
    ])
    assert caplog.text.count("MYSTERY_ONE") == 1
    assert caplog.text.count("MYSTERY_TWO") == 1


def test_missing_description_is_ignored(caplog):
    _unknown_descriptions_logged.clear()
    caplog.set_level("INFO", logger="custom_components.dpd.coordinator")
    log_unknown_descriptions([{"parcelNumber": "X"}, {"parcelNumber": "Y", "status": {}}])
    assert "not yet catalogued" not in caplog.text


# ---------------------------------------------------------------------------
# fmp_hashcode
# ---------------------------------------------------------------------------


def test_fmp_hashcode_picks_from_available_actions():
    assert fmp_hashcode(shipment_sample(fmp_hashcode="abc")) == "abc"


def test_fmp_hashcode_returns_none_when_action_missing():
    assert fmp_hashcode(shipment_sample()) is None


def test_fmp_hashcode_returns_none_when_actions_empty():
    shipment = shipment_sample()
    shipment["availableActions"] = {"FOLLOW_MY_PARCEL": []}
    assert fmp_hashcode(shipment) is None


def test_fmp_hashcode_returns_none_when_action_has_no_hashcode():
    shipment = shipment_sample()
    shipment["availableActions"] = {"FOLLOW_MY_PARCEL": [{}]}
    assert fmp_hashcode(shipment) is None


def test_fmp_hashcode_returns_none_for_empty_string():
    assert fmp_hashcode(shipment_sample(fmp_hashcode="")) is None


# ---------------------------------------------------------------------------
# shipment_planned_dt with Follow My Parcel window
# ---------------------------------------------------------------------------


def test_planned_dt_prefers_fmp_window_over_date_midnight():
    fmp = {
        "deliveryDate": "2026-06-17",
        "timeRange": {"from": "10:34:00", "to": "11:34:00"},
    }
    dt = shipment_planned_dt(shipment_sample(
        delivery_date="2026-06-17",
        tz_id="Europe/Amsterdam",
        fmp_window=fmp,
    ))
    assert dt is not None
    assert (dt.hour, dt.minute, dt.second) == (10, 34, 0)
    assert dt.tzinfo is not None


def test_planned_dt_falls_back_to_midnight_when_fmp_window_lacks_from():
    fmp = {"deliveryDate": "2026-06-17", "timeRange": {}}
    dt = shipment_planned_dt(shipment_sample(
        delivery_date="2026-06-17",
        tz_id="Europe/Amsterdam",
        fmp_window=fmp,
    ))
    assert dt is not None
    assert (dt.hour, dt.minute) == (0, 0)


def test_planned_dt_falls_back_to_midnight_when_fmp_from_is_garbage():
    fmp = {"deliveryDate": "2026-06-17", "timeRange": {"from": "not-a-time"}}
    dt = shipment_planned_dt(shipment_sample(
        delivery_date="2026-06-17",
        tz_id="Europe/Amsterdam",
        fmp_window=fmp,
    ))
    assert dt is not None
    assert (dt.hour, dt.minute) == (0, 0)


# ---------------------------------------------------------------------------
# DpdCoordinator._enrich_with_fmp
# ---------------------------------------------------------------------------


async def test_enrich_with_fmp_skips_shipments_without_hashcode(hass):
    client = MagicMock()
    client.async_fmp_delivery_window = AsyncMock(return_value={"deliveryDate": "x"})
    coordinator = DpdCoordinator(hass, client, _mock_entry())

    await coordinator._enrich_with_fmp([shipment_sample("PARCEL_HANDED")])

    client.async_fmp_delivery_window.assert_not_called()


async def test_enrich_with_fmp_stores_window_on_shipment(hass):
    window = {
        "deliveryDate": "2026-06-17",
        "timeRange": {"from": "10:34:00", "to": "11:34:00"},
    }
    client = MagicMock()
    client.async_fmp_delivery_window = AsyncMock(return_value=window)
    coordinator = DpdCoordinator(hass, client, _mock_entry())

    shipment = shipment_sample("PARCEL_OUT_FOR_DELIVERY", fmp_hashcode="xyz123")
    await coordinator._enrich_with_fmp([shipment])

    client.async_fmp_delivery_window.assert_awaited_once_with("xyz123")
    assert shipment["fmpDeliveryDateAndTime"] == window


async def test_enrich_with_fmp_leaves_shipment_alone_when_window_unavailable(hass):
    client = MagicMock()
    client.async_fmp_delivery_window = AsyncMock(return_value=None)
    coordinator = DpdCoordinator(hass, client, _mock_entry())

    shipment = shipment_sample("PARCEL_OUT_FOR_DELIVERY", fmp_hashcode="xyz123")
    await coordinator._enrich_with_fmp([shipment])

    assert "fmpDeliveryDateAndTime" not in shipment


# ---------------------------------------------------------------------------
# DpdCoordinator._enrich_detail_cache
# ---------------------------------------------------------------------------


async def test_enrich_detail_cache_populates_receiver_weight_dimensions(hass):
    client = MagicMock()
    client.async_get_parcel_detail = AsyncMock(
        return_value={
            "receiver": {"name": "Jane Doe"},
            "weight": 4.40,
            "dimensions": {"length": 31, "width": 23, "height": 17},
        }
    )
    coordinator = DpdCoordinator(hass, client, _mock_entry())

    shipment = shipment_sample("PARCEL_HANDED", parcel_number="01ABC")
    shipment["shipmentBUCode"] = "021"
    await coordinator._enrich_detail_cache([shipment], [])

    client.async_get_parcel_detail.assert_awaited_once_with(
        "01ABC", shipment_bu_code="021", parcel_type="INCOMING"
    )
    assert coordinator._detail_cache == {
        "01ABC": {
            "receiver_name": "Jane Doe",
            "weight": 4.40,
            "dimensions": {
                "length": 31, "width": 23, "height": 17,
                "text": "31 x 23 x 17 cm",
            },
            # History off by default → None; status remembered for refresh.
            "history": None,
            "_status_description": "PARCEL_HANDED",
        }
    }


async def test_enrich_detail_cache_uses_outgoing_parcel_type(hass):
    client = MagicMock()
    client.async_get_parcel_detail = AsyncMock(
        return_value={"receiver": {"name": "Out Receiver"}, "weight": None, "dimensions": None}
    )
    coordinator = DpdCoordinator(hass, client, _mock_entry())

    shipment = shipment_sample("PARCEL_HANDED", parcel_number="01OUT")
    await coordinator._enrich_detail_cache([], [shipment])

    args = client.async_get_parcel_detail.await_args
    assert args.kwargs["parcel_type"] == "OUTGOING"


async def test_enrich_detail_cache_caches_failure_and_skips_same_status(hass):
    """A failed detail call is cached so we don't retry on every refresh
    and hammer DPD when the endpoint is flaky."""
    client = MagicMock()
    client.async_get_parcel_detail = AsyncMock(return_value=None)
    coordinator = DpdCoordinator(hass, client, _mock_entry())

    shipment = shipment_sample("PARCEL_HANDED", parcel_number="01ABC")
    await coordinator._enrich_detail_cache([shipment], [])
    assert coordinator._detail_cache == {
        "01ABC": {"_failed": True, "_status_description": "PARCEL_HANDED"}
    }

    # Same status again → no new call.
    await coordinator._enrich_detail_cache([shipment], [])
    assert client.async_get_parcel_detail.await_count == 1


async def test_enrich_detail_cache_retries_failure_on_status_change(hass):
    """A cached failure is retried once the parcel's status moves, so one
    hiccup does not mean missing receiver/weight until an HA restart."""
    client = MagicMock()
    client.async_get_parcel_detail = AsyncMock(return_value=None)
    coordinator = DpdCoordinator(hass, client, _mock_entry())

    await coordinator._enrich_detail_cache(
        [shipment_sample("PARCEL_HANDED", parcel_number="01ABC")], []
    )
    assert client.async_get_parcel_detail.await_count == 1

    client.async_get_parcel_detail = AsyncMock(
        return_value={"receiver": {"name": "Jane Doe"}, "weight": 1.2, "dimensions": None}
    )
    await coordinator._enrich_detail_cache(
        [shipment_sample("IN_TRANSIT", parcel_number="01ABC")], []
    )
    assert client.async_get_parcel_detail.await_count == 1
    cached = coordinator._detail_cache["01ABC"]
    assert cached["receiver_name"] == "Jane Doe"
    assert not cached.get("_failed")


async def test_enrich_detail_cache_skips_already_cached_barcodes(hass):
    client = MagicMock()
    client.async_get_parcel_detail = AsyncMock()
    coordinator = DpdCoordinator(hass, client, _mock_entry())
    coordinator._detail_cache = {
        "01ABC": {"receiver_name": "Cached Person", "weight": 1.0, "dimensions": None}
    }

    await coordinator._enrich_detail_cache(
        [shipment_sample("PARCEL_HANDED", parcel_number="01ABC")], []
    )

    client.async_get_parcel_detail.assert_not_called()
    assert coordinator._detail_cache["01ABC"]["receiver_name"] == "Cached Person"


# ---------------------------------------------------------------------------
# map_parcel_status
# ---------------------------------------------------------------------------


def test_map_status_order_created_is_registered():
    assert map_parcel_status(shipment_sample("ORDER_CREATED")) == ParcelStatus.REGISTERED


def test_map_status_handed_in_transit_at_center_all_map_to_in_transit():
    assert map_parcel_status(shipment_sample("PARCEL_HANDED")) == ParcelStatus.IN_TRANSIT
    assert map_parcel_status(shipment_sample("IN_TRANSIT")) == ParcelStatus.IN_TRANSIT
    assert map_parcel_status(shipment_sample("AT_DELIVERY_CENTER")) == ParcelStatus.IN_TRANSIT


def test_map_status_parcel_out_for_delivery_is_out_for_delivery():
    assert (
        map_parcel_status(shipment_sample("PARCEL_OUT_FOR_DELIVERY"))
        == ParcelStatus.OUT_FOR_DELIVERY
    )


def test_map_status_delivered_is_delivered():
    assert map_parcel_status(shipment_sample("DELIVERED")) == ParcelStatus.DELIVERED


def test_map_status_parcelshop_and_return_statuses():
    """Statuses confirmed from the myDPD app (never seen in early sample data)."""
    assert (
        map_parcel_status(shipment_sample("AVAILABLE_FOR_COLLECTION"))
        == ParcelStatus.AT_PICKUP_POINT
    )
    assert map_parcel_status(shipment_sample("RETURN_TO_SENDER")) == ParcelStatus.RETURNING
    assert (
        map_parcel_status(shipment_sample("UNSUCCESSFUL_DELIVERY_ATTEMPTED"))
        == ParcelStatus.IN_TRANSIT
    )


def test_new_parcelshop_statuses_are_known_and_not_logged(caplog):
    """The new descriptions are in KNOWN_DESCRIPTIONS → no 'unrecognised' warning."""
    log_unknown_descriptions([
        shipment_sample("AVAILABLE_FOR_COLLECTION"),
        shipment_sample("RETURN_TO_SENDER"),
        shipment_sample("UNSUCCESSFUL_DELIVERY_ATTEMPTED"),
    ])
    assert "issues/new" not in caplog.text


def test_map_status_unknown_description_falls_back_to_unknown():
    assert map_parcel_status(shipment_sample("INVENTED_BY_DPD")) == ParcelStatus.UNKNOWN


def test_map_status_missing_status_field_falls_back_to_unknown():
    assert map_parcel_status({"parcelNumber": "X"}) == ParcelStatus.UNKNOWN


# ---------------------------------------------------------------------------
# _tracking_url
# ---------------------------------------------------------------------------


def test_tracking_url_built_from_parcel_number():
    assert _tracking_url({"parcelNumber": "01ABC"}) == (
        "https://www.dpdgroup.com/nl/mydpd/my-parcels/search?parcelNumber=01ABC"
    )


def test_tracking_url_uses_bu_country_segment():
    assert _tracking_url({"parcelNumber": "01ABC"}, "DPD-DE") == (
        "https://www.dpdgroup.com/de/mydpd/my-parcels/search?parcelNumber=01ABC"
    )
    assert _tracking_url({"parcelNumber": "01ABC"}, "DPD-CH") == (
        "https://www.dpdgroup.com/ch/mydpd/my-parcels/search?parcelNumber=01ABC"
    )


def test_tracking_url_uses_override_for_non_dpd_prefixed_brands():
    """BRT (Italy) is a wholly separate domain; CHR-PT (Portugal) keeps mydpd."""
    assert _tracking_url({"parcelNumber": "01ABC"}, "BRT") == (
        "https://www.mybrt.it/it/mybrt/my-parcels/incoming?parcelNumber=01ABC"
    )
    assert _tracking_url({"parcelNumber": "01ABC"}, "CHR-PT") == (
        "https://www.dpdgroup.com/pt/mydpd/my-parcels/search?parcelNumber=01ABC"
    )


def test_tracking_url_returns_none_without_parcel_number():
    assert _tracking_url({}) is None
    assert _tracking_url({"parcelNumber": ""}) is None


# ---------------------------------------------------------------------------
# normalize_parcel
# ---------------------------------------------------------------------------


def test_normalize_returns_carrier_agnostic_keys():
    raw = shipment_sample("PARCEL_OUT_FOR_DELIVERY", parcel_number="01XYZ")
    raw["senderName"] = "Acme Webshop"
    raw["status"]["deliveryType"] = "HOME"
    normalized = normalize_parcel(raw)
    assert normalized["carrier"] == "DPD"
    assert normalized["barcode"] == "01XYZ"
    assert normalized["sender"] == "Acme Webshop"
    assert normalized["receiver"] is None
    assert normalized["weight"] is None
    assert normalized["dimensions"] is None
    assert normalized["status"] == ParcelStatus.OUT_FOR_DELIVERY
    assert normalized["raw_status"] == "PARCEL_OUT_FOR_DELIVERY"
    assert normalized["delivered"] is False
    assert normalized["pickup"] is False
    assert normalized["pickup_point"] is None
    assert normalized["url"].endswith("parcelNumber=01XYZ")
    assert normalized["raw"] is raw  # original payload preserved by identity


def test_capabilities_are_known_values():
    """A typo here would silently misreport this carrier on the docs site."""
    assert CAPABILITIES <= KNOWN_CAPABILITIES


def test_capabilities_match_the_missing_pickup_point_gap():
    """CAPABILITIES must agree with test_normalize_returns_carrier_agnostic_keys
    and test_normalize_carries_weight_and_dimensions_when_provided — DPD's
    detail call fills weight/dimensions/history/window, but never a named
    pickup point."""
    assert CAPABILITIES == {
        "weight",
        "dimensions",
        "delivery_window",
        "url",
        "history",
    }


def test_normalize_url_country_segment_follows_bu():
    raw = shipment_sample("PARCEL_OUT_FOR_DELIVERY", parcel_number="01XYZ")
    normalized = normalize_parcel(raw, bu="DPD-DE")
    assert normalized["url"].startswith("https://www.dpdgroup.com/de/mydpd/")


def test_normalize_carries_receiver_when_provided():
    raw = shipment_sample("PARCEL_OUT_FOR_DELIVERY", parcel_number="01XYZ")
    normalized = normalize_parcel(raw, receiver="Jane Doe")
    assert normalized["receiver"] == "Jane Doe"


def test_normalize_carries_weight_and_dimensions_when_provided():
    raw = shipment_sample("PARCEL_OUT_FOR_DELIVERY", parcel_number="01XYZ")
    dims = {"length": 31, "width": 23, "height": 17}
    normalized = normalize_parcel(raw, weight=4.40, dimensions=dims)
    assert normalized["weight"] == 4.40
    assert normalized["dimensions"] == dims


def test_normalize_marks_pickup_for_parcelshop_delivery():
    raw = shipment_sample("PARCEL_OUT_FOR_DELIVERY")
    raw["status"]["deliveryType"] = "PARCELSHOP"
    assert normalize_parcel(raw)["pickup"] is True


def test_normalize_delivered_parcel_carries_delivered_at_not_planned_window():
    raw = shipment_sample(
        "DELIVERED",
        delivery_date="2026-06-05",
        event_dt="2026-06-05T14:23:12",
        tz_id="Europe/Amsterdam",
    )
    normalized = normalize_parcel(raw)
    assert normalized["delivered"] is True
    assert normalized["delivered_at"] is not None
    assert "2026-06-05T14:23:12" in normalized["delivered_at"]
    assert normalized["planned_from"] is None
    assert normalized["planned_to"] is None


def test_normalize_active_parcel_derives_planned_window_from_fmp():
    raw = shipment_sample(
        "PARCEL_OUT_FOR_DELIVERY",
        delivery_date="2026-06-17",
        tz_id="Europe/Amsterdam",
        fmp_window={
            "deliveryDate": "2026-06-17",
            "timeRange": {"from": "10:34:00", "to": "11:34:00"},
        },
    )
    normalized = normalize_parcel(raw)
    assert normalized["planned_from"].startswith("2026-06-17T10:34:00")
    assert normalized["planned_to"].startswith("2026-06-17T11:34:00")


def test_normalize_does_not_mutate_raw_payload():
    raw = shipment_sample(
        "PARCEL_OUT_FOR_DELIVERY",
        delivery_date="2026-06-22",
        tz_id="Europe/Amsterdam",
        delivery_time_from="21:03:00",
        delivery_time_to="22:03:00",
    )
    snapshot = set(raw.keys())
    normalized = normalize_parcel(raw)
    assert set(normalized["raw"].keys()) == snapshot
    assert "plannedDeliveryFrom" not in normalized["raw"]
    assert "plannedDeliveryTo" not in normalized["raw"]


# ---------------------------------------------------------------------------
# shipment_planned_window (from, to)
# ---------------------------------------------------------------------------


def test_planned_window_returns_fmp_range_when_available():
    fmp = {
        "deliveryDate": "2026-06-17",
        "timeRange": {"from": "10:34:00", "to": "11:34:00"},
    }
    start, end = shipment_planned_window(shipment_sample(
        delivery_date="2026-06-17",
        tz_id="Europe/Amsterdam",
        fmp_window=fmp,
    ))
    assert start is not None and end is not None
    assert (start.hour, start.minute) == (10, 34)
    assert (end.hour, end.minute) == (11, 34)
    assert start.tzinfo is not None
    assert end.tzinfo is not None


def test_planned_window_full_day_when_only_date_known():
    start, end = shipment_planned_window(shipment_sample(
        delivery_date="2026-06-17",
        tz_id="Europe/Amsterdam",
    ))
    assert start is not None and end is not None
    assert (start.hour, start.minute, start.second) == (0, 0, 0)
    assert (end.hour, end.minute, end.second) == (23, 59, 59)
    assert start.date() == end.date()


def test_planned_window_returns_none_when_no_date():
    assert shipment_planned_window({}) == (None, None)


def test_planned_window_full_day_when_fmp_lacks_from_or_to():
    fmp = {"deliveryDate": "2026-06-17", "timeRange": {"from": "10:34:00"}}
    start, end = shipment_planned_window(shipment_sample(
        delivery_date="2026-06-17",
        tz_id="Europe/Amsterdam",
        fmp_window=fmp,
    ))
    # No `to` in FMP → fall back to the top-level / full-day window
    assert (start.hour, end.hour) == (0, 23)


def test_planned_window_uses_top_level_delivery_time_when_fmp_absent():
    start, end = shipment_planned_window(shipment_sample(
        delivery_date="2026-06-22",
        tz_id="Europe/Amsterdam",
        delivery_time_from="21:03:00",
        delivery_time_to="22:03:00",
    ))
    assert start is not None and end is not None
    assert (start.hour, start.minute) == (21, 3)
    assert (end.hour, end.minute) == (22, 3)
    assert start.tzinfo is not None
    assert end.tzinfo is not None


def test_planned_window_top_level_takes_precedence_over_full_day_fallback():
    # Only one bound present → fall back to full-day, not a half-window.
    start, end = shipment_planned_window(shipment_sample(
        delivery_date="2026-06-22",
        tz_id="Europe/Amsterdam",
        delivery_time_from="21:03:00",
    ))
    assert (start.hour, end.hour) == (0, 23)


# ---------------------------------------------------------------------------
# Coordinator publishes planned windows without mutating raw payloads
# ---------------------------------------------------------------------------


async def test_coordinator_publishes_planned_window_without_touching_raw(hass):
    client = MagicMock()
    client.async_get_parcel_detail = AsyncMock(return_value=None)
    client.async_get_parcels = AsyncMock(return_value={
        "incomingShipments": [
            shipment_sample("ORDER_CREATED", parcel_number="A", delivery_date="2026-06-17"),
            shipment_sample("DELIVERED", parcel_number="B", delivery_date="2026-06-10"),
        ],
        "sendingShipments": [
            shipment_sample("PARCEL_HANDED", parcel_number="C", delivery_date="2026-06-18"),
        ],
    })
    client.async_fmp_delivery_window = AsyncMock(return_value=None)

    coordinator = DpdCoordinator(hass, client, _mock_entry("days", 30))
    result = await coordinator._async_update_data()

    # Active buckets carry the from/to on the normalised fields; delivered
    # parcels clear them because delivered_at carries the truth instead.
    for parcel in result["incoming_active"] + result["outgoing_active"]:
        assert parcel["planned_from"] is not None
        assert parcel["planned_to"] is not None
    for parcel in result["incoming_delivered"]:
        assert parcel["planned_from"] is None
        assert parcel["planned_to"] is None

    # `raw` must be pristine — no derived plannedDelivery* fields leaked in.
    for bucket in ("incoming_active", "incoming_delivered", "outgoing_active", "outgoing_delivered"):
        for parcel in result[bucket]:
            assert "plannedDeliveryFrom" not in parcel["raw"]
            assert "plannedDeliveryTo" not in parcel["raw"]


# ---------------------------------------------------------------------------
# Parcel events
# ---------------------------------------------------------------------------


def _capture(hass, event_type: str) -> list:
    events: list = []
    hass.bus.async_listen(event_type, events.append)
    return events


async def test_first_refresh_suppresses_registered_events(hass):
    """Parcels present on the first poll do not yield registered events."""
    client = MagicMock()
    client.async_get_parcel_detail = AsyncMock(return_value=None)
    client.async_get_parcels = AsyncMock(return_value={
        "incomingShipments": [
            shipment_sample("ORDER_CREATED", parcel_number="A"),
            shipment_sample("PARCEL_HANDED", parcel_number="B"),
        ],
        "sendingShipments": [],
    })
    client.async_fmp_delivery_window = AsyncMock(return_value=None)

    coordinator = DpdCoordinator(hass, client, _mock_entry())
    captured = _capture(hass, "dpd_parcel_registered")

    await coordinator._async_update_data()
    await hass.async_block_till_done()

    assert captured == []


async def test_second_refresh_fires_registered_event_for_new_parcel(hass):
    client = MagicMock()
    client.async_fmp_delivery_window = AsyncMock(return_value=None)
    client.async_get_parcel_detail = AsyncMock(return_value=None)
    client.async_get_parcels = AsyncMock(side_effect=[
        {
            "incomingShipments": [shipment_sample("PARCEL_HANDED", parcel_number="A")],
            "sendingShipments": [],
        },
        {
            "incomingShipments": [
                shipment_sample("PARCEL_HANDED", parcel_number="A"),
                shipment_sample("ORDER_CREATED", parcel_number="NEW"),
            ],
            "sendingShipments": [],
        },
    ])

    coordinator = DpdCoordinator(hass, client, _mock_entry())
    await coordinator._async_update_data()

    captured = _capture(hass, "dpd_parcel_registered")
    await coordinator._async_update_data()
    await hass.async_block_till_done()

    assert len(captured) == 1
    payload = captured[0].data
    assert payload["barcode"] == "NEW"
    assert payload["status"] == ParcelStatus.REGISTERED
    assert payload["carrier"] == "DPD"


async def test_status_change_fires_status_changed_event(hass):
    """A known parcel whose status transitions yields one status_changed event."""
    client = MagicMock()
    client.async_fmp_delivery_window = AsyncMock(return_value=None)
    client.async_get_parcel_detail = AsyncMock(return_value=None)
    client.async_get_parcels = AsyncMock(side_effect=[
        {
            "incomingShipments": [shipment_sample("PARCEL_HANDED", parcel_number="A")],
            "sendingShipments": [],
        },
        {
            "incomingShipments": [
                shipment_sample("PARCEL_OUT_FOR_DELIVERY", parcel_number="A"),
            ],
            "sendingShipments": [],
        },
    ])

    coordinator = DpdCoordinator(hass, client, _mock_entry())
    await coordinator._async_update_data()

    captured = _capture(hass, "dpd_parcel_status_changed")
    await coordinator._async_update_data()
    await hass.async_block_till_done()

    assert len(captured) == 1
    payload = captured[0].data
    assert payload["barcode"] == "A"
    assert payload["old_status"] == ParcelStatus.IN_TRANSIT
    assert payload["new_status"] == ParcelStatus.OUT_FOR_DELIVERY
    assert payload["status"] == ParcelStatus.OUT_FOR_DELIVERY


async def test_incoming_delivered_fires_dedicated_event(hass):
    """An incoming parcel that transitions to delivered fires parcel_delivered
    and NOT parcel_status_changed (delivered takes precedence)."""
    recent = (datetime.now(timezone.utc) - timedelta(days=1)).date().isoformat()
    client = MagicMock()
    client.async_fmp_delivery_window = AsyncMock(return_value=None)
    client.async_get_parcel_detail = AsyncMock(return_value=None)
    client.async_get_parcels = AsyncMock(side_effect=[
        {"incomingShipments": [shipment_sample("PARCEL_HANDED", parcel_number="A")], "sendingShipments": []},
        {"incomingShipments": [
            shipment_sample("DELIVERED", parcel_number="A", event_dt=None, tz_id=None, delivery_date=recent),
        ], "sendingShipments": []},
    ])

    coordinator = DpdCoordinator(hass, client, _mock_entry("days", 7))
    await coordinator._async_update_data()

    delivered = _capture(hass, "dpd_parcel_delivered")
    changed = _capture(hass, "dpd_parcel_status_changed")
    await coordinator._async_update_data()
    await hass.async_block_till_done()

    assert len(delivered) == 1
    assert delivered[0].data["barcode"] == "A"
    assert delivered[0].data["status"] == ParcelStatus.DELIVERED
    assert changed == []


async def test_no_events_for_new_already_delivered_incoming(hass):
    """A barcode first seen already delivered fires neither registered nor delivered."""
    recent = (datetime.now(timezone.utc) - timedelta(days=1)).date().isoformat()
    client = MagicMock()
    client.async_fmp_delivery_window = AsyncMock(return_value=None)
    client.async_get_parcel_detail = AsyncMock(return_value=None)
    client.async_get_parcels = AsyncMock(side_effect=[
        {"incomingShipments": [shipment_sample("PARCEL_HANDED", parcel_number="A")], "sendingShipments": []},
        {"incomingShipments": [
            shipment_sample("PARCEL_HANDED", parcel_number="A"),
            shipment_sample("DELIVERED", parcel_number="B", event_dt=None, tz_id=None, delivery_date=recent),
        ], "sendingShipments": []},
    ])

    coordinator = DpdCoordinator(hass, client, _mock_entry("days", 7))
    await coordinator._async_update_data()

    registered = _capture(hass, "dpd_parcel_registered")
    delivered = _capture(hass, "dpd_parcel_delivered")
    await coordinator._async_update_data()
    await hass.async_block_till_done()

    assert registered == []
    assert delivered == []


async def test_unchanged_status_fires_no_event(hass):
    """Polling the same parcel with the same mapped status fires nothing."""
    client = MagicMock()
    client.async_fmp_delivery_window = AsyncMock(return_value=None)
    # Same description across two polls — should not trigger a change.
    shipment = shipment_sample("PARCEL_HANDED", parcel_number="A")
    client.async_get_parcel_detail = AsyncMock(return_value=None)
    client.async_get_parcels = AsyncMock(return_value={
        "incomingShipments": [shipment],
        "sendingShipments": [],
    })

    coordinator = DpdCoordinator(hass, client, _mock_entry())
    await coordinator._async_update_data()

    captured_registered = _capture(hass, "dpd_parcel_registered")
    captured_changed = _capture(hass, "dpd_parcel_status_changed")
    await coordinator._async_update_data()
    await hass.async_block_till_done()

    assert captured_registered == []
    assert captured_changed == []


async def test_first_refresh_suppresses_outgoing_events(hass):
    """Outgoing parcels present on the first poll do not yield events."""
    client = MagicMock()
    client.async_fmp_delivery_window = AsyncMock(return_value=None)
    client.async_get_parcel_detail = AsyncMock(return_value=None)
    client.async_get_parcels = AsyncMock(return_value={
        "incomingShipments": [],
        "sendingShipments": [shipment_sample("PARCEL_HANDED", parcel_number="S")],
    })

    coordinator = DpdCoordinator(hass, client, _mock_entry())
    changed = _capture(hass, "dpd_outgoing_parcel_status_changed")
    delivered = _capture(hass, "dpd_outgoing_parcel_delivered")

    await coordinator._async_update_data()
    await hass.async_block_till_done()

    assert changed == []
    assert delivered == []


async def test_outgoing_status_change_fires_outgoing_status_changed(hass):
    """A sent shipment whose status transitions fires outgoing_parcel_status_changed."""
    client = MagicMock()
    client.async_fmp_delivery_window = AsyncMock(return_value=None)
    client.async_get_parcel_detail = AsyncMock(return_value=None)
    client.async_get_parcels = AsyncMock(side_effect=[
        {"incomingShipments": [], "sendingShipments": [shipment_sample("ORDER_CREATED", parcel_number="S")]},
        {"incomingShipments": [], "sendingShipments": [shipment_sample("PARCEL_HANDED", parcel_number="S")]},
    ])

    coordinator = DpdCoordinator(hass, client, _mock_entry())
    await coordinator._async_update_data()

    captured = _capture(hass, "dpd_outgoing_parcel_status_changed")
    await coordinator._async_update_data()
    await hass.async_block_till_done()

    assert len(captured) == 1
    payload = captured[0].data
    assert payload["barcode"] == "S"
    assert payload["old_status"] == ParcelStatus.REGISTERED
    assert payload["new_status"] == ParcelStatus.IN_TRANSIT


async def test_outgoing_delivered_fires_dedicated_event(hass):
    """A sent shipment that transitions to delivered fires outgoing_parcel_delivered
    and NOT outgoing_parcel_status_changed (delivered takes precedence)."""
    recent = (datetime.now(timezone.utc) - timedelta(days=1)).date().isoformat()
    client = MagicMock()
    client.async_fmp_delivery_window = AsyncMock(return_value=None)
    client.async_get_parcel_detail = AsyncMock(return_value=None)
    client.async_get_parcels = AsyncMock(side_effect=[
        {"incomingShipments": [], "sendingShipments": [shipment_sample("PARCEL_HANDED", parcel_number="S")]},
        {"incomingShipments": [], "sendingShipments": [
            shipment_sample("DELIVERED", parcel_number="S", event_dt=None, tz_id=None, delivery_date=recent),
        ]},
    ])

    coordinator = DpdCoordinator(hass, client, _mock_entry("days", 7))
    await coordinator._async_update_data()

    delivered = _capture(hass, "dpd_outgoing_parcel_delivered")
    changed = _capture(hass, "dpd_outgoing_parcel_status_changed")
    await coordinator._async_update_data()
    await hass.async_block_till_done()

    assert len(delivered) == 1
    assert delivered[0].data["barcode"] == "S"
    assert changed == []


async def test_inter_in_transit_descriptions_do_not_fire(hass):
    """PARCEL_HANDED → IN_TRANSIT → AT_DELIVERY_CENTER all map to IN_TRANSIT.

    The canonical status does not change, so no status_changed event fires
    even though the raw description does — that's the whole point of the
    enum: cross-carrier automations react to canonical lifecycle changes,
    not to internal renaming inside the carrier's tracking timeline.
    """
    client = MagicMock()
    client.async_fmp_delivery_window = AsyncMock(return_value=None)
    client.async_get_parcel_detail = AsyncMock(return_value=None)
    client.async_get_parcels = AsyncMock(side_effect=[
        {"incomingShipments": [shipment_sample("PARCEL_HANDED", parcel_number="A")], "sendingShipments": []},
        {"incomingShipments": [shipment_sample("IN_TRANSIT", parcel_number="A")], "sendingShipments": []},
        {"incomingShipments": [shipment_sample("AT_DELIVERY_CENTER", parcel_number="A")], "sendingShipments": []},
    ])

    coordinator = DpdCoordinator(hass, client, _mock_entry())
    await coordinator._async_update_data()

    captured = _capture(hass, "dpd_parcel_status_changed")
    await coordinator._async_update_data()
    await coordinator._async_update_data()
    await hass.async_block_till_done()

    assert captured == []


async def test_delivery_time_changed_fires_when_planned_time_appears(hass):
    """A parcel that gains a planned_from value fires delivery_time_changed."""
    client = MagicMock()
    client.async_fmp_delivery_window = AsyncMock(return_value=None)
    client.async_get_parcel_detail = AsyncMock(return_value=None)
    client.async_get_parcels = AsyncMock(side_effect=[
        {
            "incomingShipments": [shipment_sample("PARCEL_HANDED", parcel_number="A")],
            "sendingShipments": [],
        },
        {
            "incomingShipments": [
                shipment_sample(
                    "PARCEL_HANDED",
                    parcel_number="A",
                    delivery_date="2026-06-27",
                ),
            ],
            "sendingShipments": [],
        },
    ])

    coordinator = DpdCoordinator(hass, client, _mock_entry())
    await coordinator._async_update_data()

    captured = _capture(hass, "dpd_parcel_delivery_time_changed")
    await coordinator._async_update_data()
    await hass.async_block_till_done()

    assert len(captured) == 1
    payload = captured[0].data
    assert payload["barcode"] == "A"
    assert payload["old_planned_from"] is None
    assert payload["new_planned_from"] is not None


async def test_delivery_time_changed_fires_when_planned_time_shifts(hass):
    """A parcel whose planned_from changes to a new value fires the event."""
    client = MagicMock()
    client.async_fmp_delivery_window = AsyncMock(return_value=None)
    client.async_get_parcel_detail = AsyncMock(return_value=None)
    client.async_get_parcels = AsyncMock(side_effect=[
        {
            "incomingShipments": [
                shipment_sample(
                    "PARCEL_HANDED",
                    parcel_number="A",
                    delivery_date="2026-06-27",
                ),
            ],
            "sendingShipments": [],
        },
        {
            "incomingShipments": [
                shipment_sample(
                    "PARCEL_HANDED",
                    parcel_number="A",
                    delivery_date="2026-06-28",
                ),
            ],
            "sendingShipments": [],
        },
    ])

    coordinator = DpdCoordinator(hass, client, _mock_entry())
    await coordinator._async_update_data()

    captured = _capture(hass, "dpd_parcel_delivery_time_changed")
    await coordinator._async_update_data()
    await hass.async_block_till_done()

    assert len(captured) == 1
    assert captured[0].data["old_planned_from"] != captured[0].data["new_planned_from"]
    assert captured[0].data["new_planned_from"] is not None


async def test_no_delivery_time_changed_event_when_planned_time_clears(hass):
    """value → null transitions are silent (don't page users on lost ETAs)."""
    client = MagicMock()
    client.async_fmp_delivery_window = AsyncMock(return_value=None)
    client.async_get_parcel_detail = AsyncMock(return_value=None)
    client.async_get_parcels = AsyncMock(side_effect=[
        {
            "incomingShipments": [
                shipment_sample(
                    "PARCEL_HANDED",
                    parcel_number="A",
                    delivery_date="2026-06-27",
                ),
            ],
            "sendingShipments": [],
        },
        {
            "incomingShipments": [shipment_sample("PARCEL_HANDED", parcel_number="A")],
            "sendingShipments": [],
        },
    ])

    coordinator = DpdCoordinator(hass, client, _mock_entry())
    await coordinator._async_update_data()

    captured = _capture(hass, "dpd_parcel_delivery_time_changed")
    await coordinator._async_update_data()
    await hass.async_block_till_done()

    assert captured == []


async def test_no_delivery_time_changed_event_when_planned_time_unchanged(hass):
    """An unchanged planned_from does not fire the event."""
    client = MagicMock()
    client.async_fmp_delivery_window = AsyncMock(return_value=None)
    client.async_get_parcel_detail = AsyncMock(return_value=None)
    shipment = shipment_sample("PARCEL_HANDED", parcel_number="A", delivery_date="2026-06-27")
    client.async_get_parcels = AsyncMock(return_value={
        "incomingShipments": [shipment],
        "sendingShipments": [],
    })

    coordinator = DpdCoordinator(hass, client, _mock_entry())
    await coordinator._async_update_data()

    captured = _capture(hass, "dpd_parcel_delivery_time_changed")
    await coordinator._async_update_data()
    await hass.async_block_till_done()

    assert captured == []


async def test_coordinator_calls_fmp_for_eligible_shipments(hass):
    window = {
        "deliveryDate": "2026-06-17",
        "timeRange": {"from": "10:34:00", "to": "11:34:00"},
    }
    client = MagicMock()
    client.async_get_parcel_detail = AsyncMock(return_value=None)
    client.async_get_parcels = AsyncMock(return_value={
        "incomingShipments": [
            shipment_sample("PARCEL_OUT_FOR_DELIVERY", parcel_number="A", fmp_hashcode="hashA"),
            shipment_sample("PARCEL_HANDED", parcel_number="B"),
        ],
        "sendingShipments": [],
    })
    client.async_fmp_delivery_window = AsyncMock(return_value=window)

    coordinator = DpdCoordinator(hass, client, _mock_entry())
    result = await coordinator._async_update_data()

    client.async_fmp_delivery_window.assert_awaited_once_with("hashA")
    # FMP window lives under the preserved `raw` payload after normalisation.
    by_barcode = {p["barcode"]: p for p in result["incoming_active"]}
    assert by_barcode["A"]["raw"]["fmpDeliveryDateAndTime"] == window
    assert "fmpDeliveryDateAndTime" not in by_barcode["B"]["raw"]


# ---------------------------------------------------------------------------
# _refresh_interval
# ---------------------------------------------------------------------------


def test_refresh_interval_defaults_to_30_minutes_when_option_unset():
    entry = MagicMock()
    entry.options = {}
    assert _refresh_interval(entry).total_seconds() == 30 * 60


def test_refresh_interval_reads_minutes_from_options():
    entry = MagicMock()
    entry.options = {"refresh_interval": 120}
    assert _refresh_interval(entry).total_seconds() == 120 * 60


# ---------------------------------------------------------------------------
# _augment_dimensions
# ---------------------------------------------------------------------------


def test_augment_dimensions_formats_l_w_h_with_lowercase_x_and_cm():
    result = _augment_dimensions({"length": 31, "width": 23, "height": 17})
    assert result["text"] == "31 x 23 x 17 cm"


def test_augment_dimensions_rounds_floats_to_int():
    result = _augment_dimensions({"length": 31.4, "width": 23.6, "height": 17.0})
    assert result["text"] == "31 x 24 x 17 cm"


def test_augment_dimensions_text_is_none_when_any_field_missing():
    result = _augment_dimensions({"length": 31, "width": 23})
    assert "text" in result and result["text"] is None
    # The partial fields are still present so callers can use what they have.
    assert result["length"] == 31


def test_augment_dimensions_returns_none_for_empty_input():
    assert _augment_dimensions(None) is None
    assert _augment_dimensions({}) is None


# ---------------------------------------------------------------------------
# sort_parcels_by_ts
# ---------------------------------------------------------------------------


def _norm(barcode: str, planned_from: str | None = None, delivered_at: str | None = None) -> dict:
    return {
        "barcode": barcode,
        "planned_from": planned_from,
        "delivered_at": delivered_at,
    }


def test_sort_orders_ascending_by_planned_from():
    parcels = [
        _norm("late", planned_from="2026-06-15T10:00:00+00:00"),
        _norm("early", planned_from="2026-06-13T08:00:00+00:00"),
        _norm("mid", planned_from="2026-06-14T12:00:00+00:00"),
    ]
    ordered = [p["barcode"] for p in sort_parcels_by_ts(parcels, "planned_from")]
    assert ordered == ["early", "mid", "late"]


def test_sort_orders_descending_for_delivered_at():
    parcels = [
        _norm("oldest", delivered_at="2026-06-13T08:00:00+00:00"),
        _norm("newest", delivered_at="2026-06-15T10:00:00+00:00"),
        _norm("mid", delivered_at="2026-06-14T12:00:00+00:00"),
    ]
    ordered = [p["barcode"] for p in sort_parcels_by_ts(parcels, "delivered_at", descending=True)]
    assert ordered == ["newest", "mid", "oldest"]


def test_sort_keeps_missing_or_garbage_timestamps_at_end():
    parcels = [
        _norm("no-ts"),
        _norm("garbage", planned_from="not-a-date"),
        _norm("early", planned_from="2026-06-13T08:00:00+00:00"),
        _norm("late", planned_from="2026-06-15T10:00:00+00:00"),
    ]
    ordered = [p["barcode"] for p in sort_parcels_by_ts(parcels, "planned_from")]
    assert ordered[:2] == ["early", "late"]
    assert set(ordered[2:]) == {"no-ts", "garbage"}


def test_sort_handles_z_suffix_timestamps():
    parcels = [
        _norm("a", planned_from="2026-06-15T10:00:00Z"),
        _norm("b", planned_from="2026-06-13T10:00:00Z"),
    ]
    ordered = [p["barcode"] for p in sort_parcels_by_ts(parcels, "planned_from")]
    assert ordered == ["b", "a"]


def test_sort_empty_input_returns_empty_list():
    assert sort_parcels_by_ts([], "planned_from") == []


# ---------------------------------------------------------------------------
# map_event_status
# ---------------------------------------------------------------------------


def test_map_event_status_known_codes():
    assert map_event_status("ENA") == ParcelStatus.REGISTERED
    assert map_event_status("ORI") == ParcelStatus.IN_TRANSIT
    assert map_event_status("SPW") == ParcelStatus.IN_TRANSIT
    assert map_event_status("SPE") == ParcelStatus.IN_TRANSIT  # Status parcel - Information (#4)
    assert map_event_status("DLO") == ParcelStatus.OUT_FOR_DELIVERY
    assert map_event_status("MSDLO") == ParcelStatus.IN_TRANSIT  # notification, not the physical OFD scan
    assert map_event_status("DEY") == ParcelStatus.DELIVERED
    assert map_event_status("DEYY") == ParcelStatus.DELIVERED


def test_map_event_status_gsmt_additions():
    """Codes added from DPD's GSMT matrix (Tier 1 + parcelshop/PUDO)."""
    # Hub / sort / scan / held / driver-return → in_transit
    for code in ("HUI", "HUS", "HUZ", "SPS", "SPV", "SPZ", "DLZ",
                 "ORW", "HUW", "DLW", "DLR"):
        assert map_event_status(code) == ParcelStatus.IN_TRANSIT, code
    # Exceptions / anomalies → problem
    for code in ("ENX", "ORX", "HUX", "SPX", "DLX", "DEX", "DODEX"):
        assert map_event_status(code) == ParcelStatus.PROBLEM, code
    # Return leg → returning
    for code in ("SPR", "DEN", "DODEN", "DODEH"):
        assert map_event_status(code) == ParcelStatus.RETURNING, code
    # Parcelshop / PUDO flow
    assert map_event_status("DEHD") == ParcelStatus.IN_TRANSIT
    assert map_event_status("DODEI") == ParcelStatus.AT_PICKUP_POINT  # ready for collection
    assert map_event_status("DODEY") == ParcelStatus.DELIVERED       # collected
    assert map_event_status("DODEYY") == ParcelStatus.DELIVERED


def test_map_event_status_none_for_missing_code():
    assert map_event_status(None) is None
    assert map_event_status("") is None


def test_map_event_status_none_for_unmapped_code(caplog):
    # Distinct code so the one-shot dedupe set does not hide the warning.
    assert map_event_status("ZZ9", "Some new event") is None
    assert "ZZ9" in caplog.text
    assert "issues/new" in caplog.text


# ---------------------------------------------------------------------------
# build_history
# ---------------------------------------------------------------------------


_EVENTS = [
    {"date": "2026-06-24", "time": "08:00:00", "eventType": "ENA", "eventTypeText": "Data received and integrated"},
    {"date": "2026-06-24", "time": "10:25:29", "eventType": "DLO", "eventTypeText": "Destination depot - Out for delivery"},
    {"date": "2026-06-24", "time": "13:09:04", "eventType": "DEY", "eventTypeText": "Delivery - Delivered"},
]


def test_build_history_entry_shape_and_order():
    history = build_history(_EVENTS)
    assert [e["status"] for e in history] == [
        ParcelStatus.REGISTERED,
        ParcelStatus.OUT_FOR_DELIVERY,
        ParcelStatus.DELIVERED,
    ]
    assert history[0]["timestamp"] == "2026-06-24T08:00:00"
    assert history[-1]["raw_status"] == "Delivery - Delivered"
    assert set(history[0]) == {"timestamp", "status", "raw_status"}


def test_build_history_sorts_unsorted_input_oldest_first():
    history = build_history(list(reversed(_EVENTS)))
    assert history[0]["status"] == ParcelStatus.REGISTERED
    assert history[-1]["status"] == ParcelStatus.DELIVERED


def test_build_history_caps_to_max_events():
    many = [
        {"date": "2026-06-24", "time": f"{hour:02d}:00:00", "eventType": "ORI", "eventTypeText": "Origin depot"}
        for hour in range(0, 24)
    ]
    history = build_history(many)
    assert len(history) == 20
    assert history[0]["timestamp"] == "2026-06-24T04:00:00"


def test_build_history_respects_custom_cap():
    assert len(build_history(_EVENTS, max_events=1)) == 1


def test_build_history_unmapped_code_is_null_status():
    history = build_history([
        {"date": "2026-06-24", "time": "08:00:00", "eventType": "QQ1", "eventTypeText": "Onbekend"},
    ])
    assert history[0]["status"] is None
    assert history[0]["raw_status"] == "Onbekend"


def test_build_history_skips_entries_without_date_or_time():
    history = build_history([
        {"time": "08:00:00", "eventType": "ENA", "eventTypeText": "no date"},
        {"date": "2026-06-24", "eventType": "ENA", "eventTypeText": "no time"},
        {"date": "2026-06-24", "time": "09:00:00", "eventType": "ENA", "eventTypeText": "ok"},
    ])
    assert len(history) == 1
    assert history[0]["raw_status"] == "ok"


def test_build_history_empty_for_no_events():
    assert build_history(None) == []
    assert build_history([]) == []


# ---------------------------------------------------------------------------
# log_unknown_descriptions — feature B warning
# ---------------------------------------------------------------------------


def test_log_unknown_descriptions_warns_with_issue_link(caplog):
    _unknown_descriptions_logged.discard("WARPED")
    shipment = {"status": {"description": "WARPED", "status": 9}}
    log_unknown_descriptions([shipment])
    assert "WARPED" in caplog.text
    assert "issues/new" in caplog.text


# ---------------------------------------------------------------------------
# normalize_parcel — history field
# ---------------------------------------------------------------------------


def test_normalize_parcel_history_defaults_to_none():
    raw = shipment_sample("PARCEL_HANDED", parcel_number="01XYZ")
    assert normalize_parcel(raw)["history"] is None


def test_normalize_parcel_history_passes_through_top_level():
    raw = shipment_sample("PARCEL_HANDED", parcel_number="01XYZ")
    events = [{"timestamp": "2026-06-24T13:09:04", "status": "delivered", "raw_status": "Delivery - Delivered"}]
    normalized = normalize_parcel(raw, history=events)
    assert normalized["history"] == events
    # Top-level so it survives the aggregator's strip_raw(); not under raw.
    assert "history" not in normalized["raw"]


# ---------------------------------------------------------------------------
# _enrich_detail_cache — history wiring
# ---------------------------------------------------------------------------


_DETAIL_WITH_EVENTS = {
    "receiver": {"name": "Jane Doe"},
    "weight": 2.0,
    "dimensions": None,
    "parcelEvents": _EVENTS,
}


async def test_enrich_detail_cache_builds_history_when_option_on(hass):
    client = MagicMock()
    client.async_get_parcel_detail = AsyncMock(return_value=_DETAIL_WITH_EVENTS)
    coordinator = DpdCoordinator(hass, client, _mock_entry(include_history=True))

    shipment = shipment_sample("PARCEL_OUT_FOR_DELIVERY", parcel_number="01ABC")
    await coordinator._enrich_detail_cache([shipment], [])

    entry = coordinator._detail_cache["01ABC"]
    assert entry["history"][-1]["status"] == ParcelStatus.DELIVERED
    assert entry["_status_description"] == "PARCEL_OUT_FOR_DELIVERY"


async def test_enrich_detail_cache_no_history_when_option_off(hass):
    client = MagicMock()
    client.async_get_parcel_detail = AsyncMock(return_value=_DETAIL_WITH_EVENTS)
    coordinator = DpdCoordinator(hass, client, _mock_entry(include_history=False))

    shipment = shipment_sample("PARCEL_OUT_FOR_DELIVERY", parcel_number="01ABC")
    await coordinator._enrich_detail_cache([shipment], [])

    assert coordinator._detail_cache["01ABC"]["history"] is None


async def test_enrich_detail_cache_refetches_on_status_change_when_history_on(hass):
    client = MagicMock()
    client.async_get_parcel_detail = AsyncMock(return_value=_DETAIL_WITH_EVENTS)
    coordinator = DpdCoordinator(hass, client, _mock_entry(include_history=True))
    coordinator._detail_cache = {
        "01ABC": {
            "receiver_name": "Jane Doe",
            "weight": 2.0,
            "dimensions": None,
            "history": [],
            "_status_description": "IN_TRANSIT",
        }
    }

    # Status has moved IN_TRANSIT -> PARCEL_OUT_FOR_DELIVERY → refetch to grow history.
    shipment = shipment_sample("PARCEL_OUT_FOR_DELIVERY", parcel_number="01ABC")
    await coordinator._enrich_detail_cache([shipment], [])

    client.async_get_parcel_detail.assert_awaited_once()
    assert coordinator._detail_cache["01ABC"]["_status_description"] == "PARCEL_OUT_FOR_DELIVERY"


async def test_enrich_detail_cache_no_refetch_when_status_unchanged(hass):
    client = MagicMock()
    client.async_get_parcel_detail = AsyncMock(return_value=_DETAIL_WITH_EVENTS)
    coordinator = DpdCoordinator(hass, client, _mock_entry(include_history=True))
    coordinator._detail_cache = {
        "01ABC": {
            "receiver_name": "Jane Doe",
            "weight": 2.0,
            "dimensions": None,
            "history": [],
            "_status_description": "PARCEL_OUT_FOR_DELIVERY",
        }
    }

    shipment = shipment_sample("PARCEL_OUT_FOR_DELIVERY", parcel_number="01ABC")
    await coordinator._enrich_detail_cache([shipment], [])

    client.async_get_parcel_detail.assert_not_called()


async def test_enrich_detail_cache_no_refetch_with_history_off_even_on_status_change(hass):
    client = MagicMock()
    client.async_get_parcel_detail = AsyncMock(return_value=_DETAIL_WITH_EVENTS)
    coordinator = DpdCoordinator(hass, client, _mock_entry(include_history=False))
    coordinator._detail_cache = {
        "01ABC": {
            "receiver_name": "Jane Doe",
            "weight": 2.0,
            "dimensions": None,
            "history": None,
            "_status_description": "IN_TRANSIT",
        }
    }

    # Even though status moved, history is off → immutable fields, never refetch.
    shipment = shipment_sample("PARCEL_OUT_FOR_DELIVERY", parcel_number="01ABC")
    await coordinator._enrich_detail_cache([shipment], [])

    client.async_get_parcel_detail.assert_not_called()


# ---------------------------------------------------------------------------
# _device_id — resolved from the device registry, cached, attached to events
# ---------------------------------------------------------------------------


async def test_device_id_resolves_and_caches(hass):
    """_device_id finds the account's device and caches it for later events."""
    from homeassistant.helpers import device_registry as dr
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    from custom_components.dpd.const import DOMAIN

    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="DPD-NL:user@example.com",
        data={},
        options={
            CONF_DELIVERED_FILTER_TYPE: "days",
            CONF_DELIVERED_FILTER_AMOUNT: 7,
        },
    )
    entry.add_to_hass(hass)

    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, entry.entry_id)},
    )

    coordinator = DpdCoordinator(hass, MagicMock(), entry)

    assert coordinator._device_id() == device.id
    # Second call returns the cached value (no second registry lookup).
    assert coordinator._device_id() == device.id


async def test_device_id_none_when_no_device(hass):
    """_device_id stays None until a device has been registered."""
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    from custom_components.dpd.const import DOMAIN

    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="DPD-NL:nobody@example.com",
        data={},
        options={
            CONF_DELIVERED_FILTER_TYPE: "days",
            CONF_DELIVERED_FILTER_AMOUNT: 7,
        },
    )
    entry.add_to_hass(hass)

    coordinator = DpdCoordinator(hass, MagicMock(), entry)
    assert coordinator._device_id() is None
