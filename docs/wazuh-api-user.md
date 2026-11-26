# Wazuh API user for Vigil

Settings › Integrations › Wazuh stores a user of the Wazuh **server API** (port
55000 by default). That is the account Vigil signs in as. Sign-in is HTTP basic
authentication to `POST /security/user/authenticate` (`BasicAuth` in
`WazuhService.authenticate`, `core/integrations/wazuh/client.py`). Vigil never
sends `run_as`, so create the user with **Allow run as** left off
(`allow_run_as` stays false, which is the API default).

Do not use `wazuh` or `wazuh-wui`. Those are the administrative users a manager
creates by default, and `wazuh-wui` is the account the Wazuh dashboard signs in
with. Wazuh's built-in `readonly` role is not enough either: it has no
`logtest:run`, so testing a sample event fails.

This is not an indexer user. Wazuh's "read-only user" guide creates an indexer
user and an indexer role, then a roles mapping. That mapping does nothing unless
the API user already has `allow_run_as` on, which this account must not. Roles
mapping under Server management → Security is that path. Alerts still come from
the indexer: point the Elastic Security or OpenSearch integration at
`wazuh-alerts-4.x-*`.

## What the role allows

One custom policy, on one custom role:

| Action | Resource | What Vigil uses it for |
|---|---|---|
| `agent:read` | `agent:id:*` | `wazuh_list_agents`, `wazuh_list_groups` and `wazuh_agent_log_collection` (`GET /agents`, `GET /agents/{id}/config/logcollector/localfile`). Group counts come from the agents, not `GET /groups` (`group:read`), so a group with no agents is not listed |
| `rules:read` | `rule:file:*` | `wazuh_list_rules` and the rule inventory (`GET /rules`) |
| `logtest:run` | `*:*:*` | `wazuh_test_event` (`PUT /logtest`, then deleting that session) |

Sign-in and `GET /` need no permission. Settings' Test is those two calls.

A resource of `agent:group:<name>` instead of `agent:id:*` leaves Vigil seeing
only the agents in that group. Agent `000` (the manager) is included only when
it belongs to one of those groups.

The password Wazuh accepts is 8–64 characters and contains an upper-case letter,
a lower-case letter, a digit and a symbol.

## With the server API

Sign in as an administrator you already have. On the `wazuh/wazuh-manager` image
that administrator is `API_USERNAME`. Use it only to create the user below, and
save `vigil-reader` in Settings, not the administrator.

```sh
API=https://wazuh-manager.example.com:55000
ADMIN_USER=administrator
ADMIN_PASS='the-administrator-password'
READER_PASS='Change-Me.1'   # replace; 8–64 chars, upper, lower, digit, symbol

TOKEN=$(curl -sk -u "$ADMIN_USER:$ADMIN_PASS" -X POST "$API/security/user/authenticate" \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)["data"]["token"])')

auth() { curl -sk -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" "$@"; }
id_of() { python3 -c 'import json,sys; print(json.load(sys.stdin)["data"]["affected_items"][0]["id"])'; }

POLICY_ID=$(auth -X POST "$API/security/policies" -d '{
  "name": "vigil_reader",
  "policy": {
    "actions": ["agent:read", "rules:read", "logtest:run"],
    "resources": ["agent:id:*", "rule:file:*", "*:*:*"],
    "effect": "allow"
  }
}' | id_of)

ROLE_ID=$(auth -X POST "$API/security/roles" -d '{"name":"vigil_reader"}' | id_of)

auth -X POST "$API/security/roles/${ROLE_ID}/policies?policy_ids=${POLICY_ID}"

USER_ID=$(auth -X POST "$API/security/users" -d "{
  \"username\": \"vigil-reader\",
  \"password\": \"${READER_PASS}\"
}" | id_of)

auth -X POST "$API/security/users/${USER_ID}/roles?role_ids=${ROLE_ID}"
```

The create-user response has `"allow_run_as": false`. Do not call
`PUT /security/users/{id}/run_as`.

## In the Wazuh dashboard

Open the menu (☰) → **Server management** → **Security**. That screen is the
server API for the manager this dashboard is connected to.

1. **Policies** → **Create policy**. Policy name `vigil_reader`. Add each action,
   then each resource (the identifier `*` means all of that resource), effect
   **Allow**, then **Create policy**:
   - `agent:read`, resource `agent:id`, identifier `*`
   - `rules:read`, resource `rule:file`, identifier `*`
   - `logtest:run`, resource `*:*`, identifier `*` (the policy stores `*:*:*`)
2. **Roles** → **Create role**. Role name `vigil_reader`, policies `vigil_reader`,
   then **Create role**.
3. **Users** → **Create user**. User name `vigil-reader`, a password as above,
   the same value in **Confirm Password**, **Allow run as** left off, roles
   `vigil_reader`, then **Apply**.

Save that username and password in Settings › Integrations › Wazuh, with the
manager API URL (`https://<manager>:55000`). Test there signs in and reads
`GET /`.
