"""API package.

Routers are discovered and mounted by :mod:`api._discovery`, which scans this
package for modules exporting a ``router`` and a ``ROUTER_META``. There is
deliberately no re-export list here: the previous one covered only 19 of the
42 router modules (``main.py`` imported the other 23 directly), so it was a
second, inconsistent convention that had to be kept in sync by hand — and its
eager imports meant one bad module broke the whole package (issue #478).

Adding a router: create the module with a ``router`` and a ``ROUTER_META``.
Nothing here or in ``backend/main.py`` needs to change. A module in this
package that is *not* a router (a shared helper) must be listed in
``api._discovery._SKIP``, or discovery treats it as a router and fails at
startup.
"""
