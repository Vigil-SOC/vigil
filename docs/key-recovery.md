# Key recovery

Vigil keeps one random key per purpose in the database (the key ring): sign-in tokens, run tokens, reset links, the first-run setup code, authenticator secrets, security-key challenges and single sign-on state. A seventh key, `JWT_SECRET_KEY`, is not in the database: it wraps every ring key. This page covers losing a key, suspecting one is exposed, and what a backup does and does not protect. It is a template: copy it into your own runbook and fill in who does each step.

Everything below runs on the host with `python -m core.auth.key_ring` (under Compose, in the backend container: `docker compose exec backend python -m core.auth.key_ring ...`).

## See the keys

- `status` lists the keys that are not retired.
- `inventory` lists every key, retired ones included, as TSV (`--json` for JSON): purpose, version, state, created, overlap end and retired time. It never prints key material, so its output is safe to attach to a ticket.

## Planned rotation

`rotate <purpose>` makes a new key and lets the old one keep verifying for the purpose's overlap (24 hours or the refresh-token lifetime for sign-in, 31 days for run tokens, an hour for reset links). Nobody is signed out and no run stops.

Vigil also does this by itself for any key older than 12 months. If a rotation fails, the old key stays in use and the holders of `sessions.manage` see a warning in the notification bell until one succeeds. `JWT_SECRET_KEY` is rotated by Vigil only when it lives in the secret store (`secrets.enc`). When it comes from the environment or `.env`, Vigil writes an audit-trail entry 30 days before it is a year old and shows the warning once it is; change it where it is defined (see "Rotating `JWT_SECRET_KEY` yourself").

## Emergency rotation: a key may be exposed

`emergency <purpose|all>` rotates with **no overlap**: the old key stops verifying at once. It also stops every in-flight agent run (reason "emergency key change") and sends one notice to each holder of `sessions.manage`.

| Suspected | Run | Effect |
| --- | --- | --- |
| Sign-in tokens | `emergency signin` | Everyone is signed out and signs in again. Only this key does that. |
| Run tokens | `emergency run` | Runs stop; people stay signed in. |
| Reset links or the setup code | `emergency reset`, `emergency setup` | Outstanding links and codes stop working. |
| Not sure which | `emergency all` | Every purpose; everyone signs in again. |

Replicas pick the change up within 30 seconds. The command waits for the runs' stop backstop (about 30 seconds) before it exits. Afterwards: tell people to sign in again, check `inventory` shows the old versions as retired, and look at the audit trail (`auth.key_rotated`, `auth.key_retired`, with the operator's name).

An emergency rotation of the authenticator-secret key (`mfa`) re-encrypts every secret under the new key; nobody has to enrol again.

### The authenticator key and the database are both exposed

Rotating the key does not help: whoever holds a copy of the database from before the rotation holds the authenticator secrets, and the old key to read them. Treat every authenticator app as compromised:

1. `emergency all`.
2. Clear every person's authenticator app (set `mfa_secret`, `mfa_recovery_codes` and `mfa_last_totp_step` empty, and `mfa_enabled` false for people with no security key) so everyone enrols again, and tell them why.
3. Rotate every password you cannot rule out as exposed; password hashes are in the same database.

## Restoring a backup

A restore signs everyone out (new sign-in and run-token keys, no overlap), and a key that was retired before the restore never comes back, even though the backup holds it: its version number stays spent.

The key ring in a backup is wrapped under `JWT_SECRET_KEY`, so a restore needs that secret:

1. The install's own `JWT_SECRET_KEY` opens the ring: nothing else happens.
2. If it does not, the key saved beside the backup (below) is tried. If it opens the ring, the ring is re-wrapped under the install's secret and the secret itself is not changed.
3. If neither opens it, the restore still completes. The ring is replaced with new keys, so no old token or run token verifies; anyone whose authenticator secret cannot be opened has it cleared and enrols again (a person with a security key keeps two-step sign-in through the key). Passwords are untouched. One notice to the holders of `sessions.manage` says so and gives the count.

### Where the wrapping key is kept

Every backup writes **two snapshots in the same repository**: the main snapshot, whose `.env` copy has the `JWT_SECRET_KEY` line removed, and a small second one tagged `wrapping-key` (and `main:<id of the main snapshot>`) that holds only that value. They share the repository and its passphrase, so this separates the key from the database dump, not from whoever can open the repository. If you want the key held apart from the repository, keep your own copy of `JWT_SECRET_KEY` in a password manager or vault.

A secret that Vigil generated itself into `secrets.enc` stays in the state directory, and the state directory is in the main snapshot. The second snapshot holds the value in force when the backup ran.

Retention follows the main snapshots: a `wrapping-key` snapshot is forgotten once its main snapshot is. Backups made before this change have no second snapshot and restore as before; restoring one onto an install with a different `JWT_SECRET_KEY` replaces the ring (case 3). Helm takes no Vigil backup.

On a restore the `.env` from the snapshot does not overwrite the live `.env`'s `JWT_SECRET_KEY` line.

## Rotating `JWT_SECRET_KEY` yourself

When the secret comes from the environment or `.env`, Vigil does not change it and there is no command for it yet. Changing the value without re-wrapping the ring leaves the ring unreadable: the API refuses to sign anyone in until the old value is back. Keep the old value until a restore from a paired backup, or Vigil's own rotation of a secret held in the secret store, has re-wrapped the ring.

## Checklist for an incident

- [ ] Who decides: ____
- [ ] Which keys: `inventory` before and after.
- [ ] `emergency <purpose>` or `emergency all` run by: ____ at ____
- [ ] People told to sign in again; runs restarted where needed.
- [ ] MFA reset if the authenticator key and database were both exposed.
- [ ] Audit-trail entries reviewed; ticket updated with the `inventory` output.
