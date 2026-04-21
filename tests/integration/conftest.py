"""
Integration test fixtures.

NOTE on scope: DB-backed fixtures (sample_user, sample_role, authenticated
client with a real session) are intentionally NOT built here yet. Proper
integration tests need:
- An isolated test Postgres instance (CI has one; local runs don't)
- Session rollback between tests (`transactional_case` pattern)
- Factory fixtures for User/Role with deterministic IDs

That scaffolding is its own piece of work — tracked as a follow-up to the
auth-hardening umbrella (#76). For now the integration tests in this
directory exercise the API contract (endpoint wiring, error shapes,
middleware side effects) which runs without a populated DB.
"""
