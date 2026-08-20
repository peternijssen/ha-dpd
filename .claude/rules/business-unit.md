---
description: "Business-unit (BU) handling: which DPD BUs share the NL Keycloak backend, DE/UK's special routing, and country/tracking-URL overrides"
paths:
  - "custom_components/dpd/const.py"
  - "custom_components/dpd/config_flow.py"
  - "custom_components/dpd/api.py"
  - "custom_components/dpd/translations/*.json"
---

# Business Unit

- **`BUSINESS_UNITS`** holds NL plus 14 more BUs (`DPD-AR`, `DPD-BE`,
  `DPD-HR`, `DPD-CZ`, `DPD-EE`, `DPD-FR`, `DPD-HU`, `BRT`, `DPD-LV`,
  `DPD-LT`, `DPD-LU`, `CHR-PT`, `DPD-SK`, `DPD-SI`) confirmed (2026-08-13) to
  share NL's account backend/auth by **two independent lines of evidence** —
  the myDPD web preferences dropdown *and* the shared myDPD Android app's own
  embedded BU list — see `dpd.md` (Log, 2026-08-11 entries) in the private
  `carrier-research` repo. A non-NL `v7/parcels` list/detail payload shape is
  still **unconfirmed by a live capture**; treat any field mismatch as
  expected until one arrives. `KNOWN_DESCRIPTIONS`'s existing one-shot
  `UNKNOWN`+`WARNING` fallback (see the *parcel-core* rule) is the safety
  net, not new code written for this.
- **`DPD-CH` is in `BUSINESS_UNITS`, confirmed (2026-08-17) by the maintainer's
  own real, separately-registered mydpd.ch account** — logs in and lists
  parcels through the same shared myDPD backend as NL/UK, not the
  possibly-separate dedicated Swiss app that earlier left it deliberately out
  (see `dpd.md`'s Log). Unlike `DPD-DE`, Switzerland turned out to share NL's
  infra rather than run its own stack — do not conflate the two when reasoning
  about a future country. Plain `DPD-<CC>` shape, so no `BU_API_OVERRIDES` /
  `BU_COUNTRY_OVERRIDES` / `BU_TRACKING_URL_OVERRIDES` entry needed (`ch`
  falls out of the default derivation, matching the mobileSlider asset URL
  under `dpdgroup.com/ch/mydpd/`). A non-NL `v7/parcels` payload shape is
  still unconfirmed by a live capture, same caveat as the 14 above.
- **`DPD-DE` is not a `BUSINESS_UNITS` entry — it runs its own build.** DE
  briefly shipped in 2.8.0 as a blind pre-release, then was confirmed
  (2026-08-11, live probe) to run on a wholly separate stack —
  `api.paketnavigator.de`, an ASP.NET SOAP web service, no shared Keycloak
  realm — so it cannot work through `api.py` at any BU value. It now has its
  own module (see the *germany* rule) instead. The BU dropdown still carries a
  `DPD-DE` *option value* (`_DE_BU_VALUE` in `config_flow.py`) purely to
  route the same form into that separate path — it is never added to
  `BUSINESS_UNITS` or sent to `api.py`.
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
