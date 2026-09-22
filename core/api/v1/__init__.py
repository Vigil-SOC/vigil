"""The versioned ``/api/v1`` contract surface.

Every module here is a **frozen** external API: its route paths and response
shapes are a commitment held stable across all 1.x releases. That is the whole
reason the contract lives in its own package rather than being tagged inside
the mixed console routers — a path can be guarded (CODEOWNERS), a decorator
cannot, and a reviewer can see the promise by looking at the tree.

Rules for anything added here:

* A route belongs here only if an external consumer or the platform relies on
  it. Console-only interaction (comments, watchers, UI toggles) stays in the
  unversioned routers under ``services/api/routers/`` and ``core/<domain>/``.
* Each module mounts under ``/api/v1`` (usually ``/api/v1/<resource>``; a
  router whose routes already carry the resource in their path, like approvals,
  uses ``prefix=/api/v1``) and lists its pre-version path
  in ``legacy_prefixes`` so existing callers keep working during the port.
* Request/response shapes here are what the committed OpenAPI snapshot and the
  contract test guard. Changing one is a breaking change, not a refactor.
"""
