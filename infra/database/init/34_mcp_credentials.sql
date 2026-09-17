-- A credential a program holds, to reach Vigil's MCP surface.
--
-- Not a column on `users`. One person mints more than one -- the platform, a
-- script, a laptop -- and rotating means the new one works before the old one
-- is revoked, so two exist at once. Revoking a leaked one must also not touch
-- the account it belongs to: `users.password_hash` is how a person signs in,
-- and a machine credential does not belong beside it.
--
-- It is tied to a user and carries no permissions of its own. What the holder
-- may do is what that user may do, through the role the user already has, so
-- there is one permission model rather than two. The cost is that a credential
-- is as powerful as its user: a credential for the platform belongs to a user
-- made for the platform, not to an administrator who also happens to be a
-- person.

CREATE TABLE IF NOT EXISTS mcp_credentials (
    credential_id VARCHAR(50) PRIMARY KEY,

    -- Deleting the person deletes what was minted for them. A credential
    -- outliving its user would be a principal nothing can describe.
    user_id VARCHAR(50) NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,

    -- The token is shown once, at minting, and never stored. This is a SHA-256
    -- of it: the token is 256 bits from a CSPRNG, so there is nothing to guess
    -- and no work factor to impose. bcrypt is for a secret a person chose and
    -- would add its deliberate cost to every call a program makes.
    token_hash CHAR(64) NOT NULL UNIQUE,

    -- What the operator called it, so a list of credentials is readable and a
    -- revocation can be aimed at the right one.
    label VARCHAR(200) NOT NULL,

    created_at TIMESTAMP NOT NULL DEFAULT NOW(),
    -- Recorded on use so an unused credential can be found and removed. Not a
    -- precise access log: the Ledger records what was done.
    last_used_at TIMESTAMP,
    -- NULL means it does not expire. Stated rather than assumed.
    expires_at TIMESTAMP,
    -- Revocation is recorded, not deleted: a credential that was used and then
    -- withdrawn is a fact about the past.
    revoked_at TIMESTAMP
);

-- The lookup every authenticated call makes.
CREATE INDEX IF NOT EXISTS idx_mcp_credentials_token_hash
    ON mcp_credentials (token_hash);

-- Listing and revoking what belongs to one person.
CREATE INDEX IF NOT EXISTS idx_mcp_credentials_user
    ON mcp_credentials (user_id);
