# Audit trail

The audit trail is one hash-chained table of what happened on this install: who did it, for whom, and what came of it. Settings › Audit reads it. This page states what that record does and does not keep.

## Rows already written stay as written

A row in the trail is not rewritten. The application may insert a row, and it may update only the columns that sit beside the hashed content: source address, the actor text, the operator text, the attempted name, and personal details. The sequence, the time, the late flag, the payload and the hashes are assigned when the row is inserted and are not updated afterwards.

Erasure blanks those beside-columns for a principal, and blanks the same details on a row that is still waiting to be recorded. A name or any other detail already inside the hashed payload of an older row stays there. The chain still verifies after the beside-columns are blanked.

Erasure also rewrites the account's username, email and full name to `erased-<principal id>` placeholders, and rewrites that username wherever a record outside the trail stores it as text: case assignees, comment authors and mentions, evidence collectors and chain-of-custody entries, case links and closures, IP exclusions, finding dismissals, detection proposals, rule stages, exceptions and links, approvals, workflow runs, memory notes, agent directives and the setup record. Case watches and notices addressed to that username are deleted. Free text a person typed, such as a comment that names someone, is not rewritten.

A username can't be, or be spelled like, a principal id (`user-` and 12 hex digits, `agent:…`, `system`, or any existing principal's id), so a name in the trail never reads as another principal. Memory notes and their withdrawal, history imports and their deletion, and rule links made through chat or MCP name their actor by principal id. Entries of those actions written by 1.1.3 and earlier name the person by username instead: the actor filter finds them by that username, not the principal id, and erasure does not reach them. To find accounts created before this check whose username collides with a principal id, run `SELECT username, principal_id FROM public.users WHERE username = 'system' OR username IN (SELECT principal_id FROM public.principals) AND username <> principal_id;` and deactivate any it lists.

Erasure needs `audit.manage`. Erasing a principal that has an account also deactivates that account, so it asks what deactivating does as well: `users.write` with "Confirm it's you", a role that grants everything the account's role grants, and someone left who can manage roles. An erasure that waited seven days stays waiting if applying it would leave nobody holding `roles.manage`.

Once a retention period is in force, entries older than that period are removed from the oldest end of the trail, as a prefix, and the chain's anchor moves to the last removed entry. That removes rows. It does not rewrite a row that remains. The period is at least 365 days and takes effect 7 days after it is set.

The agent ledger (`agent_events`), one chain per run, is separate. The application may read it and insert into it. It may not update or delete a ledger row. Erasure and trail retention do not change it.

## Backups keep what erasure removed until they age out

Erasure changes the live database. A backup taken before that still holds the details erasure removed, until that backup ages out under its own retention.

A scheduled backup to a local restic repository keeps the last `keep_last` snapshots tagged manual or scheduled (14 if that is not set) and forgets the older ones with those tags. A pre-upgrade snapshot is separate: the newest is always kept, and an older one is dropped only after a later upgrade has been applied. A repository on S3, Google Cloud Storage or Azure is not forgotten by Vigil; that bucket's own lifecycle rules decide how long a snapshot stays. The dump directories written by `scripts/vigil-backup.sh` keep the last 14 runs unless `--keep` says otherwise. An off-site copy of those dumps is kept wherever it was copied.

After a restore, Vigil applies its list of erased principals again. It unions the list stored with the snapshot and the list kept outside the database, then blanks each of those principals on the restored database. A snapshot that names erased principals cannot be restored until that outside list is set, so the restore does not come up with the blanking undone.

## A copy that has left the box is yours to govern

Vigil's retention and erasure reach the live database. They do not reach a copy that has already left:

- the syslog feed's destination;
- the webhook's destination;
- an export;
- a poll.

Those copies keep what they received. A later erasure, or a prefix removed for retention, does not blank or delete them. Whoever runs the destination, the export file or the system that polled governs that copy, including how long it is kept.

## Nothing runs through the API while the database is down

Every API request that acts, a safety action included, reads or writes the database. Signing in, deactivating an account, ending sign-ins and the rest of those actions are rows in that database. While the database itself is down, the request cannot complete, so the action does not run. There is no queue that performs it later.

## An entry the trail cannot take is held, then recorded late

Some actions go ahead even when the trail refuses the entry (a check that rejects the insert, for example, while the database is still up). The entry is then held in `audit_trail_held`, a table in the same database, in its own transaction. That row survives a restart and is visible to every replica. The leader writes it into the trail with its original time and the late flag, which the Audit screen shows as "Recorded late". Settings › Audit and the feed health say how many entries are waiting and the time of the oldest. An open collapse window, and a marker that only records that an expired credential was already noted, are not waiting entries and are not counted.

If the hold also fails, the application log is the only record of that entry. Vigil does not spool it to disk.

## Accounts do not end on a date

Vigil has no emergency account, and no account with a set end date. An account made for temporary access stays active until someone deactivates or deletes it, or until the dormancy rule deactivates it.

Dormancy is `dormancy_days` on the session policy. It defaults to 35 and can be turned off. While it is on, a person's account is deactivated after that many days with no newer activity. Activity is the latest of the last sign-in, the last use of a personal MCP token, the account's creation, and a floor. The floor is when the upgrade that added dormancy ran, for an account that already existed, or the account's last reactivation. A failed sign-in does not count as activity. A service account is not deactivated for being idle. The sweep warns the account's holder and the managers first, then deactivates once that warning has been out for the warning window (7 days, or less when the period is shorter). For 7 days after a restore settles, the sweep warns and deactivates nobody. The last person who can manage roles is not deactivated for dormancy; the managers are told instead. A password's maximum age, and a token's lifetime, end that password or that token. They do not end the account.

## OAuth on the MCP surface

With OAuth on (see [OAuth on the MCP surface](mcp-oauth.md)), the authorization server writes these entries. Each one is filtered as the `oauth` area in Settings › Audit.

| Action | Shown as | Written when | Actor |
|---|---|---|---|
| `oauth.client_registered` | Registered an OAuth client (name) | A client registers at `/register`. Carries the `client_id`, the name and the redirect URIs. The caller's address sits beside the payload. | none (registration is anonymous) |
| `oauth.client_registration_refused` | Refused an OAuth client's redirect URI …; allow it in mcp.oauth_redirect_allowlist | A registration names a redirect URI that is neither loopback nor on the allowlist, or that carries a fragment. Carries the `redirect_uri` as Vigil parsed it; allowing that string lets the client register. A registration refused for its size (over 10 URIs, an over-long URI, or the 1,000-client cap) writes no entry. | none |
| `oauth.consent_granted` | Approved an app's request to connect | A person approves on the consent page. | that person |
| `oauth.consent_denied` | Denied an app's request to connect | A person denies on the consent page. | that person |
| `oauth.grant_issued` | Connected an app | The client exchanges its code and the grant is created. Carries the grant's `credential_id`, the `client_id` and the expiry. | the person who approved |
| `oauth.refresh_reuse` | Ended an app's connection: an old refresh token was presented again | The refresh token replaced by the latest rotation comes back. The grant ends and its holder is told. An older one only gets `invalid_grant`. | system |

A grant ended any other way is the ordinary token entry: `credential.revoked` (from the tokens list, or with reason "by the app" when the client calls `/revoke`), or the unused-token rule's revocation. A grant that reaches its expiry writes no entry; its tokens stop working.
