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
- **`BUSINESS_UNITS`** holds NL plus 14 more BUs (`DPD-AR`, `DPD-BE`,
  `DPD-HR`, `DPD-CZ`, `DPD-EE`, `DPD-FR`, `DPD-HU`, `BRT`, `DPD-LV`,
  `DPD-LT`, `DPD-LU`, `CHR-PT`, `DPD-SK`, `DPD-SI`) confirmed (2026-08-13) to
  share NL's account backend/auth by **two independent lines of evidence** —
  the myDPD web preferences dropdown *and* the shared myDPD Android app's own
  embedded BU list — see `dpd.md` (Log, 2026-08-11 entries) in the private
  `carrier-research` repo. A non-NL `v7/parcels` list/detail payload shape is
  still **unconfirmed by a live capture**; treat any field mismatch as
  expected until one arrives. `KNOWN_DESCRIPTIONS`'s existing one-shot
  `UNKNOWN`+`WARNING` fallback (see *Status & pickup* below) is the safety
  net, not new code written for this.
- **`DPD-DE` and `DPD-CH` are deliberately absent.** DE briefly shipped in
  2.8.0 as a blind pre-release, then was confirmed (2026-08-11, live probe
  with a real DE account) to run on a wholly separate stack —
  `portal.dpd.de`, ASP.NET WebForms, no shared Keycloak realm, same shape as
  Poland — and cannot work through this repo's `api.py` at any BU value; it
  needs its own build like `dpd-pl` would. CH looked plausible (present in
  both the dropdown and the app's BU list) but the maintainer found evidence
  of a possibly-separate dedicated Swiss app too — unresolved, so it stays
  out until it gets its own individual investigation, same as PL/DE.
  **Do not re-add either without a fresh capture.**
- **`DPD-UK` is present, but riding on `DPD-NL` under the hood — not a real
  business-unit code.** `carrier-research/dpd/dpd-uk.md` static-teardown'd
  the UK *mobile app* as its own Firebase stack, separate from myDPD/Keycloak
  — that finding still stands. But `github.com/ha-parcel-integrations/.github/
  discussions/14` (2026-08-16/17) confirmed live that a UK account logs in
  and lists its own parcels through the plain NL *web* myDPD flow (needed a
  password reset first, since the account had no password — SSO-only). The
  UK-GB/DPD-UK business unit itself doesn't exist anywhere on the shared
  backend (absent from both the preferences dropdown and the myDPD app's own
  BU list, see `dpd-log.md` 2026-08-11) — so **`BU_API_OVERRIDES` in
  `const.py`** remaps `DPD-UK` → `DPD-NL` for every wire call
  (Keycloak/consignee-sso/parcels/detail) in `api.py`, via a
  `self._request_bu` distinct from `self._bu` — `client.bu` keeps the
  configured `DPD-UK` for `unique_id` and the tracking-URL fallback.
- **`DPD-UK`'s tracking URL is resolved live, not derived.** DPD UK's real
  self-service tracker, `track.dpd.co.uk`, lives on a *third* separate host
  again (`apis.track.dpd.co.uk`) from both myDPD and the app — but its
  `GET /v1/reference?referenceNumber=<parcelNumber>&postcode=&origin=PRTK`
  is **keyless**: confirmed live (2026-08-17) with no session cookie and an
  empty/wrong postcode, all returning the same result — postcode isn't
  checked at that step at all. It returns a DPD-assigned
  `data[0].parcelCode` (``<14-digit-number>*<sequence>``, the sequence not a
  postcode formula) that `track.dpd.co.uk/parcels/<parcelCode>` expects.
  Only the *next* step, `/login`, needs reCAPTCHA — `ha-dpd` never calls it.
  `DpdApiClient.async_get_uk_tracking_code` makes that one call;
  `DpdCoordinator._enrich_uk_tracking_cache` caches the result per barcode
  for the integration's lifetime (mirrors `_detail_cache`: never refetched,
  a failure isn't retried either — the code cannot change once assigned).
  `normalize_parcel(..., uk_tracking_code=...)` uses it when present;
  `BU_COUNTRY_OVERRIDES["DPD-UK"] = "nl"` is the fallback for a barcode that
  hasn't resolved yet or failed, not the primary link. See
  `carrier-research/dpd/dpd-uk.md`'s "public web tracker" surface for the
  full reverse-engineering trail.
- `_tracking_url` derives its country segment from the account's `bu`
  (`DPD-DE` → `/de/`) rather than hardcoding `/nl/` — **`BU_COUNTRY_OVERRIDES`
  in `const.py`** handles the one BU whose code doesn't map to its country
  the obvious way (`CHR-PT` → `pt`). Same for `api.py`'s parcel-detail
  `businessUnit` param (previously double chevron-prefixed as
  `DPD-DPD-NL`, unnoticed because detail-call failures are swallowed).
- **`BU_TRACKING_URL_OVERRIDES` in `const.py`** handles BUs whose tracking
  page isn't `dpdgroup.com/<country>/mydpd/...` at all — confirmed
  (2026-08-13, live link check) for `BRT` (Italy): the acquired BRT brand
  lives entirely on `mybrt.it`, not dpdgroup.com; `dpdgroup.com/it/mybrt/...`
  404s, so this needed a full URL override, not just a brand-segment tweak.
  Check any newly-added acquired-brand BU (not a plain `DPD-<CC>` code)
  against a real tracking link before assuming the default template works.
- **The BU selector's option values are lower-case** (`dpd-nl`, not
  `DPD-NL`) because they double as translation keys and hassfest requires
  `[a-z0-9-_]+` with no upper-case — the same rule that bit `ha-gls`'s
  country selector. `async_step_user` immediately `.upper()`s the submitted
  value before use/storage; the stored/internal `bu` value everywhere else
  (API calls, `unique_id`, `entry.data`) stays upper-case, unchanged. Don't
  "simplify" this back to a shared-case value — the upper-case form is what
  DPD's API expects.
- **Every supported country's language has its own translation file**
  (`translations/<lang>.json`), including a translated `selector.bu.options`
  block for the dropdown itself — not just an English/Dutch/German UI with
  translated labels tacked onto foreign BUs. A new BU therefore needs its
  option added to **all** translation files' `selector.bu.options`, not just
  `const.py`'s `BUSINESS_UNITS` — verify with the structural key-parity
  check (compare every file's flattened key set against `en.json`) before
  committing, the same way `dpd.md`'s in-repo history caught a bad
  interleave once already.
- The user step's `description` still links a pre-filled "Add country" issue
  for anything beyond the supported list.

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
