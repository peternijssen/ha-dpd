"""Per-country DPD logic: transport, ``normalize_parcel_<code>``, status maps.

Concern-level modules (``api.py``, ``parcels.py``, ``coordinator.py``,
``config_flow.py``, ``diagnostics.py``) stay top-level and dispatch into a
country module by ``CONF_COUNTRY``; they carry no per-country branching of
their own beyond that single dispatch point.

``general`` is not one country — it's the shared myDPD/dpdgroup.com backend
serving NL plus the 14 other business units in ``const.py``'s
``BUSINESS_UNITS``. ``de`` is DPD Germany's separate Paketnavigator SOAP
stack, with its own nested ``session.py`` for the auth/session lifecycle
``general`` doesn't need.

A country gets a nested package only once it needs lifecycle handling with
no ``general`` equivalent — not merely because a second country exists.
"""
