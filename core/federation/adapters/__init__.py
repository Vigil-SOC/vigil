"""Shared base classes for the builtin federation source adapters.

The vendor adapter modules now live in their integration slices under
``core/integrations/<vendor>/adapter.py`` — each registers itself against
:func:`core.federation.registry.register_adapter` at import time. Only the
shared bases (:mod:`._base`, :mod:`._siem_base`) remain here.
"""
