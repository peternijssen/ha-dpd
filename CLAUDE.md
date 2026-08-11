# Working in this repository

Home Assistant custom integration for DPD parcel tracking. Distributed via HACS;
not part of HA core. **Silver** quality tier, minimum HA `2024.7.0`. No DTO layer.

## Shared conventions — fetch when relevant

Suite-wide rules live in
[`.github/CONVENTIONS.md`](https://github.com/ha-parcel-integrations/.github/blob/main/CONVENTIONS.md)
and are **not** repeated here. Don't fetch it every session — fetch it **before**
you act in one of these areas:

| Before you … | Fetch `CONVENTIONS.md` § |
|---|---|
| touch entities, sensors, config/options flow, coordinator, diagnostics, translations | *Home Assistant developer docs* (its table points on to the canonical HA page — don't rely on memory) |
| add/rename a parcel field, a `ParcelStatus`, or a bus event; change first-refresh or unmapped-status logging | *Parcel contract* (this repo implements it; below is only where DPD deviates) |
| consider "fixing" a lint/pattern the skill flags (poll interval, inline client) | *Deliberate skill divergences* — likely intentional, don't re-flag |
| commit, bump, tag, release, or write release notes; add a feature without a test | *Workflow / Commits / Versioning / Testing* |

**API mechanics live in `carrier-research/api/dpd/` (private research repo)** — the Keycloak
auth flow (`auth.md`), the parcels/detail endpoints + status-description and 68-code
GSMT event vocabulary (`parcels.md`), and the FMP delivery-window fetch (`fmp.md`).
Do not duplicate them here.

**Suite-wide tripwire, kept inline on purpose:** the first refresh runs in
`__init__.py` *before* `async_forward_entry_setups`, never in a platform — from a
forwarded platform HA can't catch `ConfigEntryNotReady` and half-sets-up the
entry. Runtime-only; the tests don't catch a regression here.

## Load-bearing DPD decisions — do not refactor away

**Auth & setup**
- **Auth-tier 5xx → `ConfigEntryNotReady`**: when Keycloak returns a non-JSON 5xx
  page, `api.py` raises `DpdApiError(status_code)` before parsing; `__init__.py`
  maps it to `ConfigEntryNotReady` (retry with backoff) instead of crashing on a
  `JSONDecodeError` or forcing reauth.
- **Reauth** uses `async_update_reload_and_abort`; the confirm step guards with
  `async_set_unique_id` + `_abort_if_unique_id_mismatch` so a *different* account's
  credentials abort instead of rebinding.
- **Options flow** has no `entry.add_update_listener` — `async_schedule_reload` on
  submit. `CONF_REFRESH_INTERVAL` = 15/30/60/120/240 min, default 30.
- `aiohttp.ClientError` is not caught in the coordinator (wrapped automatically).
  Config: `ConfigEntry.runtime_data` (`DpdData`), `PARALLEL_UPDATES = 0`,
  coordinator takes `config_entry=entry`.

**Business unit**
- **`DPD-NL`, `DPD-DE`, `DPD-CH`** are mapped in `BUSINESS_UNITS`. DE/CH were
  added 2026-08-11 **without a captured payload** — see `dpd.md` and
  `country-expansion.md` in the private `carrier-research` repo (org
  discussion [#11](https://github.com/ha-parcel-integrations/.github/discussions/11)
  is the DE demand signal, with an occasional tester lined up). The decision
  to ship past that gate mirrors
  `ha-gls`'s Germany release: an unauth probe confirmed DE/CH's `myDPD` web
  login redirects through the *same* Keycloak realm and `client_id` as NL
  (`login.dpdgroup.com/auth/realms/login`), and `auth.md`'s step 3
  (`consignee-sso?bu=<BU>`) was already designed to take a `bu` value — but
  nobody has confirmed the `v7/parcels` list/detail payload shape for a
  non-NL account. Treat any DE/CH field mismatch as expected until a real
  capture arrives; `KNOWN_DESCRIPTIONS`'s existing one-shot
  `UNKNOWN`+`WARNING` fallback (see *Status & pickup* below) is the safety
  net, not new code written for this.
- `_tracking_url` derives its country segment from the account's `bu`
  (`DPD-DE` → `/de/`) rather than hardcoding `/nl/` — **no per-BU mapping
  table needed** as long as a new BU's value keeps the `DPD-<CC>` shape.
  Same for `api.py`'s parcel-detail `businessUnit` param (previously double
  chevron-prefixed as `DPD-DPD-NL`, unnoticed because detail-call failures
  are swallowed — fixed alongside DE/CH since a wrong param would have hit
  every non-NL account immediately).
- The user step's `description` still links a pre-filled "Add country" issue
  for anything beyond NL/DE/CH.

**Status & pickup**
- The raw description lives on `raw_status`, never `status`; unmapped →
  `ParcelStatus.UNKNOWN` + one-shot WARNING (`_unknown_descriptions_logged` /
  `_unknown_event_types_logged`). `KNOWN_DESCRIPTIONS` and `_DESCRIPTION_MAP` both
  need updating on a new DPD lifecycle stage.
- **ParcelShop sensors**: `DpdEnRouteToParcelShopSensor` counts `pickup` parcels
  with `status != at_pickup_point`; `DpdAwaitingPickupSensor` counts
  `status == at_pickup_point` — confirm against a real parcelshop parcel if one
  appears.

**Detail cache & FMP (cost control)**
- **`_detail_cache`** (keyed by barcode, integration-lifetime) lazily fills
  `receiver` / `weight` / `dimensions` — at most one detail call per parcel. A
  **failed** call is cached (not retried every poll) and retried once the parcel's
  status moves — one hiccup must not mean missing data until restart.
- **FMP delivery-window fetch is best-effort** (any failure → `None`, poll
  continues); `planned_from`/`planned_to` reflect the FMP hour window when present,
  else the calendar-day window in the parcel's local tz.

**History (opt-in, default OFF — `CONF_INCLUDE_HISTORY`)**
- **No new endpoint** — reuse the detail call. With the option on, the cache stores
  the status and refetches detail when a barcode's status moves (history grows on a
  status change); with it off the cache is never refetched. **Do not collapse back
  into "fetch once, forever".** History reuses the parcel maps (we map only the
  consumer-realistic subset of DPD's event codes — see `carrier-research/api/dpd/`).

**Outgoing (own shipments + returns) & events**
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

**Entities & surfaces**
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

## Planned / skipped

- **Planned (next major)**: exception translations (`UpdateFailed` f-strings →
  `translation_key` + placeholders); populated `pickup_point` — blocked on DPD
  exposing the ParcelShop name/address (needs a real parcelshop parcel).

## Running tests

```
python -m pytest tests/ --cov=custom_components.dpd
```

Coverage must stay **above 95%** (silver `test-coverage` rule). Run before
committing.
