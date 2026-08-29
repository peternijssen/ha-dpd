# Working in this repository

Home Assistant custom integration for DPD parcel tracking. Distributed via HACS;
not part of HA core. **Silver** quality tier, minimum HA `2024.12.0`. No DTO layer.

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

**API mechanics live in `carrier-research/dpd/api/` (private research repo)** — the Keycloak
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

**Business unit** — `BUSINESS_UNITS` in `const.py` holds NL plus 14 more BUs
(confirmed 2026-08-13 to share NL's Keycloak backend) plus `DPD-CH` (confirmed
2026-08-17). `DPD-DE` is deliberately **not** in `BUSINESS_UNITS` — it runs its
own SOAP stack, routed via `_DE_BU_VALUE` in `config_flow.py` (see *Germany*
below). `DPD-UK` rides on `DPD-NL` under the hood via `BU_API_OVERRIDES` in
`const.py`, with its own live tracking-URL resolution against a keyless
`apis.track.dpd.co.uk` endpoint. `BU_COUNTRY_OVERRIDES` and
`BU_TRACKING_URL_OVERRIDES` in `const.py` handle the BUs whose country or
tracking domain doesn't follow the default template (`CHR-PT`→`pt`, `BRT`→
`mybrt.it`). BU selector option values are lower-case (hassfest requirement)
and `.upper()`'d immediately in `config_flow.py`; a new BU needs an entry in
`const.py` **and** in every `translations/<lang>.json`'s `selector.bu.options`.
Full history and evidence trail: [.claude/rules/business-unit.md](.claude/rules/business-unit.md).

**Germany (`countries/de/`)** — wholly separate transport isolated in its own
package: `countries/de/session.py` owns the SOAP session (double-wrapped
envelope, two-stage login discovered via a third-party client capture, not
the APK decompile `BUILD_PLAN_DE.md` was built from); `countries/de/__init__.py`
owns derivation-first status mapping (`map_parcel_status_de`, no closed
`StatusID` vocabulary) and `normalize_parcel_de`. `DpdCoordinator` dispatches
on whether a `DpdDeSession` was constructed; everything past that one point
(sorting, filtering, event-firing) is shared with the general path. As of
2026-08-17 only login + an empty inbox are wire-confirmed on a real account —
treat every mapped status/slot as provisional. Full envelope shape, error-code
handling, and hardware-ID persistence: [.claude/rules/germany.md](.claude/rules/germany.md).
`normalize_parcel_de` never populates `url` (DE exposes no tracking-page
link) though it does populate weight/dimensions/delivery_window/pickup_point
— `const.py`'s `CAPABILITIES_BY_VARIANT["Germany"]` reflects exactly that gap
against `["Other"]`'s full set; keep the two in lockstep with any change to
either normalize function (2026-08-23, replacing the single flat
`CAPABILITIES` that used to overclaim `url` for DE).

**Parcel core (status/pickup, detail cache, history, outgoing, entities)** —
unmapped `raw_status` falls to `ParcelStatus.UNKNOWN` with a one-shot WARNING;
`pickup_point` is populated by repurposing the detail call's `receiver.name`
for `PARCELSHOP` deliveries (confirmed live 2026-08-20 against a real DPD-CZ
AlzaBox parcel). DE derives the same string shape via `_address_name()` on
`DeliveryParcelShop.ParcelShop` — not yet wire-confirmed on a real PUDO
delivery, unlike the general path. `_detail_cache` is
barcode-keyed and integration-lifetime (at most one detail call per parcel,
retried only once status moves); FMP delivery-window fetch is best-effort.
Outgoing parcels come from DPD's own `sendingShipments` split (no `isReturn`
filtering needed, unlike DHL); events fire over active+delivered for incoming
and `outgoing_active`+`outgoing_delivered` for outgoing, with no outgoing
`registered`/`delivery_time_changed`. Per-parcel sensors self-remove via the
summary sensor, not individually, to avoid a ghost race; setup cleanup is
sensor-domain-scoped and `non_parcel_unique_ids` must list every non-parcel
`{entry_id}_*` sensor. Full detail: [.claude/rules/parcel-core.md](.claude/rules/parcel-core.md).

## Planned / skipped

- **Planned (next major)**: exception translations (`UpdateFailed` f-strings →
  `translation_key` + placeholders).
- **Shipped (2026-08-20)**: `pickup_point` — see *Status & pickup* above.

## Running tests

```
python -m pytest tests/ --cov=custom_components.dpd
```

Coverage must stay **above 95%** (silver `test-coverage` rule). Run before
committing.
