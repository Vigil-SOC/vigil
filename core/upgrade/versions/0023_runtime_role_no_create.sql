-- python -m core.upgrade is the one schema writer and logs in as the owner, so
-- the role the app logs in as has nothing to create. 0001_baseline grants this,
-- and the role file adoption applies must match it, so it is taken away here.

REVOKE CREATE ON SCHEMA public FROM vigil_app;
