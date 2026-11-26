# Entity facts

When triage looks at a finding, Vigil asks the identity provider and the EDR who its users and hosts are: name, title, groups, manager, account state, and the host's platform and containment. The answers are kept for a short time so the next finding about the same user or host does not ask again. This page says what is fetched, from where, how long it is kept, what you can see of it, and what outlives it.

Facts inform triage. Nothing branches on them: they go into the triage prompt and its stored result, never into a gate, a ceiling or a verdict.

## Seeing what is held

| Where | What |
|---|---|
| **Settings › Entity facts** | Per source: rows held, oldest and newest fetch, hit rate and last failure. The limits in force. The newest 100 rows, with their age. |
| `GET /api/v1/entity-facts?limit=100` | The same, as JSON. `limit` is 1 to 500. |
| `get_entity_facts` on `/mcp` | The same payload, read only. |

All three need `findings.read`, and none of them changes anything. A row tied to a case is shown, and counted, only if you may read that case under the restricted-case rule. A reader of open cases alone sees no row from a restricted case and no count that includes one.

The **hit rate** is the lookups triage served from a held row, out of every lookup it made, over the last time-to-live window (24 hours by default). The window is in the response as `window_hours`. It and the **last failure** come from the `entity_facts.read` entries on the audit trail, one per triage read. A failure shows its kind only (`timeout`, `connect_failed`, `not_enabled`, …): the trail never keeps the source's own error text, so neither does this view. A source that is paused after a failure shows `skipped`. A source that is not set up or turned off is asked on every read too: it counts as unanswered and shows `not_configured` or `not_enabled` as its last failure.

There are no edit or delete controls. Limits change through system config (below). Rows leave by their time to live or when their case closes.

## What is fetched, and from where

Each source has one projection in `core/integrations/entity_facts.py`, and that projection is the allowlist. Only these fields are stored; anything else the source returns is dropped before the row is written. An empty field is not stored.

**Entra** (`azure-ad`, for user and email entities), from Microsoft Graph `GET /users/{id}` and `/users/{id}/memberOf`:

| Field | From |
|---|---|
| display name | `displayName` |
| user principal name | `userPrincipalName`, else `mail` |
| title | `jobTitle` |
| user type | `userType` |
| account | `accountEnabled`, as enabled or disabled |
| manager | the manager's `displayName`, else `userPrincipalName` |
| groups | the group display names |

**Okta** (`okta`, for user and email entities), from `get_okta_user` and `list_okta_user_groups`:

| Field | From |
|---|---|
| display name | `profile.displayName`, else first and last name |
| login | `profile.login` |
| title | `profile.title` |
| manager | `profile.manager` |
| status | `status` |
| groups | each group's `profile.name` (left out if the tool is missing or its call fails) |

**Falcon** (`crowdstrike`, for host entities), from `falcon_search_hosts`, first match only:

| Field | From |
|---|---|
| hostname | `hostname` |
| platform | `platform_name` |
| os | `os_version` |
| containment | `status` |
| last user | `last_login_user` |
| tags | `tags` |
| host group ids | `groups` |
| last seen | `last_seen` |

A user or host the source does not know is stored as `found: false`.

**Never stored**, because no projection names them: passwords and their hashes, MFA factors and seeds, recovery codes, keys and other credentials, custom profile attributes and extension attributes, and HR, health or compensation data.

**Declared** rows come from no source. When a site is declared (`site/*.md` in the declared directory), each read also carries one row for every zone, crown jewel, owner or blind spot whose selector matches a key the finding or run names, `ip:` keys included: its kind, name, mission or what is unseen, zone, owner (resolved against active teams, or `unassigned: no team named …`), selector, file, line and revision. They are read from the applied revision each time, never stored in `entity_facts`, never counted against a source or the key limit, and a key the read would otherwise leave out joins it only when a declared item matched. A hunt reads these rows only (`read_entity_facts` with `declared_only`), and asks no source.

## Recommended read-only scopes

Vigil only reads. Give each source a credential that can do nothing else.

| Source | Grant |
|---|---|
| Entra | Microsoft Graph application permissions `User.Read.All` (the user and their manager) and `GroupMember.Read.All` (group names). If `memberOf` is denied, the other fields still come back. |
| Okta | An API token of a **Read-only Administrator**, or, for OAuth, `okta.users.read` and `okta.groups.read`. |
| Falcon | **Hosts: Read** for host facts, and **Alerts: Read** for alert collection and the credential read. Hosts: Read is also optional for the Day 1 Report's counts of hosts CrowdStrike still lists, and it lets agents search Falcon hosts. |

For Falcon, that same API client is also the CrowdStrike tool server's credential: agents can call that server's other tools (for example `falcon_search_detects`, which the hunt resolver prefers), and each tool needs its own scope in CrowdStrike and is refused without it.

## How long it is kept

- **Time to live: 24 hours** (`ttl_hours`). Past it a row is not served; the next read fetches it again.
- **Cleanup.** The daemon's daily cleanup sweep deletes rows past their time to live and rows whose case has closed, and writes one `entity_facts.sweep` entry with the counts. Between sweeps an expired row can still be held; the Settings view marks it "expired, next sweep".
- **Row cap: 10,000** (`max_rows`). Past it, the oldest rows go first.

Rows written by triage carry no case. Rows tied to a case will be written by runs on that case (#4742); the view already filters them by the restricted-case rule.

## The limits, and raising one

| Limit | Default | Range | Means |
|---|---|---|---|
| `ttl_hours` | 24 | 1–720 | How long a row is served |
| `max_rows` | 10000 | 100–1000000 | Rows held at most |
| `max_keys` | 10 | 1–100 | Users and hosts looked up for one finding |
| `calls_per_minute` | 30 | 1–1000 | Tool calls a minute, per source |
| `budget_seconds` | 5 | 1–60 | How long triage waits for the whole read, connecting included |
| `failure_skip_seconds` | 300 | 0–3600 | How long a failed source is not asked again |

They are stored as one object under the system config key `entity_facts.limits`, read on each use. A field left out reads as its default. A value outside its range also reads as the default, and is logged as an error. To change one, set the key with your name and a reason, so the change is in config history:

```bash
docker exec deeptempo-backend python -c "
from core.storage.config_service import get_config_service
get_config_service('you@example.com').set_system_config(
    'entity_facts.limits', {'max_keys': 20},
    description='Entity facts limits', change_reason='Wide findings')
"
```

Name every field you want changed from its default: the object replaces the stored one. Settings › Entity facts shows the new value, marked **Changed**, on the next load.

## What outlives the cleanup

Deleting a row from `entity_facts` does not remove every copy of what it said:

- **Triage's stored result.** The facts triage was shown are kept on the finding, under `ai_triage.result.entity_facts`, so a citation of a fact stays checkable after the row expires. They live as long as the finding.
- **A run's journal.** Once runs read entity facts (#4742), a run journals what it read in its Ledger, which is append-only and is the run's record. Facts a run journaled stay there after the row is gone.
- **The audit trail** holds counts and reason kinds per read, and the entity keys asked for in its erasable `personal` column. It holds no fact values.

A person’s erasure does not yet reach facts kept on a triage result or in a run’s journal.
