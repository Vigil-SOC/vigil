# Key compromise: a recovery plan template

Copy this page, fill in the blanks for your site, and keep the copy where the on-call person can read it without Vigil running. It covers Compose and host installs. Helm takes no Vigil backup (see [upgrading](upgrading.md)), and its keys come from the chart's Secret.

Vigil keeps one key per purpose in a key ring: `signin`, `run`, `setup`, `mfa`, `pepper`, `webauthn` and `sso`. Password reset links are not signed: each is a database row, revoked from Settings > Users or closed by any change to the account's password, email or status. The ring is stored in the database, wrapped under `JWT_SECRET_KEY`, which stays outside it. All the commands below run on the host (or in a one-shot of the backend image) as `python -m core.auth.key_ring`.

| Who | Name | Contact |
| --- | --- | --- |
| Incident lead | | |
| Holder of `sessions.manage` | | |
| Holder of the host and `.env` | | |

## 1. See what is there

```
python -m core.auth.key_ring status       # keys that are not retired
python -m core.auth.key_ring inventory    # every key, retired too; add --json for JSON
```

The inventory lists purpose, version, state, created, overlap end and retired time. It never prints key material. Attach it to the incident record.

## 2. Rotate in an emergency

A normal rotation keeps the old key verifying for an overlap, so nobody is signed out. An emergency rotation has no overlap: the old key stops working at once.

```
python -m core.auth.key_ring emergency signin   # one purpose
python -m core.auth.key_ring emergency all      # every purpose
```

Every emergency rotation also stops every running agent run (reason "emergency key change") and sends one notice to each holder of `sessions.manage`. Replicas pick the change up within 30 seconds.

| Rotated | What happens |
| --- | --- |
| `signin` | Everyone is signed out. This is the only purpose that does so. |
| `run` | Tokens held by runs stop working. The runs are stopped anyway. |
| `setup`, `webauthn`, `sso` | Links, codes and challenges in flight stop working; people ask again. |
| `mfa` | Authenticator secrets move under a new key. This does not help if the database was copied (see 4). |
| `pepper` | Every earlier pepper key is retired and nothing is re-encrypted, so every stored password and recovery-code hash becomes unreadable. Everyone must reset their password (see 4a). |

Choose by what leaked:

- **A sign-in token or the `signin` key**: `emergency signin`.
- **A run token, or the agent layer's host**: `emergency run`, then `emergency signin` if the host also held sessions.
- **The pepper, or a database copy together with the ring**: `emergency pepper` (see 4a). It makes everyone reset their password.
- **Not sure, or the host was taken over**: `emergency all`, then 3. `all` includes `pepper`, so everyone resets their password.

## 3. The wrapping key (`JWT_SECRET_KEY`)

The ring is only as secret as `JWT_SECRET_KEY` plus the database. If both were exposed, rotating ring keys does not help, because the attacker can read the new keys as they are written. Replace the wrapping secret first:

1. Set a new `JWT_SECRET_KEY` where it is defined (`.env`, the shell Compose reads, or the secret store).
2. Run `python -m core.auth.key_ring rewrap`. It asks for the previous value and wraps the ring under the new one. (A secret that Vigil keeps in `secrets.enc` rotates by itself after 12 months.)
3. Run `emergency all`.

Vigil rotates every key older than 12 months on its own, with its normal overlap. When a rotation fails the old key stays and the bell warns people who hold `sessions.manage`. A `JWT_SECRET_KEY` that is set outside Vigil cannot be changed by Vigil: the trail notes it 30 days before 12 months, and the bell warns once it is overdue.

## 4. The `mfa` key and the database were both exposed

The authenticator secrets are encrypted under the `mfa` key. Someone holding both can read every secret, and rotating the key does not make the copies they hold useless. Everyone with an authenticator app has to enrol again:

1. `python -m core.auth.key_ring emergency all`.
2. `python -m core.auth.key_ring reset-mfa --yes` clears every authenticator app and recovery code. A person who has a security key keeps MFA through it.
3. Tell people to set the app up again at their next sign-in.

For one person, reset their two-step sign-in from Settings > Users, or on the host with `python scripts/reset_password.py --reset-mfa <user>`.

A restore that cannot open the ring does this itself: it replaces the ring, clears the authenticator secrets nobody can read, and the restore notice says how many people must enrol again, and how many must reset their password (see 4a).

## 4a. The pepper was lost or exposed

Passwords and recovery codes are stored encrypted under the `pepper` key. A normal `rotate pepper` re-encrypts them under a new key and keeps the old one, which does nothing if the old key and the database leaked together. `emergency pepper` instead retires every earlier pepper key and re-encrypts nothing: the stored hashes can no longer be read by anyone, a stolen copy included.

What people see, and what you do:

- **Sign-in.** A person whose password can no longer be read is told their password must be reset. The attempt is not a wrong password: it adds nothing to the failed count and never locks the account. The trail records `auth.sign_in_failed` with the reason `password_reset_required`.
- **Reset.** The usual reset link works without reading the old password, and the minimum password age does not apply. With mail on, the person uses Forgot password. With mail off, issue a link from the host: `scripts/reset-password.sh <username>` (Compose: `docker exec deeptempo-backend python scripts/reset_password.py <username>`). The `auth.password_reset` trail entry counts the history entries it could not read (`history_skipped`).
- **Recovery codes.** They are removed at the reset when they can't be read. The person makes new ones in their two-step settings; the authenticator app and security keys are not touched.
- **Service accounts** are not affected: their gateway writes its password again at its next start.
- **A restore** whose ring cannot be opened (4, 5) replaces the pepper too. The restore notice counts the people who must reset their password, and the restore's trail entry records it as `passwords_to_reset`. A pepper version retired before the restore stays retired after it.
- **`recover_admin`** gives a role, never touches a password. If the person it names must also reset their password, issue the link as above.

## 5. Backups and the wrapping key

Each backup writes two snapshots to the same restic repository: the backup, and a small second one tagged `wrapping-key` that holds only `JWT_SECRET_KEY`. The backup's own copy of `.env` has the `JWT_SECRET_KEY` line removed, so the key and the ring it wraps are not in the same snapshot.

This is a different snapshot, **not a different repository**: whoever can read the repository and its passphrase can read both. If that is not enough for you, keep the passphrase and the repository apart from the people and hosts that can reach the database, or copy `JWT_SECRET_KEY` to a safe of your own. A key that Vigil generated into `secrets.enc` stays in the State Directory, so it is in the backup itself.

Restore uses the install's `JWT_SECRET_KEY`, and if that does not open the restored ring, the paired snapshot's key, re-wrapping the ring under the install's. If neither opens it, the ring is replaced as in 4. A backup taken before this change has no paired snapshot and restores as before.

A key retired before a restore stays retired after it, and its version number is never used again.

## 6. After

- [ ] Inventory taken again and attached.
- [ ] The trail reviewed for `auth.key_rotated`, `auth.key_retired` and `auth.emergency_rotation` entries.
- [ ] Integration credentials and API tokens revoked or rotated where the incident touched them.
- [ ] A fresh backup taken, so the paired `wrapping-key` snapshot holds the new secret.
