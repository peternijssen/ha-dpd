"""Coordinator for the DPD integration."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import DpdApiClient, DpdApiError, DpdAuthError
from .const import (
    CONF_INCLUDE_HISTORY,
    CONF_REFRESH_INTERVAL,
    DEFAULT_INCLUDE_HISTORY,
    DEFAULT_REFRESH_INTERVAL,
    DOMAIN,
    ParcelStatus,
)
from .countries.de import async_get_all_parcels_de
from .countries.de.session import DpdDeSession
from .parcels import (
    _apply_delivered_filter,
    _apply_delivered_filter_canonical,
    _augment_dimensions,
    _description,
    build_history,
    filter_active_shipments,
    filter_delivered_shipments,
    fmp_hashcode,
    log_unknown_descriptions,
    normalize_parcel,
    sort_parcels_by_ts,
)

_LOGGER = logging.getLogger(__name__)


def _refresh_interval(entry: ConfigEntry) -> timedelta:
    """Return the configured refresh interval as a ``timedelta``."""
    minutes = int(entry.options.get(CONF_REFRESH_INTERVAL, DEFAULT_REFRESH_INTERVAL))
    return timedelta(minutes=minutes)


class DpdCoordinator(DataUpdateCoordinator[dict[str, list[dict]]]):
    """Coordinator that polls the DPD parcels API on a fixed schedule.

    Dispatches the fetch to the general/NL+BU backend or DPD Germany's SOAP
    backend, based on whether ``de_session`` was passed — everything past
    that one dispatch point (sorting, filtering, event-firing) is shared.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        client: DpdApiClient | None,
        entry: ConfigEntry,
        *,
        de_session: DpdDeSession | None = None,
    ) -> None:
        """Initialize the coordinator.

        ``client`` is the general/NL+BU transport; ``de_session`` is DPD
        Germany's SOAP session. Exactly one of the two is used per hub —
        ``__init__.py`` constructs the right one for the hub's country.
        """
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=DOMAIN,
            update_interval=_refresh_interval(entry),
        )
        self._client = client
        self._de_session = de_session
        # barcode -> last seen ParcelStatus. ``None`` on the first refresh so
        # we can suppress events for parcels that already existed when the
        # integration started (we do not know their previous state).
        self._known_state: dict[str, ParcelStatus] | None = None
        # barcode -> last seen (planned_from, planned_to) tuple. Mirrors
        # ``_known_state`` for delivery-time-change detection.
        self._known_delivery_times: (
            dict[str, tuple[str | None, str | None]] | None
        ) = None
        # barcode -> last seen ParcelStatus for *outgoing* parcels (sent +
        # returns), tracked across the active and delivered buckets so a status
        # change or a transition-to-delivered fires an outgoing event. ``None``
        # on the first refresh for the same suppression reason as
        # ``_known_state``.
        self._known_outgoing_state: dict[str, ParcelStatus] | None = None
        # barcode -> per-parcel fields fetched from the detail endpoint
        # (``receiver_name``, ``weight``, ``dimensions``). The list endpoint
        # doesn't carry these; the detail endpoint does, but it costs an
        # extra HTTP call per parcel. None of these fields change for a
        # known parcel, so we fetch the detail once per barcode and reuse
        # the result for the integration's lifetime. A failed detail call is
        # cached as ``{"_failed": True, ...}`` so we don't hammer DPD when
        # their endpoint is flaky — retried once the parcel's status moves.
        self._detail_cache: dict[str, dict[str, Any]] = {}
        # barcode -> track.dpd.co.uk parcelCode, or ``None`` on a failed
        # lookup. UK-only (see _enrich_uk_tracking_cache); the code never
        # changes for a given parcel, so — like the detail cache above — one
        # successful (or failed) lookup per barcode is cached for the
        # integration's lifetime, never refetched.
        self._uk_tracking_cache: dict[str, str | None] = {}
        # Cached device id for this account, attached to every fired event so
        # device-trigger automations can filter to a specific DPD account.
        # ``None`` until the device exists (i.e. the sensors are set up).
        self._cached_device_id: str | None = None
        # Timestamp of the last successful poll, surfaced by a diagnostic
        # sensor so users can alert on a silently-stale integration (the
        # count sensors only change when a value changes, not every poll).
        self.last_success_time: datetime | None = None

    def _device_id(self) -> str | None:
        """Resolve (and cache) this account's device id for event payloads.

        Looked up from the device registry by config entry. Stays ``None``
        until the device has been registered (the sensors create it on first
        setup), which is harmless because events are suppressed on the very
        first refresh anyway.
        """
        if self._cached_device_id is not None:
            return self._cached_device_id
        registry = dr.async_get(self.hass)
        device = next(
            iter(dr.async_entries_for_config_entry(registry, self.config_entry.entry_id)),
            None,
        )
        if device is not None:
            self._cached_device_id = device.id
        return self._cached_device_id

    async def _async_update_data(self) -> dict[str, list[dict]]:
        if self._de_session is not None:
            incoming_all, outgoing_all = await self._async_fetch_de()
        else:
            incoming_all, outgoing_all = await self._async_fetch_general()

        self._fire_change_events(incoming_all)
        self._fire_outgoing_change_events(outgoing_all)

        self._known_state = {
            p["barcode"]: p["status"] for p in incoming_all if p.get("barcode")
        }
        self._known_delivery_times = {
            p["barcode"]: (p.get("planned_from"), p.get("planned_to"))
            for p in incoming_all
            if p.get("barcode")
        }
        self._known_outgoing_state = {
            p["barcode"]: p["status"] for p in outgoing_all if p.get("barcode")
        }

        self.last_success_time = datetime.now(timezone.utc)
        return {
            "incoming_active": [p for p in incoming_all if not p["delivered"]],
            "incoming_delivered": [p for p in incoming_all if p["delivered"]],
            "outgoing_active": [p for p in outgoing_all if not p["delivered"]],
            "outgoing_delivered": [p for p in outgoing_all if p["delivered"]],
        }

    async def _async_fetch_general(self) -> tuple[list[dict], list[dict]]:
        """Fetch + normalize the general/NL+BU backend, split active/delivered.

        Returns each bucket already sorted (active ascending on
        ``planned_from``, delivered descending on ``delivered_at``) and with
        the delivered-retention filter applied, matching what
        ``_async_update_data`` expects from both fetch paths.
        """
        assert self._client is not None
        try:
            payload = await self._client.async_get_parcels()
        except DpdAuthError as err:
            _LOGGER.error("DPD authentication failed: %s", err)
            raise ConfigEntryAuthFailed("DPD authentication failed") from err
        except DpdApiError as err:
            _LOGGER.warning("DPD parcels endpoint unreachable: %s", err)
            raise UpdateFailed(f"DPD error: {err}") from err
        # aiohttp.ClientError is wrapped automatically by DataUpdateCoordinator.

        incoming = payload.get("incomingShipments") or []
        outgoing = payload.get("sendingShipments") or []

        log_unknown_descriptions(incoming + outgoing)

        incoming_active = filter_active_shipments(incoming)
        incoming_delivered = self._apply_delivered_filter(
            filter_delivered_shipments(incoming)
        )
        outgoing_active = filter_active_shipments(outgoing)
        outgoing_delivered = self._apply_delivered_filter(
            filter_delivered_shipments(outgoing)
        )

        await self._enrich_with_fmp(incoming_active)
        await self._enrich_detail_cache(
            incoming_active + incoming_delivered,
            outgoing_active + outgoing_delivered,
        )
        if self._client.bu == "DPD-UK":
            await self._enrich_uk_tracking_cache(
                incoming_active + incoming_delivered
                + outgoing_active + outgoing_delivered
            )

        _LOGGER.debug(
            "DPD shipments fetched: %d incoming (%d active, %d delivered shown), "
            "%d outgoing (%d active, %d delivered shown)",
            len(incoming),
            len(incoming_active),
            len(incoming_delivered),
            len(outgoing),
            len(outgoing_active),
            len(outgoing_delivered),
        )
        if incoming or outgoing:
            _LOGGER.debug("DPD raw parcels payload: %s", payload)

        def _normalize(parcel: dict) -> dict:
            cached = self._detail_cache.get(parcel.get("parcelNumber") or "") or {}
            weight = cached.get("weight")
            dimensions = cached.get("dimensions")
            history = cached.get("history")
            # Mirror weight + dimensions back onto the raw payload too so
            # ``state_attr(..., 'raw').weight`` / ``.dimensions`` works for
            # users who want the native shape under raw rather than the
            # carrier-agnostic top-level keys. The list endpoint never
            # populates these, so adding them is non-destructive.
            if weight is not None and "weight" not in parcel:
                parcel["weight"] = weight
            if dimensions is not None and "dimensions" not in parcel:
                parcel["dimensions"] = dimensions
            return normalize_parcel(
                parcel,
                receiver=cached.get("receiver_name"),
                weight=weight,
                dimensions=dimensions,
                history=history,
                bu=self._client.bu,
                uk_tracking_code=self._uk_tracking_cache.get(
                    parcel.get("parcelNumber") or ""
                ),
            )

        normalized_active = sort_parcels_by_ts(
            [_normalize(p) for p in incoming_active], "planned_from",
        )
        normalized_delivered = sort_parcels_by_ts(
            [_normalize(p) for p in incoming_delivered],
            "delivered_at",
            descending=True,
        )
        normalized_outgoing = sort_parcels_by_ts(
            [_normalize(p) for p in outgoing_active], "planned_from",
        )
        normalized_outgoing_delivered = sort_parcels_by_ts(
            [_normalize(p) for p in outgoing_delivered],
            "delivered_at",
            descending=True,
        )

        # Incoming = active + delivered, combined so the transition to
        # delivered is visible in one set (mirrors the outgoing pattern).
        incoming_all = normalized_active + normalized_delivered
        # Outgoing = active + delivered sent shipments, combined so a hop from
        # in-transit to delivered is visible in one set.
        outgoing_all = normalized_outgoing + normalized_outgoing_delivered
        return incoming_all, outgoing_all

    async def _async_fetch_de(self) -> tuple[list[dict], list[dict]]:
        """Fetch + normalize DPD Germany's SOAP backend, split active/delivered.

        DE has no BU, no FMP, no per-parcel detail endpoint and no UK-style
        tracking lookup — the whole enrichment happens inside
        ``async_get_all_parcels_de`` before this returns, so the buckets it
        hands back are already fully normalized.
        """
        assert self._de_session is not None
        try:
            incoming, outgoing = await async_get_all_parcels_de(
                self._de_session, include_history=self._include_history
            )
        except DpdAuthError as err:
            _LOGGER.error("DPD Germany authentication failed: %s", err)
            raise ConfigEntryAuthFailed("DPD Germany authentication failed") from err
        except DpdApiError as err:
            _LOGGER.warning("DPD Germany endpoint unreachable: %s", err)
            raise UpdateFailed(f"DPD Germany error: {err}") from err

        incoming_active = [p for p in incoming if not p["delivered"]]
        incoming_delivered = _apply_delivered_filter_canonical(
            [p for p in incoming if p["delivered"]], self.config_entry
        )
        outgoing_active = [p for p in outgoing if not p["delivered"]]
        outgoing_delivered = _apply_delivered_filter_canonical(
            [p for p in outgoing if p["delivered"]], self.config_entry
        )

        _LOGGER.debug(
            "DPD Germany shipments fetched: %d incoming (%d active, %d delivered "
            "shown), %d outgoing (%d active, %d delivered shown)",
            len(incoming),
            len(incoming_active),
            len(incoming_delivered),
            len(outgoing),
            len(outgoing_active),
            len(outgoing_delivered),
        )
        if incoming or outgoing:
            _LOGGER.debug(
                "DPD Germany raw parcels payload: %s",
                [p["raw"] for p in incoming + outgoing],
            )

        normalized_active = sort_parcels_by_ts(incoming_active, "planned_from")
        normalized_delivered = sort_parcels_by_ts(
            incoming_delivered, "delivered_at", descending=True
        )
        normalized_outgoing = sort_parcels_by_ts(outgoing_active, "planned_from")
        normalized_outgoing_delivered = sort_parcels_by_ts(
            outgoing_delivered, "delivered_at", descending=True
        )

        incoming_all = normalized_active + normalized_delivered
        outgoing_all = normalized_outgoing + normalized_outgoing_delivered
        return incoming_all, outgoing_all

    def _fire_change_events(self, parcels: list[dict]) -> None:
        """Fire events for newly-registered parcels and parcel transitions.

        Silent on the very first refresh — we cannot reliably know which
        parcels are "new" to the user vs. "already there before HA started".
        From the second refresh onward, every not-yet-delivered parcel that
        was not present before yields one ``dpd_parcel_registered`` event,
        every parcel whose normalised status transitions **to** ``DELIVERED``
        yields one ``dpd_parcel_delivered`` event, every other status change
        yields one ``dpd_parcel_status_changed`` event, and every parcel
        whose ``planned_from`` or ``planned_to`` changed to a non-null value
        yields one ``dpd_parcel_delivery_time_changed`` event. ``delivered``
        takes precedence over ``status_changed`` for the terminal hop, and a
        parcel first seen already-delivered fires nothing — mirroring the
        outgoing events.
        """
        if self._known_state is None:
            return

        known_times = self._known_delivery_times or {}
        device_id = self._device_id()

        for parcel in parcels:
            barcode = parcel.get("barcode")
            if not barcode:
                continue
            new_status = parcel["status"]
            if barcode not in self._known_state:
                if new_status != ParcelStatus.DELIVERED:
                    self.hass.bus.async_fire(
                        f"{DOMAIN}_parcel_registered",
                        {**parcel, "device_id": device_id},
                    )
                continue

            if self._known_state[barcode] != new_status:
                if new_status == ParcelStatus.DELIVERED:
                    self.hass.bus.async_fire(
                        f"{DOMAIN}_parcel_delivered",
                        {**parcel, "device_id": device_id},
                    )
                else:
                    self.hass.bus.async_fire(
                        f"{DOMAIN}_parcel_status_changed",
                        {
                            **parcel,
                            "device_id": device_id,
                            "old_status": self._known_state[barcode],
                            "new_status": new_status,
                        },
                    )

            old_from, old_to = known_times.get(barcode, (None, None))
            new_from = parcel.get("planned_from")
            new_to = parcel.get("planned_to")
            # Fire only when at least one of the two ends up with a real
            # (non-null) value AND that value differs from the last-known
            # one. value -> null transitions are intentionally silent —
            # they mean the carrier dropped the ETA, which is not what
            # users want to be paged about.
            from_changed = new_from is not None and new_from != old_from
            to_changed = new_to is not None and new_to != old_to
            if from_changed or to_changed:
                self.hass.bus.async_fire(
                    f"{DOMAIN}_parcel_delivery_time_changed",
                    {
                        **parcel,
                        "device_id": device_id,
                        "old_planned_from": old_from,
                        "new_planned_from": new_from,
                        "old_planned_to": old_to,
                        "new_planned_to": new_to,
                    },
                )

    def _fire_outgoing_change_events(self, parcels: list[dict]) -> None:
        """Fire status/delivered events for outgoing parcels (sent + returns).

        Silent on the very first refresh (``_known_outgoing_state is None``),
        matching ``_fire_change_events``. From the second refresh onward, every
        outgoing parcel whose normalised status transitions **to**
        ``DELIVERED`` yields one ``dpd_outgoing_parcel_delivered`` event, and
        every other status change yields one
        ``dpd_outgoing_parcel_status_changed`` event. ``delivered`` takes
        precedence over ``status_changed`` for that final transition, so the
        terminal hop fires exactly one (dedicated) event, not both. A parcel
        that is already delivered the first time it is seen never fires (its
        status did not change). There is no outgoing ``registered`` or
        ``delivery_time_changed`` event — those are intentionally out of scope.
        """
        if self._known_outgoing_state is None:
            return

        device_id = self._device_id()

        for parcel in parcels:
            barcode = parcel.get("barcode")
            if not barcode or barcode not in self._known_outgoing_state:
                continue
            old_status = self._known_outgoing_state[barcode]
            new_status = parcel["status"]
            if new_status == old_status:
                continue

            if new_status == ParcelStatus.DELIVERED:
                self.hass.bus.async_fire(
                    f"{DOMAIN}_outgoing_parcel_delivered",
                    {**parcel, "device_id": device_id},
                )
            else:
                self.hass.bus.async_fire(
                    f"{DOMAIN}_outgoing_parcel_status_changed",
                    {
                        **parcel,
                        "device_id": device_id,
                        "old_status": old_status,
                        "new_status": new_status,
                    },
                )

    async def _enrich_with_fmp(self, shipments: list[dict]) -> None:
        """In-place: add ``fmpDeliveryDateAndTime`` to shipments that expose FMP.

        Only shipments with an ``availableActions.FOLLOW_MY_PARCEL`` action
        are queried — typically those out for delivery today. Failures are
        swallowed by the API client (returns ``None``) so a broken FMP
        call never breaks the parcels poll.
        """
        assert self._client is not None
        for shipment in shipments:
            hashcode = fmp_hashcode(shipment)
            if not hashcode:
                continue
            window = await self._client.async_fmp_delivery_window(hashcode)
            if window:
                shipment["fmpDeliveryDateAndTime"] = window

    @property
    def _include_history(self) -> bool:
        """Whether the opt-in per-parcel history option is enabled."""
        return bool(
            self.config_entry.options.get(
                CONF_INCLUDE_HISTORY, DEFAULT_INCLUDE_HISTORY
            )
        )

    async def _enrich_detail_cache(
        self,
        incoming: list[dict],
        outgoing: list[dict],
    ) -> None:
        """Populate ``self._detail_cache`` for any barcode we have not seen.

        Calls the per-parcel detail endpoint once per barcode and extracts
        the fields that aren't on the list endpoint: ``receiver.name``,
        ``weight``, ``dimensions`` and — when the history option is on — the
        ``parcelEvents`` timeline. Receiver/weight/dimensions never change
        for a known parcel, so by default one call per parcel ever is enough
        and the cache lives for the integration's lifetime (it resets on HA
        restart, which then backfills active + delivered + outgoing on the
        first refresh).

        History is the exception: it **grows** on every status change, so
        when the option is on we refetch the detail for an already-cached
        barcode whenever its ``status.description`` has moved since the last
        fetch. No extra endpoint is needed — it is the same detail call.
        Failures are swallowed by the API client (returns ``None``); a
        failure is cached as ``{"_failed": True}`` so we do not retry on
        every refresh, but we do retry once the parcel's status moves.
        """
        assert self._client is not None
        include_history = self._include_history
        for shipment, parcel_type in (
            *((s, "INCOMING") for s in incoming),
            *((s, "OUTGOING") for s in outgoing),
        ):
            barcode = shipment.get("parcelNumber")
            if not barcode:
                continue
            if barcode in self._detail_cache:
                cached = self._detail_cache.get(barcode) or {}
                if cached.get("_failed"):
                    # The last detail fetch failed. Don't hammer DPD on every
                    # poll, but do try again once the parcel's status moves —
                    # otherwise one hiccup means no receiver/weight/dimensions
                    # until an HA restart.
                    if cached.get("_status_description") == _description(shipment):
                        continue
                elif not include_history:
                    # Successfully fetched and history is off: the cached
                    # fields are immutable — never refetch.
                    continue
                elif cached.get("_status_description") == _description(shipment):
                    # History on: only refetch to refresh the timeline, and
                    # only when the status has actually moved.
                    continue
            detail = await self._client.async_get_parcel_detail(
                barcode,
                shipment_bu_code=shipment.get("shipmentBUCode"),
                parcel_type=parcel_type,
            )
            if detail is None:
                self._detail_cache[barcode] = {
                    "_failed": True,
                    "_status_description": _description(shipment),
                }
                continue
            self._detail_cache[barcode] = {
                "receiver_name": ((detail.get("receiver") or {}).get("name")),
                "weight": detail.get("weight"),
                "dimensions": _augment_dimensions(detail.get("dimensions")),
                "history": (
                    build_history(detail.get("parcelEvents"))
                    if include_history
                    else None
                ),
                # Remembered so we can detect a status change and refetch the
                # growing history timeline (see above).
                "_status_description": _description(shipment),
            }

    async def _enrich_uk_tracking_cache(self, shipments: list[dict]) -> None:
        """Populate ``self._uk_tracking_cache`` for any UK barcode not yet seen.

        Only called when the account's ``bu`` is ``DPD-UK``. One keyless call
        per barcode, ever — the resolved ``parcelCode`` (or a failed lookup)
        never changes for a given parcel, unlike the detail cache's history
        option there is nothing here that would need a refetch.
        """
        assert self._client is not None
        for shipment in shipments:
            barcode = shipment.get("parcelNumber")
            if not barcode or barcode in self._uk_tracking_cache:
                continue
            self._uk_tracking_cache[barcode] = (
                await self._client.async_get_uk_tracking_code(barcode)
            )

    def _apply_delivered_filter(self, shipments: list[dict]) -> list[dict]:
        """Trim the delivered list according to the configured options."""
        return _apply_delivered_filter(shipments, self.config_entry)
