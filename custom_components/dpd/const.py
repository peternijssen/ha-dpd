"""Constants for the DPD integration."""
from enum import StrEnum

from homeassistant.const import Platform

DOMAIN = "dpd"


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

# myDPD Mobile App client credentials (base64 of "<client_id>:<client_secret>"),
# fetched from DPD's Firebase Remote Config and hardcoded in the mobile app.
DPD_BASIC_TOKEN = (
    "bXlEUEQgTW9iaWxlIEFwcDpaMVdzeTQ4RGpseWcweDdVWjhvWTlYdmZIT2xIbW4yTmpJdnYycmpVVjY3N1hDOGhiTGlkNHY2OWpCQzlvZnpU"
)

USER_AGENT = "okhttp/4.12.0"

CONF_BU = "bu"

BUSINESS_UNITS = [
    {"value": "DPD-NL", "label": "Netherlands"},
    {"value": "DPD-DE", "label": "Germany"},
    {"value": "DPD-CH", "label": "Switzerland"},
]

DEFAULT_BU = "DPD-NL"

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
