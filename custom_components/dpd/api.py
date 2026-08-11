"""DPD API client."""
from __future__ import annotations

import asyncio
import logging
from typing import Any

import aiohttp

from .const import (
    DEFAULT_BU,
    DPD_BASIC_TOKEN,
    DPD_CONSIGNEE_SSO_URL,
    DPD_FMP_AUTHENTICATE_URL,
    DPD_FMP_SHIPMENT_URL,
    DPD_GUEST_TOKEN_URL,
    DPD_PARCEL_DETAIL_URL,
    DPD_PARCELS_URL,
    KEYCLOAK_CLIENT_ID,
    KEYCLOAK_TOKEN_URL,
    USER_AGENT,
)

_LOGGER = logging.getLogger(__name__)


class DpdAuthError(Exception):
    """Raised when DPD authentication fails."""


class DpdApiError(Exception):
    """Raised when a DPD API call returns a non-success status."""

    def __init__(self, status_code: int) -> None:
        """Store the status code that triggered the error."""
        super().__init__(f"DPD API request failed with status {status_code}")
        self.status_code = status_code


class DpdApiClient:
    """Client for the DPD parcel tracking API."""

    def __init__(
        self,
        email: str,
        password: str,
        session: aiohttp.ClientSession,
        bu: str = DEFAULT_BU,
    ) -> None:
        """Initialize the API client."""
        self._email = email
        self._password = password
        self._session = session
        self._bu = bu
        self._token: str | None = None
        self._reauth_lock = asyncio.Lock()

    @property
    def access_token(self) -> str | None:
        """Return the current DPD access token, or ``None`` if not yet logged in."""
        return self._token

    @property
    def bu(self) -> str:
        """Return the configured DPD business unit (e.g. ``DPD-NL``)."""
        return self._bu

    async def async_login(self) -> str:
        """Run the three-step auth flow and return the DPD access token.

        1. Keycloak login with username/password
        2. Fetch a guest token using the hardcoded mobile app client credentials
        3. Exchange the Keycloak token for a DPD user token via consignee-sso
        """
        kc_token = await self._async_keycloak_login()
        guest_token = await self._async_guest_token()
        dpd_token = await self._async_consignee_sso(kc_token, guest_token)
        self._token = dpd_token
        return dpd_token

    async def _async_keycloak_login(self) -> str:
        data = {
            "client_id": KEYCLOAK_CLIENT_ID,
            "grant_type": "password",
            "scope": "openid",
            "username": self._email,
            "password": self._password,
        }
        async with self._session.post(
            KEYCLOAK_TOKEN_URL,
            data=data,
            headers={"User-Agent": USER_AGENT},
        ) as response:
            if response.status >= 500:
                raise DpdApiError(response.status)
            body: dict[str, Any] = await response.json(content_type=None)

        token = body.get("access_token")
        if not token:
            error = body.get("error_description") or body.get("error", "unknown")
            raise DpdAuthError(f"Keycloak login failed: {error}")
        return token

    async def _async_guest_token(self) -> str:
        async with self._session.post(
            DPD_GUEST_TOKEN_URL,
            params={"grant_type": "client_credentials"},
            headers={
                "Authorization": f"Basic {DPD_BASIC_TOKEN}",
                "Content-Type": "application/json",
                "User-Agent": USER_AGENT,
            },
        ) as response:
            if response.status >= 500:
                raise DpdApiError(response.status)
            body: dict[str, Any] = await response.json(content_type=None)

        token = body.get("access_token")
        if not token:
            raise DpdAuthError("DPD guest token request did not return a token")
        return token

    async def async_get_parcels(self) -> dict[str, Any]:
        """Retrieve the parcels payload, re-authenticating once on session expiry."""
        async def _fetch() -> dict[str, Any]:
            async with self._session.post(
                DPD_PARCELS_URL,
                params={"bu": self._bu, "lang": "en"},
                json={
                    "incomingParcels": [],
                    "sendingParcels": [],
                    "confirmedParcels": None,
                    "shipmentCollections": [],
                    "confirmedShipmentCollections": None,
                },
                headers={
                    "Authorization": f"Bearer {self._token}",
                    "Content-Type": "application/json",
                    "User-Agent": USER_AGENT,
                },
            ) as response:
                if response.status != 200:
                    raise DpdApiError(response.status)
                data: dict[str, Any] = await response.json(content_type=None)
            return data

        return await self._async_call_with_reauth(_fetch)

    async def async_get_parcel_detail(
        self,
        parcel_number: str,
        *,
        shipment_bu_code: str | None = None,
        parcel_type: str = "INCOMING",
    ) -> dict[str, Any] | None:
        """Fetch /v10/parcels/details/{n}. Best-effort: returns None on any failure.

        The list endpoint is a thin summary; this detail endpoint carries the
        recipient block, weight, dimensions and current truck position. We use
        it only to populate the canonical ``receiver`` field; failures are
        swallowed so a broken detail call never breaks the main parcels poll.
        """
        params: dict[str, str] = {
            "parcelType": parcel_type,
            "businessUnit": self._bu,
            "lang": "en",
            "continueWithoutVerification": "false",
        }
        if shipment_bu_code:
            params["shipmentBUCode"] = shipment_bu_code
        try:
            async with self._session.post(
                f"{DPD_PARCEL_DETAIL_URL}/{parcel_number}",
                params=params,
                headers={
                    "Authorization": f"Bearer {self._token}",
                    "Content-Type": "application/json",
                    "User-Agent": USER_AGENT,
                },
            ) as response:
                if response.status != 200:
                    return None
                return await response.json(content_type=None)
        except aiohttp.ClientError:
            return None

    async def async_fmp_delivery_window(self, hashcode: str) -> dict[str, Any] | None:
        """Fetch the Follow My Parcel delivery window for a parcel hashcode.

        Returns the raw ``deliveryDateAndTime`` dict
        (``{"deliveryDate": ..., "timeRange": {"from": ..., "to": ...}}``)
        when DPD has scheduled a precise window — typically on the day a
        parcel is out for delivery. Returns ``None`` when the window is
        not (yet) available or the call fails; FMP is best-effort and
        should never break the main parcels poll.
        """
        try:
            fmp_token = await self._async_fmp_authenticate(hashcode)
        except DpdApiError as err:
            _LOGGER.debug(
                "FMP authenticate failed for hashcode %s: HTTP %d",
                hashcode[:8], err.status_code,
            )
            return None
        except DpdAuthError as err:
            _LOGGER.debug("FMP authenticate returned no token: %s", err)
            return None

        try:
            shipment = await self._async_fmp_shipment(fmp_token)
        except DpdApiError as err:
            _LOGGER.debug(
                "FMP shipment fetch failed for hashcode %s: HTTP %d",
                hashcode[:8], err.status_code,
            )
            return None

        delivery = shipment.get("deliveryDateAndTime")
        return delivery if isinstance(delivery, dict) else None

    async def _async_fmp_authenticate(self, hashcode: str) -> str:
        """Exchange a parcel hashcode for an FMP access token."""
        async with self._session.post(
            DPD_FMP_AUTHENTICATE_URL,
            json={"authMethod": "HASHCODE", "credentials": hashcode},
            headers={
                "Authorization": f"Bearer {self._token}",
                "Content-Type": "application/json",
                "User-Agent": USER_AGENT,
            },
        ) as response:
            if response.status != 200:
                raise DpdApiError(response.status)
            body: dict[str, Any] = await response.json(content_type=None)

        token = body.get("access_token")
        if not token:
            raise DpdAuthError("DPD FMP authenticate did not return a token")
        return token

    async def _async_fmp_shipment(self, fmp_token: str) -> dict[str, Any]:
        """Fetch the FMP shipment detail for the current FMP token."""
        async with self._session.get(
            DPD_FMP_SHIPMENT_URL,
            params={"lang": "en"},
            headers={
                "Authorization": f"Bearer {fmp_token}",
                "Content-Type": "application/json",
                "User-Agent": USER_AGENT,
            },
        ) as response:
            if response.status != 200:
                raise DpdApiError(response.status)
            data: dict[str, Any] = await response.json(content_type=None)
        return data

    async def _async_call_with_reauth(self, coro_fn: Any) -> Any:
        """Call coro_fn(), re-authenticating once if the session has expired."""
        try:
            return await coro_fn()
        except DpdApiError as err:
            if err.status_code not in (401, 403):
                raise
        async with self._reauth_lock:
            await self.async_login()
        return await coro_fn()

    async def _async_consignee_sso(self, kc_token: str, guest_token: str) -> str:
        async with self._session.post(
            DPD_CONSIGNEE_SSO_URL,
            params={"bu": self._bu},
            data=kc_token,
            headers={
                "Authorization": f"Bearer {guest_token}",
                "Content-Type": "text/plain",
                "User-Agent": USER_AGENT,
            },
        ) as response:
            if response.status >= 500:
                raise DpdApiError(response.status)
            body: dict[str, Any] = await response.json(content_type=None)

        token = body.get("access_token")
        if not token:
            raise DpdAuthError("DPD consignee-sso did not return a token")
        return token
