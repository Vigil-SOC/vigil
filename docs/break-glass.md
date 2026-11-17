# Break-glass accounts

A break-glass account is a local account that still signs in while Require single sign-on is on and the identity provider is down. It signs in on the login page's "Sign in with a password (break-glass accounts)" route with its password, one recovery code from the safe, and a reason. Every such sign-in is on the audit trail with its reason, and each account manager is told who, why and from where.

Only the host marks or unmarks an account. Natively the commands run through `scripts/reset-password.sh`; on Compose, use `docker exec -it deeptempo-backend python scripts/reset_password.py`. Below, `reset_password.py` means either.

```
reset_password.py --mark-break-glass <user>     # mark it, and print 10 fresh codes once
reset_password.py --unmark-break-glass <user>   # remove the mark
```

Each asks for your name, a reason and how you confirmed the person, and records them on the trail.

## Setup

1. **Invite the account in the console** at a role mailbox no identity provider uses (for example `vigil-breakglass@example.com`, not a person's work address), so nobody's everyday SSO sign-in matches it and the provider never reaches it. The custodian sets the password from the invite.
2. **Give it a role that can manage roles.** Marking refuses an account whose role can't, an account linked to the identity provider, a service account, and one that holds live API tokens.
3. **Mark it at the host**: `reset_password.py --mark-break-glass <user>`. It prints 10 recovery codes once. Put them in the safe; nobody can show them again.
4. **Make one test sign-in** right after, on the break-glass route, with a reason such as `Setup test of break-glass account, <date>, <your name>`. It proves the password, a code and the route work before an incident needs them, and that the managers' notice arrives. It is a real use: it spends one code and every manager is told. Give a reason that says it was a test, so nobody reading the trail later takes it for an incident.

Team & access shows a **Break-glass** badge on each marked account. The badge only shows the mark; nothing in the console sets or clears it.

## Mail, and who hears about a use

Each use notifies every account manager in the bell, and by email only when mail is configured (`VIGIL_EMAIL_BACKEND=smtp` and the `SMTP_*` settings). While the identity provider is down, managers who sign in through it can't see the bell. Then email is the only way they hear of a use, and only if their mailbox is not itself behind the identity provider.

- Configure mail wherever an account is marked. Marking without it still goes ahead, and the command says that no manager will hear about uses of or attacks on the account.
- Keep at least one manager on a mailbox not signed into through the identity provider.

When nobody else holds `users.write` or `sessions.manage`, the trail is the only record of a use.

## Codes

The codes come only from the host: marking prints a fresh set, replacing any the account held. Marking an already marked account again is how to issue a fresh set; the old codes stop working.

- A set covers up to 10 sign-ins. Each sign-in spends one code; a sign-in that ends in a forced password change spends a second when the account signs in again.
- The console never issues codes for a marked account. Setting up an authenticator app or adding a security key keeps the codes the host issued. Inside a break-glass sign-in either asks for the password only: the code spent to sign in stands for the second factor.
- "New recovery codes" in the console is refused: "Remove the break-glass mark at the host first".
- Removing a method, turning two-step sign-in off, or an MFA reset stays open. Those can leave the account with fewer factors, and the bell shows it.
- No API token is minted or rotated for a marked account, in the console, through the API, or from the host (`python -m core.auth.mcp_token`). Each is refused with the same message. Revoking a token stays open.

Codes are random from the operating system's generator, carry 64 bits, and are stored only as salted hashes under a password hashing scheme.

### The bell

Holders of `users.write` see a condition in the bell for each marked account that can't sign in now, or has 2 or fewer codes left. It names what is wrong and the command that fixes it. An account can sign in now when it is active and not deleted, has no identity-provider link, has a role that can manage roles, has a password the current key ring can read, and holds at least one unspent code or a security key. Nothing shows while no account is marked.

## After a restore

A restore brings back the backup's marks, codes and passwords. Codes issued since the backup stop working, and codes spent since work again. After any restore:

1. Confirm each mark at the host. Run `--mark-break-glass` for every account that should be marked: it says "still break-glass" for one the backup had marked, and "marked break-glass" for one it didn't. Run `--unmark-break-glass` for any that should not be. Team & access shows the result as the Break-glass badge.
2. Put the codes that run printed in the safe, replacing the old set.

## After a rollback

A rollback is a restore to before the upgrade (see [upgrading](upgrading.md)). If that release predates break-glass accounts, no account is marked afterwards, and Require single sign-on refuses every password sign-in again. The way back is the host off-switch:

```
python -m core.auth.require_sso_off
```

Upgrade again, mark the accounts again, and put the new codes in the safe.

## After an emergency key rotation

`python -m core.auth.key_ring emergency pepper` (or `emergency all`) makes every stored password and recovery code unreadable (see [compromise recovery](compromise-recovery.md#4a-the-pepper-was-lost-or-exposed)). A marked account then can't sign in, and the bell says so. For each marked account:

1. Issue fresh codes: `reset_password.py --mark-break-glass <user>`.
2. Issue a host reset link: `reset_password.py <user>`. The link works for a marked account under Require single sign-on.

## Containment

If a break-glass account or its safe may be compromised:

1. Remove the mark at the host: `reset_password.py --unmark-break-glass <user>`. Its sign-ins end, and under Require single sign-on its password and codes no longer sign in.
2. Deactivate it in Team & access, or delete it.

Then set up a new account as above, with a new password and a new set of codes.
