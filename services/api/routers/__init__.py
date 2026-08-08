"""Parked API routers.

Routers whose domain is not yet in ``core/`` (blocked on the auth-service
migration, a loose ``services/`` dependency, or a domain not yet extracted)
live here until their slice lands, then move to ``core/<domain>/<name>_router.py``.
Discovery (``services/api/discovery.py``) scans this directory and
``core/**/*_router.py`` alike.
"""
