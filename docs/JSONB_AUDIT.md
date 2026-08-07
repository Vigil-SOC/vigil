# JSONB column audit

Classification of every `JSONB` column in the ORM as **keep** (legitimately
schemaless) or **promote** (typed data stashed in JSON, hiding structure from the
ORM, the API contract and any query). Answers issue #468.

## Method and scope

**Audited artefact: `database/models.py` — the ORM.** That is the effective
schema, because `create_all` still builds any table absent from
`database/init/*.sql`.

**The column count is 54, not the 74 in the issue title.** 54 `JSONB` column
declarations across 32 model classes. 74 appears to be a count of `JSONB`
*occurrences* in the file (69 today, and the same 69 at `8abecc2` / v0.4.0, so
nothing was deleted to explain the gap) rather than column declarations.

**Verdicts are grounded in the `Mapped[...]` annotation plus at least one real
reader or writer**, not the column name. Where a shape could not be confirmed
from a call site, the verdict says so rather than guessing.

| Verdict | Count |
|---|---|
| **Keep as JSONB** | 39 |
| **Promote to typed** | 15 |

---

## Promote (15)

Grouped by the migration each needs, because the shape of the fix — not the
table it lives in — is what makes these separable pieces of work.

### A. Scalar arrays → `ARRAY(TEXT)` (6)

Lists of plain strings. JSONB buys nothing here and costs indexability: you
cannot put a GIN index on `?| ARRAY['x']` semantics as cheaply as on a real
text array, and the ORM surfaces them as untyped `list`.

| Column | Annotation | Rationale |
|---|---|---|
| `users.password_history` | `Mapped[List[str]]` | List of prior bcrypt hashes, newest first, capped at `PASSWORD_HISTORY_LIMIT`. Homogeneous strings; `backend/api/auth.py:158-162` slices it like a list. |
| `users.mfa_recovery_codes` | `Mapped[list]` | `AuthService._generate_recovery_codes` is documented as returning "(plaintext, bcrypt-hashed) lists of 10 one-time recovery codes" — a flat list of hash strings. |
| `skills.required_tools` | `Mapped[List[str]]` | MCP tool names. Promoting enables the query the current shape forbids: "which skills require `splunk.search`?" |
| `custom_agents.recommended_tools` | `Mapped[list]` | Same content as above, same argument. |
| `workflow_runs.skill_tools_available` | `Mapped[list]` | Written from `skill_tool_names` (`services/workflows_service.py:740`) — a name list. |
| `custom_workflows.trigger_examples` | `Mapped[list]` | Example trigger phrases. **Shape inferred from name and `or []` handling, not confirmed from a writer that constructs the elements** — confirm before migrating. |

### B. One-to-many relations → child tables (7)

These are collections of records with their own identity, ordering and
timestamps, stored as a JSON array on the parent. That costs three things:
rows cannot be indexed, filtered or paginated; a concurrent append is a
read-modify-write of the whole array and can silently lose the other writer's
entry; and there is no FK to attribute an entry to a user or finding.

| Column | Annotation | Rationale |
|---|---|---|
| `case_evidence.chain_of_custody` | `Mapped[List[dict]]` | **Highest priority.** An evidence chain of custody is an append-only legal record. In JSONB it cannot be constrained append-only, cannot be indexed by actor or time, and every append rewrites the array — so two concurrent custody events can drop one. |
| `findings.mitre_predictions` | `Mapped[dict]` | `{technique_id: confidence}` — a typed map read by `services/graph_builder_service.py:334`. A child table `(finding_id, technique_id, confidence)` enables "findings by technique", which is core SOC triage and currently impossible without a full scan. Highest query value of the set. |
| `cases.timeline` | `Mapped[List[dict]]` | Append-only case events, rendered chronologically. Same concurrency and indexing argument. |
| `cases.notes` | `Mapped[List[dict]]` | Analyst notes — want author FK, edit timestamps, and pagination. |
| `cases.activities` | `Mapped[Optional[List[dict]]]` | Same family as `timeline`; worth deciding whether these two are one table with a discriminator rather than two. |
| `cases.resolution_steps` | `Mapped[Optional[List[dict]]]` | Ordered steps with completion state — ordering and state belong in columns. |
| `case_tasks.checklist_items` | `Mapped[Optional[List[dict]]]` | Individually checkable items; each needs its own state and ideally its own FK. |

### C. RBAC → `role_permissions` join table (1)

| Column | Annotation | Rationale |
|---|---|---|
| `roles.permissions` | `Mapped[dict]` | A flat `permission-name → bool` map (`database/init/06_auth_tables.sql:59+`). **The promotion is semantically lossless:** `AuthService.check_permission` is `role.permissions.get(permission, False)`, so an absent key already means denied and the stored explicit `false` entries are documentary only. Promoting makes "which roles can approve AI decisions?" a query instead of a scan. **Security-critical — see the caveat below.** |

### D. Promote pending shape confirmation (1)

| Column | Annotation | Rationale |
|---|---|---|
| `investigations.trigger_ids` | `Mapped[List[dict]]` | The annotation says `List[dict]` but the name and use (`daemon/orchestrator.py:950`, `inv.trigger_ids or []`) suggest a list of finding-id strings. **Annotation and name disagree — resolve which is true before choosing `ARRAY(TEXT)` versus a join table.** That disagreement is itself a finding: the ORM type is not trustworthy here. |

---

## Keep as JSONB (39)

Grouped by *why*, since "it's a dict" is not a reason.

### Open vocabulary defined by code, not by data (1)

The key set is not a fixed list of flags — it is whatever a caller passes at
runtime, so typed columns would cap an open set and cost a migration per new key.

`case_watchers.notification_preferences` — **corrected verdict; this column was
originally classified as "promote to real columns" and filed as
[#553](https://github.com/Vigil-SOC/vigil/issues/553).** Tracing the reader
before designing columns reversed it. `services/case_notification_service.py`
reads the column as `prefs.get(notification_type, True)`, where
`notification_type` is a **runtime argument**, so the honoured keys are exactly
the set of types passed to `notify_watchers` — today `new_comment` and
`sla_warning`. Nothing declared that set anywhere, which was the real defect;
#553 shipped it as `WATCHER_NOTIFICATION_TYPES` plus API-boundary rejection of
keys the lookup cannot honour, with no schema change.

Two further reasons the promotion would not have paid:

- **No row has data.** `add_watcher` is insert-only and returns early on an
  existing row, discarding incoming preferences; there is no PATCH route; the
  frontend posts `{user_id}` only. The migration would have moved nothing.
- **The mechanism does not apply.** `case_watchers` has no DDL anywhere — the
  table comes from `Base.metadata.create_all()`, not `database/init/`. See
  cross-cutting finding 5.

*Method note:* the original verdict was drawn from the annotation and the column
name without tracing the read. That is the one place this audit did not meet its
own stated bar, and it is why every remaining promotion should enumerate its
readers before columns are designed.

### Arbitrary third-party payloads (8)
Shape is owned by an external vendor or tool and changes without our involvement.

`threat_indicators.raw_stix` (raw STIX from the feed) · `case_iocs.enrichment_data`
(VirusTotal / Shodan / OTX all differ) · `case_attachments.scan_details` (AV
scanner output) · `case_evidence.analysis_results` (output of whichever forensic
or malware-analysis tool ran — one shape per tool) · `attack_layers.layer_data`
(MITRE Navigator layer — an external interchange format) ·
`llm_interaction_logs.request_messages` · `llm_interaction_logs.tool_calls` ·
`llm_interaction_logs.tool_results` (provider message wire formats).

### Per-instance configuration, polymorphic by design (8)
The whole point is that each row's shape differs.

`system_config.value` · `user_preferences.preferences` · `integration_configs.config`
· `llm_provider_configs.config` · `ai_model_configs.settings` ·
`federation_sources.cursor` (the column comment already says "adapter-defined") ·
`config_audit_log.old_value` · `config_audit_log.new_value` (snapshots of the
above — necessarily as loose as what they snapshot).

### Model output (2)
`findings.ai_enrichment` — the LLM enrichment record. Its keys track the prompt's
requested schema and it deliberately carries `raw_response`; pinning it to columns
would make every prompt change a migration. `ai_decision_logs.decision_metadata` —
open-ended by name and contract.

### Heterogeneous by source (2)
`findings.entity_context` — genuinely irregular: sources disagree on singular vs
plural (`src_ip` vs `src_ips`), on `dst` vs `dest`, and integrations graft
sub-objects in (`entity_context["vstrike"]`). `findings.evidence_links` — small,
display-only link list; promoting adds a table without enabling a query anyone
makes.

### Documents versioned as a unit (7)
Authored artefacts where the array *is* the thing, edited and saved whole.

`skills.input_schema` and `skills.output_schema` (literally JSON Schema documents
— promoting a schema to columns is a category error) · `skills.execution_steps`
(the interpreted skill program) · `custom_workflows.phases` (workflow definition)
· `case_templates.task_templates` · `case_templates.playbook_steps` (template
bodies) · `sla_policies.escalation_rules` (rule DSL).

### Per-invocation context and results (9)
Contents depend entirely on which trigger fired or which action ran.

`workflow_runs.trigger_context` · `workflow_run_phases.input_context` ·
`workflow_run_phases.output` · `approval_actions.parameters` ·
`approval_actions.evidence` · `approval_actions.execution_result` ·
`investigations.proposed_actions` · `investigation_logs.details` ·
`case_notifications.notification_metadata`.

### UI state (1)
`custom_workflows.graph_layout` — canvas coordinates. No query will ever filter on
it.

### Provider-shaped conversation data (1)
`chat_messages.tool_calls` — same wire format argument as the interaction logs.

---

## Cross-cutting findings

These came out of the audit and matter more than several individual verdicts.

**1. The ORM and the init SQL disagree about the schema.** The ORM declares 54
JSONB columns across 32 classes; `database/init/*.sql` declares 27 across 16
tables. The gap exists because `create_all` still builds tables absent from the
init SQL — exactly what #411 (adopt Alembic, retire `create_all`) sets out to
end. Any promotion must decide which artefact it is changing, and a promotion
touching a table that exists in *both* has to change both consistently.

**2. Promotions that touch `database/init/` inherit the Helm checklist.** Per
`CLAUDE.md`, the chart bundles a *copy* under `helm/vigil/files/database-init/`.
A new or modified init file must be copied there, added to
`helm/vigil/values.yaml` `dbInit.sqlFiles` **in execution order**, and verified
with `helm template ... | grep -E '^[[:space:]]*apply "NEWFILE\.sql"'`. Skipping
the copy while keeping the filename in `dbInit.sqlFiles` hard-fails the dbInit
Job at runtime. This belongs in every follow-up issue, not just this document.

**3. `roles.permissions` is security-critical.** `get_user_permissions` returns
`Dict[str, bool]` and the SPA gates UI on it, so the API must keep serving that
shape even after the storage becomes a join table — derivable, but it means the
migration is not purely internal. Migrate this one on its own, behind tests that
assert the same allow/deny outcome for every seeded role before and after.

**4. `investigations.trigger_ids` has an untrustworthy ORM type.** Annotated
`List[dict]`, used as if it were a list of ids. Worth a grep of the writers
before anyone relies on either reading.

**5. Half the promotion targets have no init SQL at all, so the numbered-file
mechanism does not reach them.** Finding 1 framed this as a *disagreement*
between the ORM and the init SQL; for these tables there is nothing to disagree
with. They are created only by `Base.metadata.create_all()`
(`database/connection.py`), so an `ALTER TABLE` in a numbered init file would run
in the dbInit Job before the table exists and hard-fail.

| Target table | Init SQL | Affected issues |
|---|---|---|
| `users`, `roles` | `06_auth_tables.sql` | #547, #552 |
| `skills`, `custom_agents`, `workflow_runs`, `custom_workflows` | `07_*.sql`, `08_*.sql`, `12_*.sql` | #548 |
| `cases`, `findings`, `case_evidence`, `case_tasks`, `case_watchers`, `investigations` | **none — ORM-only** | #549, #550, #551, #553, #554 |

For the ORM-only tables the actual mechanism is `scripts/migrate_schema.py`, a
decorator-registered list of idempotent `ALTER TABLE ... ADD COLUMN IF NOT
EXISTS` steps. **It is invoked by no deploy path** — not `start.sh`, not Helm,
not Docker. `create_all` is `checkfirst=True` and never alters an existing table,
so adding a column to the ORM silently drifts an existing database until a human
runs the script; `database/connection.py` detects the drift and
`backend/api/storage_status.py` surfaces the instruction. Resolve that before
promoting anything on an ORM-only table, and prefer #547, #548 or #552 as the
first migration since those exercise the documented Helm path.

---

## Proposed follow-up issues

One issue per PR, grouped so each is independently reviewable and revertible.

**On the #411 dependency.** These need a migration mechanism, but "blocked on
#411" overstates it, and the difference is per-deploy-path:

- **Helm:** already has forward migration. `helm/vigil/templates/db-init-job.yaml`
  maintains `_vigil_schema_versions(filename PRIMARY KEY, applied_at)` and skips
  files already recorded, so a new numbered file in `database/init/` *does* apply
  on `helm upgrade`.
- **docker-compose:** has none. `database/init/` is mounted at
  `/docker-entrypoint-initdb.d`, which Postgres runs **only when initialising an
  empty data directory** — so an existing local database never sees a new file.
  A developer has to recreate the volume or apply the SQL by hand.

So a promotion **on a table that has init SQL** is *deliverable* today via a
numbered file, at the cost of a manual step for existing compose databases. #411
is what makes the mechanism uniform and reversible, not what makes these
possible. Each follow-up should state which path it has verified.

**This does not cover every promotion below.** Six of the target tables have no
init SQL at all and are created only by `create_all`, so no numbered file can
alter them — see cross-cutting finding 5 for which issues that hits and what the
mechanism actually is. That distinction was missed when these issues were filed;
several of them prescribe an init file for a table that does not have one.

| Issue | Scope | Columns | Why separate |
|---|---|---|---|
| [#547](https://github.com/Vigil-SOC/vigil/issues/547) | Security scalar arrays → `ARRAY(TEXT)` | `users.password_history`, `users.mfa_recovery_codes` | Touches credential material; wants its own review and tests |
| [#548](https://github.com/Vigil-SOC/vigil/issues/548) | Tool-name arrays → `ARRAY(TEXT)` | `skills.required_tools`, `custom_agents.recommended_tools`, `workflow_runs.skill_tools_available`, `custom_workflows.trigger_examples` | Mechanical and low-risk. Confirm `trigger_examples`' element type first |
| [#549](https://github.com/Vigil-SOC/vigil/issues/549) | `case_evidence.chain_of_custody` → table | 1 | Correctness, not tidiness: append-only legal record open to lost updates |
| [#550](https://github.com/Vigil-SOC/vigil/issues/550) | `findings.mitre_predictions` → child table | 1 | Highest query value; touches the hottest table, so isolate it |
| [#551](https://github.com/Vigil-SOC/vigil/issues/551) | Remaining case children → child tables | `cases.resolution_steps`, `case_tasks.checklist_items` | Best after #544 establishes the pattern |
| [#552](https://github.com/Vigil-SOC/vigil/issues/552) | `roles.permissions` → `role_permissions` | 1 | Security-critical; must preserve the `Dict[str, bool]` API shape |
| ~~[#553](https://github.com/Vigil-SOC/vigil/issues/553)~~ | ~~`case_watchers.notification_preferences` → columns~~ | 1 | **Reclassified — no promotion.** The key set is a runtime `notification_type`, not a fixed flag list; no row has data; the table has no init SQL. Shipped instead as a declared vocabulary with API validation. See *Keep as JSONB* |
| [#554](https://github.com/Vigil-SOC/vigil/issues/554) | Resolve `investigations.trigger_ids`' real shape | 1 | Investigation, not migration — its ORM type is wrong |

### Already filed: the `cases` event trio

`cases.timeline`, `cases.activities` and `cases.notes` turned out to be the same
concept stored three ways — `backend/api/timeline.py:95-119` already flattens all
three into one stream and gives `timeline` and `activities` the **same**
`type="activity"`. `activities` is a strict superset of `timeline`
(`{timestamp, activity_type, description, details}` vs `{timestamp, event}`), so
unification means adopting that shape, not reconciling peers.

Filed as three sequenced issues, one PR each, in this order:

| Order | Issue | Scope | Blocked by |
|---|---|---|---|
| 1 | [#543](https://github.com/Vigil-SOC/vigil/issues/543) | Fix silent loss of in-place JSONB appends — a **live bug**, not a refactor | nothing |
| 2 | [#544](https://github.com/Vigil-SOC/vigil/issues/544) | Unify the three into `case_events`, API output unchanged | #543 |
| 3 | [#545](https://github.com/Vigil-SOC/vigil/issues/545) | Collapse the API to one stream, drop the legacy columns | #544 |

#543 must land first: it fixes `case.timeline.append(...)` silently failing at
`services/case_workflow_service.py:403` (auto-assignment) and `:470`
(escalation) — no JSONB column is wrapped in `MutableList` and `flag_modified`
appears nowhere in the codebase, so SQLAlchemy emits no `UPDATE`. Migrating
before that fix would backfill from columns already missing events. It is worth
landing on its own merits even if #544 and #545 are never approved.

---

*Analysis by Claude (Claude Code), reviewed by @craig-dt before publishing.
Verified against `main` at `311790e`.*
