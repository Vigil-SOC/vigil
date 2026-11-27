# Acts

An act is a containment Vigil carries out once a person, or the autonomy
ceiling, approves it: isolate a host, disable an account, block an address or a
domain, end an account's sessions, quarantine a file. Which integration carries
an act is a **carrier**, a binding on the act tool's own row. A person sets it
with `PUT /api/v1/autonomy/tools/{name}` (`binding`); Vigil ships the carriers
for Defender, Carbon Black, Entra ID, Cloudflare (WAF, Gateway, Access) and Palo
Alto in `core/integrations/bindings.yaml`. A person's binding replaces the
shipped one for that tool; `{}` binds nothing.

The ceiling, the crown-jewel hold, a person's reversal hold, the hourly cap and
a workflow's step bar key on the act, never on the server that carries it.

## The carrier

A carrier sits on the act tool's row. Every tool it names is a bare name of
that tool's own server (`mde_find_machine`, not
`microsoft-defender_mde_find_machine`), and a server carries at most one carrier
per act kind. A carrier missing a part its act needs is refused with a 422
naming the part.

| Field | | |
|---|---|---|
| `act` | string | The act kind, one of the table below. |
| `carrier` | string | Optional. The label a case and a refusal show, such as `Cloudflare WAF`. The server's name otherwise. |
| `id_arg` | string | The act tool's argument for the vendor's ID of the target. |
| `note_arg` | string | The act tool's argument for Vigil's note (`Vigil <action id>: <title>`). |
| `act_state` | reading | What the act's answer says. |
| `state_read` | `{tool, arg}` | Optional. A tool that reads the act's state again, called with `arg` set to the act answer's field of the same name. |
| `read_state` | reading | With `state_read`: what its answer says. |
| `undo` | string | Optional. The tool that undoes the act. An act kind with no undo verb takes none. |
| `undo_from` | string | Optional, with `undo`. A field of the act's answer the undo is called with, under the same name (Cloudflare's `rule_id`). Without it the undo takes `id_arg` and `note_arg`, as the act does. |

A target the vendor knows by its own ID is **looked up** first:

| Field | | |
|---|---|---|
| `lookup` | string | The lookup tool. |
| `lookup_arg` | string | Its argument for the target. |
| `address_arg` | string | Optional. Its argument for the target when the target is an address. |
| `rows` | string | Optional. The list the lookup answers in. Omitted, the answer is one record, or none when it says `found: false`. |
| `id` | string | Each record's vendor ID, passed to the act as `id_arg`. |
| `name` | string | Each record's name, shown to the person choosing. |
| `state` | reading | What a record says. With no `state_read`, a requested act holds once the lookup reads the act's done state. |

A **keyless** target is the vendor's key itself (an address, a domain, an email)
and has no lookup:

| Field | | |
|---|---|---|
| `takes` | list of strings | The target kinds the act takes: `address`, `network`, `domain`, `email`, `url`, `hash`, `path`, `account`, `object_id`, `single_label`, `text`, `not_an_address`, `token`. Any other target is refused on the row, in a sentence made from the label. |

### A reading

```json
{
  "read": [
    {"field": "isolation.pending.status", "map": {"Pending": "requested", "InProgress": "requested"}},
    {"field": "isolation.state", "map": {"isolated": "contained"}}
  ],
  "default": "not_contained"
}
```

Each entry reads one dotted path of the answer. The first entry whose value is
a key of its map gives its state; with none, the `default` does. A reading with
only a default reads every answer alike. A boolean value matches the key `true`
or `false`, a number its digits.

Every reading answers one state word: `requested` while the vendor still works
on it, `failed` when it refused, or the act's own words below.

## The state words

| Act | Done | Undone | Lookup's word once undone | Undo verb |
|---|---|---|---|---|
| `isolate_host` | `contained` | `released` | `not_contained` | Release |
| `disable_user` | `disabled` | `enabled` | `enabled` | Enable |
| `block_ip` | `blocked` | `unblocked` | `unblocked` | Unblock |
| `block_domain` | `blocked` | `unblocked` | `unblocked` | Unblock |
| `revoke_session` | `revoked` | none | none | none |
| `quarantine_file` | `quarantined` | `restored` | `restored` | Restore |

`quarantine_file` ships with no carrier: until one is bound, a proposed
quarantine stays a proposal for a person.

## Already in force

Before a run's proposal is filed, each connected lookup that reads the act's
state is asked whether the target is already in it: a carrier's lookup, and a
read-only state lookup in `core/integrations/state_reads.yaml` on a server
that carries none of the act (Falcon's host search, for `isolate_host`). When
every record found reads as done, nothing is filed and the case notes what the
vendor reported; when any record is not done, or a lookup can't tell, the
proposal is filed for a person.

A state lookup takes the same reading as a carrier. Its `template` and
`address_template` fill the lookup argument with the target quoted, as in
`hostname:'{target}'`. Falcon matches the name exactly: a short name against
a host Falcon knows by its full name finds nothing, and the proposal is filed.
A state lookup is never called to act.

## Changes

This form changes only by addition: a field is never removed or given a new
meaning, so a carrier declared today keeps working.
