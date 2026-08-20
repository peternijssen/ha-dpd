"""Constants for the DPD integration."""
from enum import StrEnum

from homeassistant.const import Platform

DOMAIN = "dpd"


class DpdAuthError(Exception):
    """Raised when DPD authentication fails.

    Defined here (not in ``api.py``) so a country module can raise it
    without an import cycle back through ``api.py``.
    """


class DpdApiError(Exception):
    """Raised when a DPD API call returns a non-success status."""

    def __init__(self, status_code: int) -> None:
        """Store the status code that triggered the error."""
        super().__init__(f"DPD API request failed with status {status_code}")
        self.status_code = status_code


class ParcelStatus(StrEnum):
    """Carrier-agnostic parcel status.

    Maps the carrier-specific raw status strings into a small set of
    canonical values shared across DHL, DPD, PostNL and the parcel
    aggregator. Listed in roughly the order a parcel moves through.
    """

    REGISTERED = "registered"               # Sender announced the parcel; carrier has not handed-over yet
    IN_TRANSIT = "in_transit"               # In the carrier's network, somewhere between sender and delivery point
    OUT_FOR_DELIVERY = "out_for_delivery"   # On a delivery vehicle today
    AT_PICKUP_POINT = "at_pickup_point"     # Arrived at the chosen ServicePoint / PostNL Point / ParcelShop
    DELIVERED = "delivered"                 # Handed over (mailbox, recipient, neighbour, picked up)
    RETURNING = "returning"                 # Failed delivery, going back to sender
    PROBLEM = "problem"                     # Carrier reports an exception, intervention, or other issue
    UNKNOWN = "unknown"                     # Raw status we have not mapped yet — logged at info level

PLATFORMS: list[Platform] = [Platform.BUTTON, Platform.CALENDAR, Platform.SENSOR]

# Every optional key the parcel contract defines. CAPABILITIES below must be a
# subset of this — it exists so a typo in CAPABILITIES fails a test instead of
# silently dropping this carrier off a table on the docs site.
KNOWN_CAPABILITIES = frozenset(
    {"weight", "dimensions", "delivery_window", "pickup_point", "url", "history"}
)

# Which optional contract fields this carrier's API actually populates — feeds
# the comparison table on the docs site. Keep in lockstep with
# normalize_parcel() in parcels.py: everything not listed here comes back as a
# literal None there. DPD's per-parcel detail call fills weight, dimensions,
# the FMP delivery window, opt-in history and a ParcelShop pickup point name.
CAPABILITIES = frozenset(
    {"weight", "dimensions", "delivery_window", "pickup_point", "url", "history"}
)

POLL_INTERVAL = 900  # seconds (15 minutes) — legacy hard-coded fallback

KEYCLOAK_TOKEN_URL = (
    "https://login.dpdgroup.com/auth/realms/login/protocol/openid-connect/token"
)
KEYCLOAK_CLIENT_ID = "MOBILE-APP-PROD"

DPD_BASE_URL = "https://www.dpdgroup.com/concept/webservice"
DPD_GUEST_TOKEN_URL = f"{DPD_BASE_URL}/oauth/token"
DPD_CONSIGNEE_SSO_URL = f"{DPD_BASE_URL}/users/login/consignee-sso"
DPD_PARCELS_URL = f"{DPD_BASE_URL}/v7/parcels"
# Per-parcel detail endpoint. Carries the recipient block (`receiver.name`)
# plus weight/dimensions/currentPosition/parcelHistory, none of which appear
# in the list endpoint. Used to populate the canonical `receiver` field on
# the parcel shape — cached per barcode so we only call it once per parcel.
DPD_PARCEL_DETAIL_URL = f"{DPD_BASE_URL}/v10/parcels/details"

# Follow My Parcel — DPD's same-day delivery-window sub-API. Per-parcel
# auth flow: take the hashcode from
# ``availableActions.FOLLOW_MY_PARCEL[0].hashcode`` on the shipment,
# exchange it for an FMP access token at the authenticate endpoint, then
# fetch the shipment detail to read ``deliveryDateAndTime.timeRange``
# (``from`` / ``to``).
DPD_FMP_AUTHENTICATE_URL = f"{DPD_BASE_URL}/fmp/authenticate"
DPD_FMP_SHIPMENT_URL = f"{DPD_BASE_URL}/v3/fmp/shipment"

# DPD UK's own public web tracker (track.dpd.co.uk) — an entirely separate,
# keyless host from the myDPD backend above. Confirmed live (2026-08-17,
# static read of the tracker's own React bundle + a live capture): GET
# /v1/reference with a bare parcelNumber (no auth, and the postcode param is
# not actually checked at this step) returns a ``data[0].parcelCode`` in the
# shape ``<14-digit-number>*<sequence>`` — the sequence is DPD-assigned, not
# a postcode transform. That code is what ``track.dpd.co.uk/parcels/<code>``
# expects. See DpdApiClient.async_get_uk_tracking_code.
DPD_UK_REFERENCE_URL = "https://apis.track.dpd.co.uk/v1/reference"
DPD_UK_TRACKING_URL = "https://track.dpd.co.uk/parcels"

# myDPD Mobile App client credentials (base64 of "<client_id>:<client_secret>"),
# fetched from DPD's Firebase Remote Config and hardcoded in the mobile app.
DPD_BASIC_TOKEN = (
    "bXlEUEQgTW9iaWxlIEFwcDpaMVdzeTQ4RGpseWcweDdVWjhvWTlYdmZIT2xIbW4yTmpJdnYycmpVVjY3N1hDOGhiTGlkNHY2OWpCQzlvZnpU"
)

USER_AGENT = "okhttp/4.12.0"

CONF_BU = "bu"

BUSINESS_UNITS = [
    {"value": "DPD-NL", "label": "Netherlands"},
    {"value": "DPD-AR", "label": "Argentina"},
    {"value": "DPD-BE", "label": "Belgium"},
    {"value": "DPD-HR", "label": "Croatia"},
    {"value": "DPD-CZ", "label": "Czech Republic"},
    {"value": "DPD-EE", "label": "Estonia"},
    {"value": "DPD-FR", "label": "France"},
    {"value": "DPD-HU", "label": "Hungary"},
    {"value": "BRT", "label": "Italy"},
    {"value": "DPD-LV", "label": "Latvia"},
    {"value": "DPD-LT", "label": "Lithuania"},
    {"value": "DPD-LU", "label": "Luxembourg"},
    {"value": "CHR-PT", "label": "Portugal"},
    {"value": "DPD-SK", "label": "Slovakia"},
    {"value": "DPD-SI", "label": "Slovenia"},
    {"value": "DPD-CH", "label": "Switzerland"},
    {"value": "DPD-UK", "label": "United Kingdom"},
]

DEFAULT_BU = "DPD-NL"

# The config-flow's single country/BU dropdown — BUSINESS_UNITS plus DPD
# Germany's separate SOAP backend, so the user picks a destination country
# once and never has to answer "which backend" as its own question. Kept
# apart from BUSINESS_UNITS itself: that constant is also used to build
# general-backend requests (bu query values sent to myDPD/Keycloak), and
# "DPD-DE" must never flow into that path.
COUNTRY_OPTIONS = BUSINESS_UNITS + [{"value": "DPD-DE", "label": "Germany"}]

# ``DPD-UK`` has no real business-unit code of its own on the shared myDPD
# backend (confirmed absent from the myDPD app's own BU list, see
# carrier-research/dpd/dpd-log.md 2026-08-11) — but a UK account was
# confirmed (2026-08-17, github.com/ha-parcel-integrations/.github/
# discussions/14) to log in and list its own parcels through the plain
# DPD-NL flow. `bu` sent to Keycloak/consignee-sso/parcels/detail is
# remapped through this table before use; `DpdApiClient.bu` (config,
# unique_id, tracking-url derivation) keeps the configured value.
BU_API_OVERRIDES = {
    "DPD-UK": "DPD-NL",
}

# Country segment of the tracking URL is normally derived from the BU
# (``DPD-DE`` -> ``de``, see `_tracking_url`), but a few acquired brands
# on the shared myDPD backend don't follow the `DPD-<CC>` shape.
# ``DPD-UK`` -> ``nl`` is the deliberate fallback used only when the live
# UK tracking-code lookup (DPD_UK_REFERENCE_URL, see parcels.py/coordinator.py)
# fails or hasn't resolved yet for a barcode — not a placeholder. Confirmed
# (2026-08-17) reachable and shows the account's own parcels even though it's
# the NL-branded page.
BU_COUNTRY_OVERRIDES = {
    "CHR-PT": "pt",
    "DPD-UK": "nl",
}

# BUs whose tracking page isn't dpdgroup.com/<country>/mydpd/my-parcels/search
# at all — confirmed for Italy: BRT keeps its own domain and path entirely
# (mybrt.it/it/mybrt/my-parcels/incoming), not just a different brand segment
# under dpdgroup.com (that guess 404s).
BU_TRACKING_URL_OVERRIDES = {
    "BRT": "https://www.mybrt.it/it/mybrt/my-parcels/incoming?parcelNumber={parcel_number}",
}

# Pre-filled "add my country" GitHub issue, linked from the setup form so
# users can request another DPD business unit. Passed as a description
# placeholder (translation strings may not contain raw URLs).
NEW_COUNTRY_ISSUE_URL = (
    "https://github.com/ha-parcel-integrations/ha-dpd/issues/new"
    "?title=Add%20country%3A%20%3Cyour%20country%3E&labels=enhancement"
)

# Known DPD `status.description` strings, in roughly the order a parcel
# moves through. The numeric `status.status` follows the same 0 → 5
# progression in samples we have seen.
#
# These constants are descriptive — the only filter we apply today is
# "delivered vs everything-else" (using `DELIVERED_DESCRIPTION`). The
# 2.0.0 normalization layer will map each value onto the canonical
# `ParcelStatus` enum used by the other carriers.
STATUS_ORDER_CREATED = "ORDER_CREATED"                  # 0 — label printed; not yet collected
STATUS_PARCEL_HANDED = "PARCEL_HANDED"                  # 1 — handed to DPD by the sender
STATUS_IN_TRANSIT = "IN_TRANSIT"                        # 2 — in DPD's network
STATUS_AT_DELIVERY_CENTER = "AT_DELIVERY_CENTER"        # 3 — at the regional sorting hub the morning of delivery
STATUS_PARCEL_OUT_FOR_DELIVERY = "PARCEL_OUT_FOR_DELIVERY"  # 4 — on the delivery vehicle today
STATUS_DELIVERED = "DELIVERED"                          # 5 — terminal

# Additional status.description values used by the myDPD consumer app's own
# `parcel_status` taxonomy (confirmed from the myDPD Android app, 3.78.26).
# We never saw these in sample data (no parcelshop / return / failed-attempt
# parcel was on the account), but they are first-class consumer statuses.
STATUS_AVAILABLE_FOR_COLLECTION = "AVAILABLE_FOR_COLLECTION"  # ready to collect at a ParcelShop
STATUS_RETURN_TO_SENDER = "RETURN_TO_SENDER"                  # going back to the sender
STATUS_UNSUCCESSFUL_DELIVERY = "UNSUCCESSFUL_DELIVERY_ATTEMPTED"  # missed attempt; will be retried

# Terminal status — every other status.description is treated as "active".
DELIVERED_DESCRIPTION = STATUS_DELIVERED

# All description values the integration recognises. Anything outside this
# set is still treated as "active" (so we never accidentally swallow a
# parcel) and surfaced via a one-shot warning so we can grow the list.
KNOWN_DESCRIPTIONS: frozenset[str] = frozenset({
    STATUS_ORDER_CREATED,
    STATUS_PARCEL_HANDED,
    STATUS_IN_TRANSIT,
    STATUS_AT_DELIVERY_CENTER,
    STATUS_PARCEL_OUT_FOR_DELIVERY,
    STATUS_DELIVERED,
    STATUS_AVAILABLE_FOR_COLLECTION,
    STATUS_RETURN_TO_SENDER,
    STATUS_UNSUCCESSFUL_DELIVERY,
})

CONF_DELIVERED_FILTER_TYPE = "delivered_filter_type"
CONF_DELIVERED_FILTER_AMOUNT = "delivered_filter_amount"
DEFAULT_DELIVERED_FILTER_TYPE = "days"
DEFAULT_DELIVERED_FILTER_AMOUNT = 7

# Refresh interval (minutes) controls how often the coordinator polls DPD.
# Default 30 min — gentle on the consumer API which has shown to be flaky
# during peak hours. Minimum 15 min for the same reason, maximum 240 min
# (4h) for users who just want one or two checks a day.
CONF_REFRESH_INTERVAL = "refresh_interval"
REFRESH_INTERVAL_OPTIONS = (15, 30, 60, 120, 240)
DEFAULT_REFRESH_INTERVAL = 30

CONF_INCLUDE_HISTORY = "include_history"
DEFAULT_INCLUDE_HISTORY = False
# Cap each parcel's history to the most recent N events so the attribute
# stays well under HA's ~16 KB state-attribute limit.
HISTORY_MAX_EVENTS = 20

# Country selection — DPD Germany is a separate backend from the
# myDPD/dpdgroup.com one BUSINESS_UNITS configures. "general" is the shared
# backend serving NL plus the 14 other business units; DE has none.
CONF_COUNTRY = "country"
COUNTRY_GENERAL = "general"
COUNTRY_DE = "de"
DEFAULT_COUNTRY = COUNTRY_GENERAL

# Persisted in entry.data (not entry.options) so a DE hub keeps the same
# device identity across HA restarts.
CONF_DE_HARDWARE_ID = "de_hardware_id"

# DPD Germany — Paketnavigator app SOAP backend. countries/de/session.py is
# the only module that sends these on the wire.
DPD_DE_SOAP_URL = "https://api.paketnavigator.de/services/v1/Navigator3Service.asmx"
DPD_DE_SOAP_NAMESPACE = "https://cloud.dpd.com/"
DPD_DE_PARTNER_NAME = "Android Paketnavigator3"
DPD_DE_PARTNER_TOKEN = "A33363237662F5945576"
DPD_DE_PARTNER_SECRET = "272 WetFd2mpXrgD"
DPD_DE_USER_AGENT = "ksoap2-android/2.6.0+"
DPD_DE_LANGUAGE = "de_DE"
# SoapApiEndpoint.isTimeCorrupted() rejects a response TimeStamp more than
# this far from local time — the practical clock-skew tolerance for the
# minute-derived KeyPhase.
DPD_DE_CLOCK_SKEW_TOLERANCE_MINUTES = 8
