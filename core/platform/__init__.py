"""Platform domain package (Reorg R5 / #486).

Shared runtime plumbing — local service orchestration, autostart
config, memory-palace paths, demo-data seeding, URL/SSRF safety.

Callers import directly from ``core.platform.<module>``; the legacy
``services.*`` paths were removed with this move.
"""
