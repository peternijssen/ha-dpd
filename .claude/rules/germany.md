---
description: "DPD Germany's separate SOAP transport, two-stage login, envelope shape, and derivation-first status mapping"
paths:
  - "custom_components/dpd/countries/de/**"
---

# Germany (`countries/de/`)

- **Wholly separate transport, isolated in its own package.** `countries/de/
  session.py` owns the SOAP session (envelope, signing, login lifecycle);
  `countries/de/__init__.py` owns status derivation + `normalize_parcel_de`.
  Neither `api.py` nor `coordinator.py`'s general fetch path is touched —
  `DpdCoordinator` dispatches on whether `de_session` was passed
  (`__init__.py` constructs a `DpdDeSession` instead of a `DpdApiClient` when
  `CONF_COUNTRY == COUNTRY_DE`), everything past that one dispatch point
  (sorting, filtering, event-firing) is shared with the general path.
- **Two-stage login, not documented anywhere DPD publishes** — discovered by
  capturing a working client against a real account, not from the decompiled
  app the first notes were based on. An anonymous `getSessionFullState` (empty `SessionToken`)
  bootstraps a throwaway `SessionToken`; `getUserLogin` exchanges that plus
  credentials for the real account `SessionToken` + `cloudUserID`.
  `async_login()` always runs both steps — there is no one-stage path.
- **The SOAP envelope double-wraps every call**: a bare `<methodName>`
  element around an inner `<methodNameRequest>` element that holds the
  fields (`_build_envelope`); responses unwrap the same way
  (`<methodNameResponse><methodNameResult>…`, case-sensitive,
  lowercase-first). Getting either wrapper wrong doesn't 400 — the server
  NullReferenceExceptions into a generic HTTP 500 SOAP fault, which
  `_async_raw_call` detects via `"faultstring" in body` and raises
  `DpdApiError` (not `DpdAuthError` — a fault is a shape bug, not a rejected
  login, and must not push a user into reauth).
  `KeyPhase` is signed the same way (`compute_key_phase`; minute-derived
  MD5, see the docstring) but was never the actual bug.
- **Failure detection reads `Ack` + a top-level singular `ErrorCode`**, not
  the decompiled `ErrorDataList[]` shape alone — real
  rejections use both; `_error_codes()` unions them. `async_get_parcels()` /
  `async_call()` reauth **once** on `ERROR_SESSION_NOT_VALID` /
  `ERROR_KEYPHASE`, never loop; two consecutive `ERROR_KEYPHASE` responses
  warn once (`_warn_keyphase_rotation_once`) since that likely means DPD
  rotated the partner secret, not routine expiry.
- **Status is derivation-first, not enum-first** — `StatusID` has no closed
  vocabulary, so `map_parcel_status_de` falls through `ParcelFlowTypeID` →
  `DeliveryParcelShop.ParcelStatus` → `LastStatusInfo.StatusID` →
  `StatusInfoContainer` slots (deepest-reached-first) →
  `isDelayed`/`showWarning` → `UNKNOWN`, one-shot-warning at the first
  unmapped point. As of 2026-08-17 only login + an empty inbox have been
  wire-confirmed on a real account — the status vocabulary itself is still
  unexercised; treat every mapped `StatusID`/slot as provisional until a real
  parcel confirms it.
- **`CONF_DE_HARDWARE_ID` is persisted in `entry.data`**, generated once at
  config-flow time (`uuid4()`), and reused on every restart
  (`__init__.py` falls back to a fresh one only if it's somehow missing) — a
  new id every setup would look like a different device to DPD on every
  reload.
- **No BU, no FMP, no per-parcel detail endpoint, no UK-style tracking
  lookup** — `async_get_all_parcels_de` does all enrichment (incl. opt-in
  history via `getTrackingScanList`) before `_async_fetch_de` ever sees the
  parcels, so DE's coordinator path filters an already-normalized list
  (`_apply_delivered_filter_canonical` in `parcels.py`, reading
  `delivered_at` off the normalized shape, not `_apply_delivered_filter`'s
  raw-payload shape).
- Debug logging (`_LOGGER.debug`, gated behind `isEnabledFor(DEBUG)`) in
  `_async_fetch_de` mirrors the general path's shipment-count + raw-payload
  summary — added after the initial build shipped with none, which left no
  way to confirm a successful DE poll actually fetched anything.
