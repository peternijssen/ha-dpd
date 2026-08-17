"""Thin dispatcher onto the country-specific DPD transport clients.

``DpdApiClient`` (general/NL+14-BU) moved to ``countries/general/`` and is
re-exported here unchanged, so existing imports and patch targets keep
working. ``DpdApiError``/``DpdAuthError`` live in ``const.py`` to avoid an
import cycle, re-exported here too.

DPD Germany doesn't use this client — its SOAP transport is
``countries/de/session.py``, constructed directly once ``CONF_COUNTRY`` is
known.
"""
from __future__ import annotations

from .const import DpdApiError, DpdAuthError
from .countries.general import GeneralApiClient as DpdApiClient

__all__ = ["DpdApiClient", "DpdApiError", "DpdAuthError"]
