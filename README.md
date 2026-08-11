# DPD Parcel Tracker

[![Release](https://img.shields.io/github/v/release/ha-parcel-integrations/ha-dpd.svg)](https://github.com/ha-parcel-integrations/ha-dpd/releases)
[![HACS](https://img.shields.io/badge/HACS-Default-41BDF5.svg)](https://github.com/hacs/integration)
[![License](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

> 💬 Questions or feedback? Join the discussion on the [Home Assistant community](https://community.home-assistant.io/t/packages-postnl-dhl-nl-dpd-and-gls-parcel-integration/112433/).

A custom Home Assistant integration that tracks your DPD shipments.

## Contents

- [Features](#features)
- [Requirements](#requirements)
- [Installation](#installation)
- [Configuration](#configuration)
- [Options](#options)
- [Removal](#removal)
- [Sensors](#sensors)
- [Parcel status reference](#parcel-status-reference)
- [Events](#events)
- [Examples](#examples)
- [Debugging](#debugging)
- [Troubleshooting](#troubleshooting)
- [Related integrations](#related-integrations)
- [Disclaimer](#disclaimer)
- [Contributing](#contributing)
- [License](#license)

## Features

- Incoming and outgoing active-parcel count sensors
- Per-parcel sensor per active incoming shipment, with full status details as attributes
- Optional per-parcel status history timeline (opt-in; off by default)
- Configurable delivered-parcels sensor (last N days, or N most recent)
- Automatic lifecycle management — per-parcel sensors are created and removed as parcels move through delivery
- Re-authentication support
- Country (business unit) selection during setup — Netherlands, Germany and Switzerland available today, more to come

## Requirements

- Home Assistant 2024.7 or newer
- A DPD account (the same credentials you use in the myDPD mobile app)

## Installation

### HACS (recommended)

This integration is available in the default HACS store.

1. Open HACS, search for **DPD** and install it
2. Restart Home Assistant

Or click the button below to open it directly in HACS:

[![Open your Home Assistant instance and open a repository inside the Home Assistant Community Store.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=ha-parcel-integrations&repository=ha-dpd&category=integration)

### Manual

1. Copy the `dpd` folder into your `config/custom_components/` directory
2. Restart Home Assistant

## Configuration

1. Go to **Settings → Devices & Services → Add Integration**
2. Search for **DPD**
3. Enter your DPD **email**, **password**, and pick your **country**
4. Choose how you want the **delivered parcels** sensor to filter (last N days, or N most recent)
5. Click **Submit**

### Setup parameters

| Field | Description |
|---|---|
| Email | The email address of your DPD consumer account (the one you use in the myDPD mobile app). |
| Password | The password for that account. Stored in the HA config entry and refreshed automatically when the integration triggers a re-authentication. |
| Country | The DPD business unit to query: **Netherlands** (`DPD-NL`), **Germany** (`DPD-DE`) or **Switzerland** (`DPD-CH`). Germany and Switzerland are new and not yet confirmed against real account data — if a parcel's status ever looks wrong, please [report it](https://github.com/ha-parcel-integrations/ha-dpd/issues/new?template=unrecognised_status.yml). More countries land once contributors share parcel-payload samples. |

## Options

Click **Configure** on the integration card. The form is split into three
sections:

### Delivered parcels

| Option | Description |
|---|---|
| Filter by | `Days` keeps delivered parcels visible for the last N days. `Number of parcels` keeps only the N most recent regardless of age. |
| Amount | The N used by the filter above. |

### Parcel history

| Option | Description |
|---|---|
| Include status history | Adds a `history` attribute to each parcel — the ordered list of status updates (timestamp, canonical status, original DPD text), capped to the most recent 20. **Off by default.** The attribute is kept out of the recorder database. |

### Polling

| Option | Description |
|---|---|
| Refresh every | How often the integration checks DPD. Choices: **15 / 30 / 60 / 120 / 240 minutes** — default 30. A slower interval is gentler on DPD's consumer API. Changes take effect immediately, no HA restart needed. |

## Removal

Standard HA removal applies: **Settings → Devices & Services →
DPD → ⋮ → Delete**. No DPD-side cleanup is needed; deleting the
config entry stops the polling. To revoke API access entirely, change
your DPD account password — the integration will trigger a re-auth
notification, which you can then ignore.

## Sensors

The integration creates one device per DPD account, named
**`DPD (<your-email>)`**. With multiple accounts each gets its own device
named after its email. The entities below show the friendly-name pattern;
their entity_ids carry the same account suffix:

| Friendly name pattern | Description |
|---|---|
| `DPD (account) Incoming parcels` | Number of active incoming parcels |
| `DPD (account) Parcel <barcode>` | Canonical status of a single incoming shipment |
| `DPD (account) Next delivery` | Earliest expected delivery datetime |
| `DPD (account) En route to ParcelShop` | Active incoming parcels still in transit to a DPD ParcelShop |
| `DPD (account) Awaiting pickup` | Parcels that have arrived at a ParcelShop and are ready to collect |
| `DPD (account) Delivered parcels` | Recently delivered incoming parcels (configurable window) |
| `DPD (account) Outgoing parcels` | Number of active outgoing parcels |
| `DPD (account) Outgoing delivered parcels` | Recently delivered outgoing parcels (same configurable window) |

Every parcel exposed on a sensor attribute uses a carrier-agnostic shape:

| Key | Type | Meaning |
|---|---|---|
| `carrier` | string | `"DPD"` |
| `barcode` | string | Parcel tracking number |
| `sender` | string \| null | Sender name (e.g. webshop) |
| `receiver` | string \| null | Recipient name. May briefly be `null` the first time a new barcode appears. |
| `status` | `ParcelStatus` | Canonical status — see the [status reference](#parcel-status-reference) |
| `raw_status` | string \| null | Original DPD status description |
| `delivered` | bool | Whether the parcel has been delivered |
| `delivered_at` | ISO 8601 \| null | Delivery moment, if known |
| `planned_from` | ISO 8601 \| null | Expected delivery window start (the precise hour on delivery day, otherwise midnight on the planned date) |
| `planned_to` | ISO 8601 \| null | Expected delivery window end |
| `pickup` | bool | Destined for a pickup point rather than a home address |
| `pickup_point` | string \| null | ParcelShop name when `pickup` is true (always `null` for now — DPD does not expose the field) |
| `url` | string \| null | Deep link to the parcel's tracking page |
| `weight` | float \| null | Parcel weight in kilograms |
| `dimensions` | dict \| null | Parcel dimensions in centimeters: `{length, width, height, text}` where `text` is a pre-formatted `"L x W x H cm"` string |
| `history` | list \| null | Ordered status timeline (oldest → newest), each entry `{timestamp, status, raw_status}`, capped to the most recent 20. `null` unless the **Parcel history** option is enabled — see [Options](#options). |
| `raw` | dict | The original DPD API payload |

## Parcel status reference

`status` on every parcel is one of the canonical `ParcelStatus` values
below. Use these in your automations rather than DPD's raw description
strings — the raw value stays available on `raw_status` for power
users.

| `status` | Meaning | DPD raw description that maps here |
|---|---|---|
| `registered` | DPD knows about the label but the parcel is not yet in transit | `ORDER_CREATED` |
| `in_transit` | Picked up; somewhere in DPD's network | `PARCEL_HANDED`, `IN_TRANSIT`, `AT_DELIVERY_CENTER`, `UNSUCCESSFUL_DELIVERY_ATTEMPTED` |
| `out_for_delivery` | On the delivery vehicle today | `PARCEL_OUT_FOR_DELIVERY` |
| `at_pickup_point` | Arrived at the ParcelShop, ready to collect | `AVAILABLE_FOR_COLLECTION` |
| `delivered` | Handed over (mailbox, recipient, neighbour, picked up) | `DELIVERED` |
| `returning` | Failed delivery, on the way back to the sender | `RETURN_TO_SENDER` |
| `problem` | Carrier reports an exception, intervention, or other issue | (not yet observed) |
| `unknown` | Raw description we have not mapped yet | anything else — logged once at warning level with a ready-to-paste issue link so it can be added to the map |

## Events

The coordinator fires events on the HA event bus when something
interesting happens to a parcel, so automations can react without
polling per-parcel sensors.

| Event | When | Payload |
|---|---|---|
| `dpd_parcel_registered` | A new barcode appears in the active list | The full parcel dict (see the table above) |
| `dpd_parcel_status_changed` | A known barcode's canonical `status` value changes, except the final hop to delivered | Same payload plus `old_status` and `new_status` |
| `dpd_parcel_delivered` | An incoming parcel is delivered | The full parcel dict |
| `dpd_parcel_delivery_time_changed` | A known barcode's expected delivery time changes to a new value | Same payload plus `old_planned_from`, `new_planned_from`, `old_planned_to`, `new_planned_to` |
| `dpd_outgoing_parcel_status_changed` | A known **outgoing** parcel (something you sent) changes status, except the final hop to delivered | Same payload plus `old_status` and `new_status` |
| `dpd_outgoing_parcel_delivered` | An outgoing parcel reaches the recipient | The full parcel dict |

Every payload also carries a `device_id` identifying the DPD account the
parcel belongs to, so automations can tell two accounts apart.

Events do not fire for parcels that were already in your account when HA first started.

If you build automations in the UI, these same events are also available
as no-code **device triggers** (**Settings → Automations → Create → Add
trigger → Device**), scoped to the selected account's device. The raw
events above are there for templates and YAML automations.

See [`examples/automations/`](examples/automations/) for ready-to-paste
event-driven automations, or the
[parcel aggregator](https://github.com/ha-parcel-integrations/ha-parcel-aggregator)
for a carrier-agnostic re-emit layer that fires
`parcel_aggregator_parcel_*` events covering every installed carrier
in one go.

## Examples

Ready-to-paste automations and dashboard cards live in [`examples/`](examples/).

### Community Lovelace cards

Third-party cards that work with this integration's sensors:

- [jonisnet/hki-parcels-card](https://github.com/jonisnet/hki-parcels-card)
- [klaptafel/ha-package-tracker-card](https://github.com/klaptafel/ha-package-tracker-card)

## Debugging

To capture the raw DPD API response (useful when reporting a bug or helping map the shipment object structure), enable debug logging for the integration:

1. Add this to your `configuration.yaml`:
   ```yaml
   logger:
     default: warning
     logs:
       custom_components.dpd: debug
   ```
2. Restart Home Assistant.
3. Wait for the next poll cycle (or reload the integration from **Settings → Devices & Services → DPD → ⋮ → Reload**).
4. Open **Settings → System → Logs**, filter for `dpd`, and copy the `DPD raw parcels payload: ...` line into your bug report or message to the maintainer.

The raw payload is only logged when there is at least one incoming or outgoing shipment.

## Troubleshooting

| Symptom | Likely cause |
|---------|--------------|
| `invalid_auth` error during setup | Wrong email or password |
| `cannot_connect` error during setup | DPD API is unreachable; check your network |
| Re-authentication prompt appears | DPD session expired and could not be refreshed silently; log in again |
| Sensors not updating | Check **Settings → System → Logs** for `dpd` entries |

## Related integrations

This integration is part of [**ha-parcel-integrations**](https://github.com/ha-parcel-integrations) — a family of
parcel-carrier integrations that all publish the same canonical parcel format,
statuses and events.

- [**Parcel Aggregator**](https://github.com/ha-parcel-integrations/ha-parcel-aggregator) rolls every installed carrier
  up into one set of sensors.
- Browse [the organisation](https://github.com/ha-parcel-integrations) for the current list of supported carriers.

## Disclaimer

This is an independent, community-built project with no affiliation, endorsement, or connection to DPD or any of its subsidiaries. The DPD API used here is undocumented (reverse-engineered from the mobile app) and may change without notice. The maintainers have not asked DPD for permission to use this API; installing this integration may breach DPD's Terms of Service. You take any risk that follows — account suspension, service disruption, etc. No warranty (see [LICENSE](LICENSE)).

## Contributing

Pull requests and issues are welcome. Please open an issue before submitting a large change.

## License

MIT
