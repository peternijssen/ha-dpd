---
description: "Status/pickup mapping, detail cache & FMP cost control, opt-in history, outgoing shipments/events, and entity surfaces for the general (non-DE) path"
paths:
  - "custom_components/dpd/coordinator.py"
  - "custom_components/dpd/parcels.py"
  - "custom_components/dpd/sensor.py"
  - "custom_components/dpd/calendar.py"
  - "custom_components/dpd/button.py"
  - "custom_components/dpd/countries/general/**"
---

# Status & pickup

- The raw description lives on `raw_status`, never `status`; unmapped →
  `ParcelStatus.UNKNOWN` + one-shot WARNING (`_unknown_descriptions_logged` /
  `_unknown_event_types_logged`). `KNOWN_DESCRIPTIONS` and `_DESCRIPTION_MAP` both
  need updating on a new DPD lifecycle stage.
- **ParcelShop sensors**: `DpdEnRouteToParcelShopSensor` counts `pickup` parcels
  with `status != at_pickup_point`; `DpdAwaitingPickupSensor` counts
  `status == at_pickup_point` — confirmed against a real DPD-CZ AlzaBox
  parcel (2026-08-20, maintainer-supplied diagnostics), both counted and
  transitioned correctly end to end (`in_transit` → `at_pickup_point` →
  `delivered`).
- **`pickup_point` is populated by repurposing the detail call's
  `receiver.name`.** Confirmed live (2026-08-20, the same AlzaBox capture):
  for a `PARCELSHOP` delivery, DPD's per-parcel detail endpoint puts the
  ParcelShop's own name (branch + town) in `receiver.name` instead of a
  person — there is no separate field carrying the actual recipient in that
  case. `normalize_parcel` in `countries/general/__init__.py` reads that as
  `pickup_point` when `is_pickup` and leaves `receiver` `None` rather than
  mislabel a shop name as the recipient; for a non-pickup delivery `receiver`
  is unaffected. `CAPABILITIES` in `const.py` now includes `pickup_point`.
  `normalize_parcel_de` mirrors the same display-name shape via
  `_address_name()` on `DeliveryParcelShop.ParcelShop` — not yet
  wire-confirmed on a real PUDO delivery, unlike the general path.

# Detail cache & FMP (cost control)

- **`_detail_cache`** (keyed by barcode, integration-lifetime) lazily fills
  `receiver` / `weight` / `dimensions` — at most one detail call per parcel. A
  **failed** call is cached (not retried every poll) and retried once the parcel's
  status moves — one hiccup must not mean missing data until restart.
- **FMP delivery-window fetch is best-effort** (any failure → `None`, poll
  continues); `planned_from`/`planned_to` reflect the FMP hour window when present,
  else the calendar-day window in the parcel's local tz.

# History (opt-in, default OFF — `CONF_INCLUDE_HISTORY`)

- **No new endpoint** — reuse the detail call. With the option on, the cache stores
  the status and refetches detail when a barcode's status moves (history grows on a
  status change); with it off the cache is never refetched. **Do not collapse back
  into "fetch once, forever".** History reuses the parcel maps (we map only the
  consumer-realistic subset of DPD's event codes — see `carrier-research/api/dpd/`).

# Outgoing (own shipments + returns) & events

- DPD splits server-side into `incomingShipments` / `sendingShipments`, so a return
  the account ships back lands in `sendingShipments` and flows into the outgoing
  sensors automatically — **no `isReturn` filtering** needed here (unlike DHL).
- `_async_update_data` splits `sendingShipments` into active + delivered (via the
  shared `_apply_delivered_filter`), feeding `DpdOutgoingDeliveredParcelsSensor`.
- Incoming events run over **active + delivered** combined (terminal hop → only
  `_delivered`; `delivery_time_changed` only on a non-null `planned_*` that
  differs). Outgoing events over `outgoing_active + outgoing_delivered`; `delivered`
  wins the terminal hop; **no** outgoing `registered` / `delivery_time_changed`.
  State in `_known_state` / `_known_delivery_times` / `_known_outgoing_state`.
  `device_id` on every payload (`_cached_device_id`).

# Entities & surfaces

- `has_entity_name = True` + `translation_key`, `icons.json`, translated units —
  every summary sensor uses the single `parcels` unit. Device name `"DPD (<email>)"`;
  `_attr_attribution`; `_unrecorded_attributes` keeps parcel lists (and `history`)
  out of the recorder.
- **Per-parcel sensors are removed by the summary sensor** (the old self-remove
  raced and left ghosts). **Setup cleanup is sensor-scoped** (filter
  `domain == "sensor"`, else it deletes the button); all non-parcel `{entry_id}_*`
  sensors **must** stay in `non_parcel_unique_ids`.
- **Refresh `button`**, **diagnostic `last_update` sensor**
  (`coordinator.last_success_time`), **deliveries `calendar`** (read-only over
  `incoming_active`, no extra API calls, enabled by default).
