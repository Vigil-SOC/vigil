"""Storage domain package (Reorg R5 / #486).

Shared persistence/data-access tier — the metadata DB data-access
layer, the S3 object-store client, and DB/connection proxies.

Callers import directly from ``core.storage.<module>``; the legacy
``services.*`` paths were removed with this move.
"""
