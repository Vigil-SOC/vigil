# Vigil — `core/` domain structure

How code is grouped under `core/`. The reorg (epic #481) moves loose
`services/*.py` modules into named domain packages so each capability owns its
files and cross-cutting infrastructure has a deliberate home. `core/` has two
tiers: **capability domains** (what the SOC does) and a **shared-infrastructure
tier** (`storage`, `platform`) that capability domains depend on. One section
reaches outside `core/`: the **agent layer**'s vocabulary is here because its
terms collide with the domains' rather than sitting apart from them. A third
grouping, the **versioned API surface** (`core/api/v1/`), sits above the
capability domains: it composes them into the frozen HTTP contract and imports
domains freely. Nothing under `core/` imports it back (`.importlinter`): where
a console router and a v1 router answer the same read, the read lives in the
domain and both call down into it.

## Language

### Capability domains

**Finding**:
The atomic unit of security signal — one detection/alert instance ingested into
Vigil. Findings are finding-level (evidence, entity graphs, MITRE predictions
attach to a Finding), distinct from the Case that groups them.
_Avoid_: alert, event (when you mean a Finding specifically)

**Case**:
An investigation container that groups Findings with evidence, IOCs, SLA, and a
lifecycle. `cases` owns case lifecycle + anything that writes into a Case
(e.g. sandbox reports correlated into case evidence/IOCs).
_Avoid_: incident, ticket

**Source Evidence**:
Normalized, bounded evidence attached to a Finding (contract in
[source evidence](https://vigilsoc.org/docs/source-evidence/)). A finding-level concept, not case-scoped.

**Detection** (`detections`):
Detection-*rule* sources and their management — not finding analysis. "The rules
that produce Findings," distinct from the Findings themselves.

**Response** (`response`):
Autonomous containment actions and the approval workflow that gates them.

**Threat Intel** (`threat_intel`):
External threat knowledge — STIX/TAXII feed ingestion and MITRE ATT&CK taxonomy
resolution. MITRE lookup lives here as a reusable taxonomy resolver.

**Ingestion** (`ingestion`):
Normalizing *external* security data into Findings (SIEM, Kafka, S3-dropped
findings) — *what* a source yields and how it becomes a Finding. Distinct from
`storage`: ingestion *uses* storage clients; storage never depends on ingestion.

**Federation** (`federation`):
The scheduled poll loop that *drives* ingestion sources — the `federation_sources`
registry, per-adapter loops, cursors, and the global on/off toggle. Ingestion is
*what* a source yields; Federation is *when and how often* Vigil asks for it.
A vendor slice carries both: `ingestion.py` subclasses `SIEMIngestionService`,
and `adapter.py` wraps that service to satisfy the Federation contract.
_Avoid_: polling, sync, multi-tenancy (it is not Vigil-to-Vigil federation)

**Integration Health**:
One stored record per source (`source_health`, `core/storage/source_health.py`)
and the one state judged from it (`core/integrations/health.py` `judge`, served
at `GET /api/integration-health`). A source is an integration, keyed by its
integration id, or one of Vigil's own inputs under an id that can't collide with
one: `kafka`, `webhook:daemon`, `webhook:darktrace`, `webhook:cloudflare`,
`detection_source:<id>`. The record holds the last attempt with its outcome and
error kind, consecutive failures, dropped records (cumulative and the current
hour), how many findings its severity floor has skipped (``filtered_total``,
cumulative, kept across a reset the way dropped records are), last data
received, the last real check, a reset mark, and the labels
senders put on webhook events (information only; the judge does not read
them). A floor skip is the operator's filter, not a drop and not an unreadable
record: the judge does not read ``filtered_total``, and it never makes a source
unhealthy. Scheduled collection, Test, detection-source syncs and the push sources
(Kafka, the daemon webhook, the Darktrace and Cloudflare receivers) write it.
A push source writes a change of state at once and its counts at most once a
minute. Switching an integration on or changing its endpoint or credential
resets it to "no attempt yet". The states are healthy, no attempt yet, unhealthy (3 failures in a row, or dropped
records in the last hour), failing once, requires a test, not checked (with a
reason), switched off, and unknown since a time, for a record its writer has
stopped refreshing. Federation's health fields and the integrations API's
`last_test` are read from it. Medic does not use the judge.
_Avoid_: status (a detection source's file field), last test (only one writer)

**Install Readiness**:
Whether a new install is actually working, computed when it is read
(`core/integrations/readiness.py`, served at `GET /api/install-readiness`
under `platform.read`). It is ready when the background service is running,
the provider assigned to reporting (or to chat, when reporting has no row) is
routable, at least one alert source is switched on, every switched-on source
is healthy, and every alert source is collected by scheduled collection or an
enabled receiver. An input that cannot be read makes the whole state unknown.
Unknown is not ready. It blocks nothing: Home shows each unmet item and its
fix, and an hourly sweep on the API records one audit entry when readiness is
reached or lost. That sweep also re-checks one switched-on integration a day.
On the desktop app, which has no background service, it reads "not available
on the desktop app: no background service".
_Avoid_: `/readyz`, `/api/health/ready` (those say whether this process is up)

**Production Start**:
The moment a person declares that Vigil works this install's live alerts as
part of the real SOC process. It is never inferred. One system setting,
`production.start`, holds `started_at` (the moment they name, now or earlier)
and `declared_at` (when they wrote it). Clearing writes both empty. Home shows
"In production since" that time, or "Not in production", and a person holding
`platform.write` declares, changes, or clears it there. It is not a detection
rule's live stage: "go live" and "live" belong to rule rollout.
_Avoid_: live, go live

**Production Start Report**:
A stored report of the install's first stretch in production, run from the
same Install Readiness sweep as the Day 1 Report. It runs once, trigger
`production`, at the first tick at or after Production Start plus 48 hours.
A start already that old is due on the next tick. A cleared start starts
nothing, and a change after that one run does not start another. Someone
holding `platform.write` can run it on demand (`production_on_demand`) once a
start is declared; before that, the action reads "Not in production yet". An
on-demand run does not take the place of the automatic one. The record stores
the start it used and the window in `evidence["production"]`. The page is
`/report`, the same screen as the Day 1 Report, rendered from the trigger.
Home, beside the Production Start line, offers Run when the latest report
used a different start, and Re-run when the automatic start did not finish.
An automatic run sends one link-only notice to `platform.write` holders; an
on-demand run notifies the person who started it.
_Avoid_: Day 1 Report (that report is the install before this window)

**Day 1 Report**:
A stored report of what a new install can see, run from the Install Readiness
sweep (`services/api/readiness.py` calls `core/reporting/day1.py`). It runs
once when readiness is first reached, or 24 hours after the later of setup's
recorded finish (or the earliest person account, when setup was not finished)
and the upgrade that added the report. One run when both fall due together,
marked readiness. A report that ran earlier runs again on first readiness.
Someone holding `platform.write` can run it on demand. The page is
`/report`, read at `GET /api/day1-report` under `platform.read`. Home shows a
run this process is still writing, a start that didn't complete (with Re-run),
and a link to the latest report. An automatic run sends one link-only notice
to `platform.write` holders; an on-demand run notifies the person who started
it.
_Avoid_: Install Readiness (that decides when this runs; it is not a module)

**Jurisdiction**:
The operator's statement of whose rules they are subject to, stored with
the Data or Organisation Kinds on one `system_config` row
(`day1_report.selection`). This release offers the United States, New York,
the European Union and Australia. New York's parent is the United States:
selecting New York stores the United States, and clearing a jurisdiction
clears its sub-jurisdictions and their kinds. Read at
`GET /api/day1-report/selection` under `platform.read`; a `platform.write`
holder changes it with `PUT`, which the settings service already audits.
A missing row is no jurisdiction. A row that does not parse is a failed
read, and is never replaced with the empty selection.
_Avoid_: sector, region (where a provider processes)

**Data or Organisation Kind**:
The operator's statement of what this Vigil install handles, or what the
organisation is, worded as its source names it. Eleven kinds ship in this
release, each listed under the jurisdiction its source names; PCI DSS names
none. The cardholder-data-environment kind brings the PCI DSS assessment
kind. The two New York kinds exclude each other. Stored on the same row as
Jurisdiction. A missing row is no kind. A stored id this release does not
offer is kept and reported as no longer offered; a save of offered ids drops
it. A stored kind whose jurisdiction is not selected is kept and flagged,
and changes nothing.
_Avoid_: sector, regulated activity

**Report Record**:
One completed Day 1 Report run (`report_records`), inserted once and never
rewritten. `vigil_app` may insert and read it, not update, delete or truncate
it. It holds the modules that ran, each check's outcome with its evidence and
reason, what started the run (readiness, the fallback time, a person's Day 1
run, the automatic Production Start Report, or an on-demand one), the time
and Vigil's version. The run's start is its own row
(`report_runs`), so a start with no record can be shown. People are stored as
account ids and shown by name when the record is read.
_Avoid_: result store (outcomes live only here)

**Report Check**:
One entry in the check registry (`core/reporting/checks.py`): Vigil's own id,
its module, the permission that gates its source, and its rule. A settings
check also carries its scoped values. Each value names jurisdictions and
kinds, or every install, and carries a ceiling, floor or level, whether it
is in force or proposed, the source id, the clause, the link, the date it
was checked, and Vigil's own wording. There is no field for quoted text. The
registry holds the checks for what Vigil can see, for Vigil's own settings,
for detection coverage, and for what CrowdStrike, Defender and Wazuh still
list. Outcomes are not stored on the entry.
_Avoid_: rule check, credential check (those are other checks)

**Applicable Value**:
The one in-force value a settings check is scored against on a report run.
It is the tightest value whose scope the selection holds in full. A scope is
held in full when every jurisdiction and kind it names is selected. When no
scoped value is held, the every-install value is used. A scoped value is
used even when it equals the every-install value. Two equal scoped values:
the scope that names more of the selection, else the registry order. A
proposed value inside the selection is shown and is not scored. When none
is in force, the check stores no cited value. That is not a Check Outcome:
the row says "no cited value for your selection", shows no pass or fail, and
stays out of the summary's actions. The selection and the Applicable Value
are written onto the Report Record when the run is stored. A record that
stored no citation keeps the citation text from before values were scoped.
_Avoid_: check outcome, compliance

**Provider Statement**:
What the operator states about a model provider type, recorded on the
provider's settings under AI models with `platform.write`: self-hosted (the
model runs on hardware the operator controls; stored as `config.self_hosted`,
which pricing reads too), a signed BAA, the HIPAA-eligible account mode, the
processing and support region, a FedRAMP authorization and an EU transfer
basis. Each takes a value from a list, "not stated" included. It is held per
provider type: it counts only when every active row of the type carries it.
Each keeps the account id that made it, the date, and a hash of the addresses
the provider's calls reach (`core.llm.residency`); a different hash clears it,
as a change to Vigil's copy of the Vertex region clears a region statement. A
credential set or cleared since makes the check "needs a person" until
someone re-confirms it. Read by one check, where model processing runs; never
exported, never a vendor's claim. An address is never a self-hosted statement.
_Avoid_: attestation, certification, verified

**Check Outcome**:
What one Report Check returned on one Report Record: pass, fail, not checked
(with the reason), or needs a person. Not checked never reads as a pass.
_Avoid_: result, finding

**Report Module**:
One section of the Day 1 Report. What Vigil can see (`integrations.read`)
lists each source's health and reason, what it brings in (alerts, lookups or
rules), when data last arrived, and the labels seen on webhook events, all as
information, plus switched-off integrations and the state of collection, the
background service and the model. Vigil's own settings compares each live
session and retention value with its Applicable Value, and shows that
comparison, the citation, a link to the setting and, for MFA, a count of
accounts.
Detection coverage (`detections.read`) lists techniques that are not proven,
blind spots, techniques covered on paper only, rules that fail to run or whose
data is silent, and Quiet, Noisy and Silent results that are not a pass. A
category with none of those passes. A result the sweep did not store reads not
checked: search budget; a rule that is too new reads not checked: too new. The
stored record stays complete; a reader who lacks the module's permission is
not shown that section. On a demo-data install the report's first line says
it describes the install, not the demo data. Demo findings are not read as
data received.
_Avoid_: Visibility Gap, Declared Gap (those are run-scoped)

**Report Section**:
A Report Module whose output is figures and lists, with no checks and no
outcome. It is absent from the one-page summary and from comparison. Your
"before" (`history.before`), the re-check list (`history.recheck`) and tuning
candidates (`history.tuning`) are Report Sections, each read under
`findings.read`. Their figures, counts and ticket references are stored on the
Report Record's evidence, under the module id. Titles, entity values and
unmapped disposition text are read from the import when the report is viewed,
and only for a reader who holds `automation.read`. A section that cannot be
read says not checked and the reason, and the rest of the report completes.
The stored record stays complete; a reader who lacks `findings.read` is not
shown the section.
_Avoid_: Report Check (a section has no checks)

**Workflow** (`workflows`):
The user-facing product noun — a named, multi-agent procedure an analyst runs
from the Workflows screen. Authored as a **Playbook**, executed by **Compose**.
_Avoid_: playbook (that is the artifact, not the product noun)

**Playbook**:
The authored artifact a Workflow compiles to, and the single source of truth for
it — its ordered **Phases**, its per-role directives, its narrative, and the
catalog facts the Workflows screen shows. No schemas, no model or budget
settings: those are the arch and config layers of the three-file split
(arch · playbook · config). Both a `WORKFLOW.md` and a custom workflow's
structured phases are Playbooks. Every other reader derives from it.
_Avoid_: workflow definition, template

**Phase**:
One step of a Playbook — an agent, its instructions, and whether it stops for
approval first. Phases are ordered and the order is authored, not decided: an
agent may appear in more than one Phase, and Compose never reorders or skips
them.
_Avoid_: step, stage

**Compose**:
The run kind that executes a Playbook's phases in order. One of the harness run
kinds beside hunt, investigate and chat.

**Reporting**, **Chat**:
PDF/report generation; the agentic chat loop + durable conversations.

**Auth** (`auth`):
User identity — who the human is and what they may do: authentication, password
policy, cookies, and session/token revocation.
_Avoid_: connector trust (that's **Connector Trust**), permissions

**Connector Trust** (`integrations/extension`):
Which page-extension connector origins Vigil admits into its own page (CSP
`script-src`/`connect-src`, the SSRF guard) and the short-lived tokens minted to
them. A supply-chain trust decision about a third party, not a user login.
_Avoid_: auth, extension auth

**Integration** (`integrations`):
A third-party security product Vigil is configured to talk to. Splits by whether
Vigil carries code for it: a **Catalog Entry** has only a credential form; a
**Vendor Slice** has code. Both are "integrations" to a user; only the second is
one to the codebase.
_Avoid_: connector (that's the page extension), tool, plugin

**Catalog Entry**:
A vendor listed in the Settings UI with a credential form and no Vigil code —
config is stored and read by nothing. Declared once in
`clients/web/src/config/integrations.ts`; the backend derives its secret-field
routing from that declaration rather than restating it.
_Avoid_: integration (unqualified), stub

**Vendor Slice** (`integrations/<vendor>/`):
A code-backed integration: a package owning some of `descriptor`, `ingestion`,
`adapter`, `client`, `tool`, and its router. Every vendor Vigil has code for owns
exactly one slice — there is **no** top-level `tools/` package.

**Integration Descriptor** (`<vendor>/descriptor.py`):
The single source of truth for a Vendor Slice's registry facts — its Integration
Id, its complete field list (not just the secret ones), and the MCP Server Names
it backs. Registries derive from it; they never restate it. Discovered by
scanning `core/integrations/*/descriptor.py`, so a descriptor cannot go dead by
being left out of an import list. A Catalog Entry has no descriptor.

**Integration Id**:
An Integration's persisted identity — primary key of `integration_configs`, key
in `integrations_config.json`, and the stem of each secret's storage name
(`<UPPER_ID>_<FIELD>`). Kebab-case. Renaming one orphans a DB row, a config
entry, and a stored credential at once, so it is a migration, not an edit.
_Avoid_: server name, integration name

**MCP Server Name**:
The key in `mcp-config.json` naming a server *process*, and the prefix on the
tool names that process exposes. Deliberately distinct from Integration Id: one
identifies stored config, the other a running process. One Integration may back
several — Splunk has both an official server and the self-hosted one Vigil ships
— so a descriptor declares a tuple, and CI asserts every name resolves to a real
`mcp-config.json` key.
_Avoid_: integration id, tool name

**Declared Gap**:
A question an investigation never gathered evidence for, carrying why nothing was
gathered. Not a **Verdict** with an empty outcome: it has no activity window,
because there was no activity to bound. Gaps are the common case rather than the
exception, and what ranks an entity up is accumulation of them across
investigations, never being one.
_Avoid_: visibility gap (that is a tool that could not answer), unknown, null result

**Declared Site**:
What the operator states about the site as files under change control:
`site/*.md` in the declared directory (`VIGIL_INTENT_DIR`), beside `INTENT.md`.
Four lists, each item by **Selector**: zones, crown jewels with the mission they
serve, owners naming a Vigil team, and **Site Blind Spots**. The daemon applies
each revision on its own verdict (`site.applied`), refuses a broken one whole
with file and line and keeps the last good one. It ranks and informs; it never
closes, permits or acts. A crown jewel is held for a person (`crown_jewel_held`
in `core/response/ceiling.py`, `hold_crown_jewels` on the ceiling, on by
default): no model closes a case whose findings name it, and a run no person
started may not add an exception, dismiss a finding or make a reversible or
irreversible outside call naming it. Limits: a vendor device id is not read; a
crown jewel declared only by name does not match a range, so declare its
addresses too; a case whose findings name no crown jewel is not held; an
exception keyed on no host, address or account is not read. One module holds
the site: `core/site.py`.
_Avoid_: inventory, asset list, CMDB (Vigil keeps no list of hosts)

**Selector**:
A plain string naming entities in the **Declared Site**: `cidr:<range>`, which
matches `ip:` keys, or an **Entity Key** of a kind in `ENTITY_KEY_TYPES`,
normalised as `normalise_key` mints it, where `*` in the value matches any run
of characters. No other wildcard, no regex, and no group, tag or cloud
selectors: a revision's validity never depends on a source being connected.
Matched only in Python, by `core.site.match` and `core.site.crown_jewel`.
_Avoid_: filter, query, pattern

**Site Blind Spot**:
A place the operator states Vigil cannot see, declared in `site/*.md` with its
selectors and what is not seen there. It reaches a hunt as an operator gap.
Not a coverage blind spot (a data source no rule reads), not a **Visibility
Gap** (a tool a run could not use) and not a **Declared Gap** (a question an
investigation left open): those are found by Vigil, this is stated by a person.
_Avoid_: blind spot (alone), visibility gap, declared gap

**Episodic Memory** (`memory`):
What earlier investigations saw and concluded, so a later run stops re-deriving
a settled answer. A hunt's is derived from its **Ledger** at the terminal; a
**Case**'s is derived from the Case itself when it closes, and has no Ledger
behind it at all. Reorders the frontier and never decides (ADR 0015).
_Avoid_: MemPalace (the component this replaced, since removed), cache, RAG

**Distil** (`memory`):
The job that turns a finished investigation into Episodic Memory rows. One per
kind of investigation -- a hunt's reads the Ledger, a Case's reads the closed
Case -- and both poll rather than being told, so a lost signal is a late write
and not a missing one.
_Avoid_: ETL, sync, ingest (those move data; this concludes about it)

**Closure Category** (`cases`):
What closing a **Case** determined: `resolved`, `false_positive`, `duplicate`,
`unable_to_resolve`, or `unspecified` for a close that stated none. All but
`duplicate` map to a **Verdict** outcome; `duplicate` writes none. `unspecified`
is a recorded absence, not a determination.
_Avoid_: resolution, disposition, reason

**False-Positive Cause** (`cases`):
Which kind of fix a false-positive determination is: `expected` (the activity
was expected or authorized), `too_broad` (the rule's logic needs work), `data`
(a field, parser or source is wrong), or `other`. It refines a **Closure
Category** of `false_positive` and a noise mark. Absence is a null column, not
a member of the set.
_Avoid_: not given, reason, disposition

**Entity Key**:
The `type:value` string an **Episodic Memory** row is written and queried on --
`ip:10.0.0.5`, `host:dc01`. Minted by one rule, defang then case-fold, so a stored
key and a queried key cannot be normalised two ways. `arn` and `aws_key` keep
case whole. `process` keeps it for an absolute POSIX path, and `url` keeps it
in its path, query and fragment. `user` and `email` are case-folded by design,
on every path a key enters by, including keys a person types.

Whether two spellings are one account depends on the system that owns it, and
a key sees only a type and a value. Most account systems Vigil sees match names
without case: Windows and Active Directory, Entra ID, AWS IAM, Okta, Google
Workspace and most SaaS, and LDAP. Linux and Unix local accounts are
case-sensitive; their names are lower case by convention, so two that differ
only in case are rare. A bare name such as `deploy` looks the same in a Windows
logon event and in an sshd log, and the key cannot tell them apart. Keeping
case would silently split the history of most accounts. Case-folding merges
that rare pair. The key is a matching index. The finding keeps its spelling, so
an investigation still rests on the original. Recall, entity facts and the hunt
graph inform an investigation and never decide it. The insider-lead path is
where the case-fold steers an action, and there it narrows: a lead joins a hunt
already running instead of starting a second one, and the attach is recorded on
that case's activity. The parity check compares the two halves and holds no
expected keys, so the same change made in both passes it. No operator has
reported a wrong recall from merged case-variant accounts.

Two accounts on a case-sensitive system (Linux or Unix local accounts) that
differ only in case share one history. A Sighting shows its source system, not
the spelling. A Verdict, Gap or note shows the spelling only where its prose
does. A lead on one such account joins a hunt already running on the other
instead of starting its own, and the attach is recorded on that case's
activity. A hunt opened from a lead names the account in lower case. The
finding on its case keeps the original spelling. An email's local part can be
case-sensitive by its standard (RFC 5321), but providers and email-style
identity systems match it without case, so `email` is case-folded the same way
as `user`. The case-fold is lower-casing. The parity check compares ASCII
inputs; non-ASCII names follow each runtime's own case tables.

A `shared_iocs` key is the same shape on a different vocabulary and is not one
of these.
_Avoid_: IOC key, indicator, entity id

**Recall**:
One read of **Episodic Memory** on exact entity keys. A run performs one at start
and renders it into the frozen prefix; a worker performs one mid-run through the
`recall_entity` tool, which lands in the prompt tail and leaves the prefix
undisturbed. Recall never contributes to corroboration — it reorders what to look
at and settles nothing (ADR 0015).

A run's keys come from what the run was opened on. A hunt's are the subjects an
operator declared for the hypotheses actually being put up, and where none were
declared, the entities the hypotheses name. An investigation opened on findings
has neither, so its keys are the entities its trigger findings carry.
Declared keys first because a person typed them and the spec refused the
unusable ones; extraction at all because a scheduled hunt declares none, and the
autonomous path would otherwise never read memory. A key read out of a statement
can be beside the point rather than wrong, which spends prefix budget and, since
recall only reorders, can never move a **Verdict**.
_Avoid_: search, retrieval, lookup (unqualified), context

**Recall Event**:
The **Ledger** record of one run-start **Recall**. A read that was served carries
the keys queried, the rows returned with their provenance, what was dropped and
why, and the selection parameters in force; a read that could not be served is an
**Unavailable Read**, which carries the keys and the reason and none of the rest.
It carries rows and not an order — the order they were
presented in is a fold, so ranking may change without invalidating a historical
Ledger. The parameters are copied in rather than referenced, because a **RunSpec**
records the arch by name and not by version.
_Avoid_: recall log (that is the audit table, which is Python's), snapshot

**Unavailable Read**:
The **Recall Event** a run journals when the read could not be served at all --
the far side down, the token wrong, the answer unreadable. It carries the keys
asked about and why, and it is deliberately not an empty **Recall**: empty lists
mean *known-to-be-none*, so recording an outage that way would say those entities
have no history, which is true of every entity while memory is down -- so nothing
would look wrong. A run that opens on one carries no recalled rows for the rest of
its life rather than retrying on a later turn, because a read that succeeded on
turn four would move a prefix the run had already decided against. The run itself
still finishes: **Recall** reorders and never decides, so losing it costs a run an
aid and not an input.
_Avoid_: failed recall, empty recall, **Declared Gap** (that is a question an
investigation left open, not a read that did not happen), **Visibility Gap** (that
is a tool that could not answer about the estate)

**Facts Event**:
The **Ledger** record of the entity facts an investigation opened on (#4742): what
Entra, Okta and Falcon said about the users and hosts its trigger findings name,
read by the harness at run start beside **Recall** and on the same terms -- not a
granted tool, once per run, on the trigger findings' **Entity Keys**. A read that
was served carries each row with its source and `fetched_at`, and each source that
could not answer with its reason; a read that could not happen at all -- the
endpoint failed or refused, or the run may not read its Case -- carries the keys
and the reason and none of the rest, and the run carries it for its life as it
would an **Unavailable Read**. A run on a Case reads that Case's own rows, copying
a fresh caseless row onto the Case before asking a source. It also carries
"Declared" rows from the applied site revision for every key the run names, `ip:`
keys included, each with its kind, name, owner, file, line and revision; they come
from no source and are never stored. The prefix block and a replay render from
this event, never from the table. Facts inform and settle nothing; a hunt reads
declared rows only.
_Avoid_: enrichment, entity context, Recall (facts are what a source says now, not
what past work concluded)

**Sighting**:
What one investigation observed about one entity from one source. One row per
entity, investigation and source, so growth tracks hunts and not telemetry
volume. The weaker and truer claim a **Verdict** does not make: *seen during a run
that concluded X*, rather than a subject of that conclusion.
_Avoid_: finding, evidence, observation, hit

**Source Tier**:
What a source *is*: `telemetry` observed our own estate, `feed` asserts about the
world, `not_evidence` is neither. Distinct from **Trust**, which is who
concluded — `analyst` or `agent`. Both sit on a **Verdict**'s sources, and one
does not imply the other: a feed can be cited by an analyst.
_Avoid_: connector trust, confidence, severity

**Stance**:
How one source bore on a **Verdict**: `supports`, `weakens` or `neither`. Replaces
a flat corroborated list, which cannot express direction — a source that weakened
a hypothesis and a source that never bore on it are not the same row.
_Avoid_: corroboration (that is the effect of several sources agreeing, not one
source's direction), confidence, polarity, sentiment

**Trust**:
Who concluded. `analyst` only when a person closed the case through the console
sign-in, the access cookie. A bearer access token, a program holding a
credential, a verified tool principal, and a close with no caller are `agent`,
and so is the development bypass, which is not a cookie. The person is still
the actor: when the stored name is a live username, `closed_by` becomes that
principal and the name stays beside it. A program cannot claim `analyst` by
sending a field. The other axis on a **Verdict**'s sources, alongside **Source
Tier**. Unrelated to **Connector Trust**, which is about admitting a
third-party origin.
_Avoid_: connector trust, source tier, confidence

**Verdict**:
What one investigation concluded about one hypothesis, naming its subjects
rather than every entity its evidence touched (ADR 0016).
_Avoid_: finding, case closure, outcome

### Shared-infrastructure tier

**Storage** (`storage`):
How Vigil persists and reads *its own* data — the full metadata-DB layer (ORM
`models`, the engine/session in `connection`, `DatabaseService`, the DB-backed
`config_service`), the higher-level data-access layer, DB/connection proxies, and
the S3 object-store client. A capability domain may depend on `storage`;
`storage` depends on no capability domain. There is **no** top-level `database/`
Python package and **no** `core/platform/db/` — all DB code lives here.

**Platform** (`platform`):
Process/config/runtime plumbing — local service orchestration and process
supervision, autostart config, runtime-config resolution, demo-data seeding,
URL/SSRF safety. Not a junk drawer: a file belongs here only
if it's runtime plumbing with no owning capability. The cut against a capability
domain is **mechanism vs. knowledge**: supervising a process, or resolving a
setting, is `platform`; knowing what the setting *means* is the domain's.

**State Directory**:
The one per-install directory outside the repo holding everything a Vigil
install accumulates that the metadata DB does not — credentials, integration
config, MCP enable flags, detection sources, theme, exports. Defaults to
`~/.vigil`; `VIGIL_DIR` names it explicitly and is the **only** override.
Resolved by `vigil_path()` — a mechanism, so it is `platform` by the cut above.
_Avoid_: config dir, state dir, `.vigil`, "where secrets live", VIGIL_HOME.

**Mirror** vs **Original** (State Directory contents):
A **Mirror** is a State Directory file the DB is authoritative for — the router
reads "database first" and writes the file only "for backward compatibility"
(`s3_config.json`, `integrations_config.json`). Losing one costs nothing. An
**Original** has no DB home, so losing the file loses the data: the secrets
store, `mcp_server_enabled.json`, `detection_sources.json`,
`custom_integrations/`, `general_config.json` (its `demo_mode` key has no DB
row). Only Originals constrain where the State Directory can live.

### Versioned API surface

**`core/api/v1/`**:
The frozen HTTP contract — the subset of routes an external caller (the platform,
contributors, other tools) may rely on, versioned under `/api/v1`. A route lives
here because it is a durable record or an act on one; console-only wiring stays
on the unversioned routers under `services/api/routers/` and `core/<domain>/`.
Membership is by file: a route in this package is a promise, guarded by the
`contract.snapshot.json` pin. Each router dual-mounts (its `/api/v1/<res>` path
plus the pre-version `/api/<res>` via `RouterMeta.legacy_prefixes`) so existing
callers keep working during the port. This package composes the capability
domains (and may import them); the shared-infrastructure tier must not import it
(`.importlinter` tiers contract). See `core/api/v1/README.md` for the recipe and
the internal/external test.
_Avoid_: "the API" (ambiguous with the unversioned console routes), calling a
console route "v1".

### Agent layer

Vocabulary of `services/agent/` — TypeScript, outside `core/`, but the terms
collide with the capability domains above often enough to belong beside them.

**Ledger**:
The append-only event log of one run, and its only durable record. Every other
view of a run is derived from it rather than stored beside it. The application
role `vigil_app` and the agent layer's own role `vigil_agent` may `SELECT` and
`INSERT`; `UPDATE`, `DELETE` and `TRUNCATE` are revoked at the database.
`vigil_agent` holds nothing beyond the agent tables.
_Avoid_: journal, audit log, history

**Fold**:
The pure function from a Ledger's events to a **Projection** — and by extension
everything derived from one: digest, evidence strength, verdicts, termination,
entities, report.
_Avoid_: reducer, replay (**Replay** is a distinct check — rebuilding a Digest a
decision was shown, to see whether it still matches)

**Projection**:
The state of a run computed by folding its Ledger — hypotheses, questions,
evidence, dispatches, checkpoints. Computed on read, never persisted.
_Avoid_: state, snapshot

**Hypothesis**:
A falsifiable claim a hunt is testing, carrying a status and the **Evidence**
linked for and against it.

**Evidence**:
One observation a worker reported during a run — a summary, a salience, and the
entities it mentions — linked to Hypotheses as supporting or weakening. A
run-scoped concept, distinct from **Source Evidence**, which attaches to a
Finding.
_Avoid_: finding, result, observation

**Visibility Gap**:
Something a run could not see because a tool timed out or was unavailable —
recorded so an absence of evidence is not read as evidence of absence. A refusal
or a bad argument is a defect and must never be recorded as one
(`services/agent/contracts/tool.ts`). Distinct from a **Declared Gap**, which is
a question nobody gathered evidence for rather than a lookup that failed.
_Avoid_: declared gap, error, failure, unknown

**Digest**:
The bounded view of a Projection presented to the lead for a single decision:
recent Evidence, entities seen, open questions. Its sampling is seeded from the
run, so the same Projection yields the same Digest.
_Avoid_: context, prompt

**Golden**:
A recorded output of the implementation being *replaced*, kept as the comparison
target for its port. Its worth is entirely in that provenance: a Golden produced
by the code under test asserts nothing.
_Avoid_: snapshot, baseline, fixture (a fixture is an input; a Golden is an
expected output)

**Fold Equivalence**:
The property that every Fold over a historical Ledger reproduces its Golden
byte-for-byte, projection and derivations alike. Its inputs are the pre-harness
Ledgers under `services/agent/tests/fixtures/runs/`; a run recorded by current code
is **Replay**'s fixture and not one of these.

**Replay**:
Rebuilding what a decision was shown from the Ledger alone, and checking it
against what was journaled at the time: the **Digest** from the events before the
decision, and the recalled rows from the **Recall Event**. Distinct from **Fold
Equivalence** — that check compares this implementation against the one it
replaced, over a fixture population closed to old-format Ledgers, while a Replay
reads a run this code recorded (`services/agent/tests/fixtures/replay/`). A Replay that
re-queried **Episodic Memory** rather than reading the journaled rows would read a
neighbourhood that has moved since the run, and pass while showing a decision
something it never saw.
_Avoid_: fold, re-run, regression test (a regression snapshot is the fixture; the
Replay is the check)

**Stop Cause**:
Why the model path stopped a run, from a closed set, named once where the call
failed (`ProviderError.kind`) and carried on the terminal beside the outcome. It
never replaces the outcome: a gateway budget refusal is `budget_exhausted` with the
cause `gateway_budget_refused`. A run a model call did not stop has none. The console
words each cause and its next step in one table (`clients/web/src/shared/stopCause.ts`).
_Avoid_: provider error, failure reason (the reason is the provider's own text)

### Console (web client)

Vocabulary of `clients/web/src/` — TypeScript, outside `core/`, and here for the
same reason the agent layer is: it re-declares the domains' nouns as its own view
shapes, so the collisions need naming rather than leaving to the reader.

**Console** (SOC Console):
The authenticated surface an analyst works in — nav rail, topbar, screen area and
the Vigil chat dock, all under one `.soc-console` root. Login and Setup are
full-page surfaces that render *outside* it.
_Avoid_: dashboard (that is one Screen), app, UI, redesign (retired — the console
was "the redesign" only while a second UI existed, and that ended with #502)

**Screen**:
One of the eight named views the Console routes, each owning a URL (`/<screen>`)
and implementing the `ConsoleScreenProps` contract the shell passes it. Login,
Setup and the in-shell 404 are views but not Screens: nothing routes them by key
and none implements the contract.
_Avoid_: page, tab, view

## Relationships

- A **Case** groups one or more **Findings**
- **Episodic Memory** derives **Verdicts** from a **Ledger** after a hunt
  terminates, and from a **Case** when it closes; it never writes during a run,
  and a run reads it once at start (ADR 0015)
- Closing a **Case** writes one **Verdict**, whose Trust is `analyst` only when
  a person closed it through the console cookie, and `agent` otherwise — a
  bearer token, a program holding a person's credential (which the actor
  names), a tool principal, or no caller; reopening the Case withdraws the
  Verdict
- A **Verdict**'s sources each carry a **Source Tier** and the Verdict carries
  one **Trust**. The two are independent axes: **Trust** is who concluded, and
  a `feed`-tier source can be cited by an `analyst`
- **Source Tier** is stamped at write time, never joined at read time, so
  recategorising an **Integration** later cannot change how a past **Verdict**
  was corroborated
- **Trust** is unrelated to **Connector Trust**, which is about admitting a
  third-party origin rather than weighing a conclusion
- A **Recall** returns **Sightings**, **Verdicts** and **Declared Gaps** in one
  shape, carried two ways: journaled verbatim as the **Recall Event** at run
  start, and returned as the single row of a `recall_entity` tool result mid-run.
  One result shape and not two — a second copy of it is a second contract, and the
  second one drifts. An **Unavailable Read** is not that second copy: it is the
  account of a read that did not happen, which only the harness ever writes.
  Declared in `core/memory/recall_contract.py` and
  `services/agent/contracts/memory.ts`, which must agree
- **Ingestion** produces **Findings** and depends on **Storage** (never the reverse)
- **Federation** drives **Ingestion** (an adapter wraps an ingestion service);
  Ingestion never depends on Federation
- Capability domains depend on the **Storage**/**Platform** tier; the tier
  depends on no capability domain. This is no longer prose: `.importlinter`
  enforces it, plus "core must not import the deployables", on every PR with
  no exemptions. The rule had stood since R5 and accumulated 20 live
  counterexamples by R9, which is the argument for a gate over a convention.
- **LLM** code (`core/llm/`, in flight as #485/#522) is a separate slice, not
  part of these domains
- The **LLM gateway** (`core/llm/gateway`) enqueues LLM jobs onto the `arq:llm`
  queue; the **worker** (`services/worker`) is the sole consumer that executes
  them — the enqueue/execute seam between `core/` and the `services/` deployables
- The agent layer has exactly **two ways in**, and they are what it splits along
  rather than by protocol: the **run queue** (`agent-runs`, enqueued by
  `core/agents/queue.py`) and the **agent HTTP surface** (chat and run
  projections). **Agent Worker** drains the first, **Agent Serve** answers the
  second; every HTTP route the layer exposes is request-driven, so projections
  ride with chat rather than earning a third deployable
- A **Workflow** is authored as exactly one **Playbook** and executed by
  **Compose**; a Playbook holds one or more ordered **Phases**, and a **Phase**
  names exactly one agent
- The **Playbook** is the registry for a Workflow: the Workflows catalog, the
  phase sequence and the per-role prompts all derive from it and none restates
  it — the same rule ADR-0001 sets for the **Integration Descriptor**
- An **Integration** is either a **Catalog Entry** or a **Vendor Slice**, never
  both; only a Vendor Slice has an **Integration Descriptor**
- An **Integration Descriptor** declares exactly one **Integration Id** and zero
  or more **MCP Server Names**; the secret registry, the bridge's server map, and
  `mcp-config.json` all derive from it rather than restating it
- A **Vendor Slice** lives only under `core/integrations/<vendor>/`. The
  top-level `tools/` package holds no vendor servers — `tools/mcp/` is a separate
  thing: servers that talk to Vigil's *own* services, which are not Integrations
- A **Ledger** folds to exactly one **Projection**, and the Projection is never
  stored — there is no second copy of a run's state to drift
- A **Digest** is derived from a **Projection** for one lead decision; **Evidence**
  links to **Hypotheses** as supporting or weakening
- A **Golden** is the output of the implementation being replaced, never of the one
  under test. This is the whole of **Fold Equivalence**'s value and the rule
  ADR 0012 exists to hold
- The **Console** routes one or more **Screens**; a Screen is named by exactly one
  `ConsoleScreenKey` and reached at exactly one URL
- A **Screen** renders a **Finding view-model**, not a **Finding** — the mapping
  between them lives in one place (`src/data/mappers.ts`) and is lossy in both
  directions

## Flagged ambiguities

- **"workflow" meant three things.** The five `WORKFLOW.md` definitions, the
  DB-authored custom workflows (the `custom_workflows` table), and the
  TypeScript control-flow modules under `services/agent/workflows/` were all
  "workflows", and `CONTEXT.md` itself glossed the domain as "multi-agent
  playbooks" — making "playbook" an informal synonym. #624 spent that word on a
  precise concept, so the synonym is withdrawn. Resolved: **Workflow** is the
  product noun, **Playbook** is the authored artifact, **Compose** is the run
  kind that executes one. A Workflow is authored as a Playbook and executed by
  Compose.

- **"finding" work kept falling into `cases`.** `source_evidence` and
  `graph_builder` are finding-level, not case-level. Resolved: they belong to a
  **`findings`** domain, deferred until PR #537 (`services/findings/enrichment/`,
  issue #470) lands, then consolidated into `core/findings/` in a follow-up.
  Until then both stay in `services/`.
- **`platform` was absorbing LLM config.** `defaults.py` and `runtime_config.py`
  read as "central config" but their content is model/thinking/AI-ops settings.
  Resolved: they're **LLM-slice** files (#485), not `platform`. `defaults.py`
  (`DEFAULT_MODEL`, `build_thinking_kwargs`) now lives at `core/llm/defaults.py`
  — moved with the worker slice (#508), which also killed the `core/llm/gateway`
  → `services.defaults` inversion. **Amended (R9):** `runtime_config.py` cannot
  "stay in `services/`" — `services/` now means deployables only (`api`, `daemon`,
  `worker`). Re-resolved by the mechanism-vs-knowledge cut: it is a DB > env >
  default resolver with a TTL cache — a *mechanism* — so it lands at
  `core/platform/runtime_config.py`, not `core/llm/`. Its keys are LLM-ops; the
  resolver is not. This also removes the last `core.chat`/`core.llm` →
  `services.*` inversions.
- **`platform` ↔ `llm` was a cycle, not a violation (R9).** `core/platform/
  service_manager.py` and `core/llm/providers/ollama.py` imported each other
  across the tier boundary — 7 edges, every one a function-local import deferred
  purely to dodge the cycle. Resolved: `ollama.py` is misfiled. Its docstring
  calls it a "Host-native Ollama supervisor"; it implements `service_manager`'s
  own `ServiceSpec`/`ServiceStatus`/`ActionResult` protocol, and `service_manager`
  is its *only* consumer — nothing in `core/llm/` imports it. It moves to
  `core/platform/ollama_supervisor.py`, deleting the cycle and letting the
  remaining imports return to module top-level. Supervising the Ollama process is
  platform's "local service orchestration"; the payload being LLM traffic doesn't
  make the supervision LLM knowledge.
- **`s3_service`: ingestion or storage?** Its purpose is sourcing findings, but
  `storage`'s own data-access layer depends on it. Resolved: **storage** (an
  object-store client), so the layering isn't inverted.
- **DB code: `platform/db/` or `storage`?** REARCHITECTURE §7 routed the
  remaining top-level `database/*.py` (models, connection, service,
  config_service) to `core/platform/db/`. Resolved (R6, epic #481): they join
  **`core/storage/`** — storage already owned the data-access layer + `db_proxy`,
  and a `platform/db/` split would only relocate the cross-domain reach
  (`core/storage/database_data_service` → top-level `database`) instead of killing
  it. No `core/platform/db/`; the top-level `database/` package is retired and its
  SQL moves to `infra/database/init/`.
- **"integration id" meant five different strings.** `mcp-config.json` keys,
  descriptor `id`, descriptor `mcp_server_name`, the frontend catalog `id`, and
  the snake_case key each `tools/*.py` passed to `get_integration_config()` all
  drifted apart. The damage was not cosmetic: six of the eleven `tools/` servers
  read a snake_case key the UI never writes, so their config resolved to `{}`;
  every `"<x>-server"` value in `INTEGRATION_TO_SERVER_MAP` matched no
  `mcp-config.json` key, so the bridge's dedupe never fired and it fell through to
  spawning `python -m tools.<name>` for modules #484 had already moved. Resolved:
  exactly two identifiers survive — **Integration Id** (persisted, kebab-case) and
  **MCP Server Name** (process). They stay separate because they identify
  different things, and CI asserts each descriptor's server name resolves to a
  real `mcp-config.json` key. No id is renamed: renaming is a data migration, and
  the drift is fixable without one.
- **`tools/` vs `core/integrations/`.** #484/#557 moved the ten vendors that had a
  slice to consolidate and left eleven single-file MCP servers behind, so location
  encoded "did this vendor have other files" rather than any real distinction.
  Resolved: **Vendor Slice** is the only home — every code-backed vendor owns
  `core/integrations/<vendor>/`, and the top-level `tools/` package is retired.
- **"evidence" and "finding" each meant two things.** `Finding` and **Source
  Evidence** are ingest-side and finding-level; the hunt's **Evidence** is a
  worker's observation inside a run and never attaches to a Finding. The worker
  prompts make it worse by calling their own output a "finding" ("nothing matched
  is a real finding"). Resolved: the two Evidence concepts are distinct and stay
  distinct — the run-scoped one is **Evidence**, the finding-scoped one is
  **Source Evidence**, and a worker's prose calling its output a finding is loose
  usage, not the term.
- **"golden" read as "saved output".** Treated that way it invites the one thing
  that voids it — generating goldens from the code under test, which produces a
  green suite pinning a port to itself. Resolved: a **Golden** is defined by
  provenance, not by being saved; regenerating one means re-running the *original*
  implementation. See ADR 0012.
- **the web client's `Finding` is not the domain's `Finding`.** The console
  declares its own `Finding` and `CaseRow` interfaces, and `src/data/mappers.ts`
  says plainly that "the view shapes carry richer fields than the API returns" —
  so a field visible on a Finding in the UI may be derived, defaulted to an
  em-dash, or a neutral placeholder rather than anything the backend sent.
  Resolved: they are distinct, and the UI one is a **Finding view-model**. Reading
  a screen as evidence of what a Finding *is* gets the domain wrong; `mappers.ts`
  is the only honest account of which fields survive the trip.
- **"a future run can be added" is narrower than it reads.** A new fixture is only
  a valid Fold Equivalence input if its Ledger is in the pre-harness file format,
  and nothing produces that format any more. The population of possible fixtures
  is therefore closed — the ten committed runs plus whatever old-format ledgers
  still exist. A hunt run by current code can be a regression snapshot, never a
  Golden.
- **`type:value` keys are minted by two rules that disagree.** An **Entity Key**
  (`core/memory/entity_keys.py`) keeps case whole for `arn` and `aws_key`, and
  in part for `process` and `url`; a
  `shared_iocs` key (`core/storage/shared_ioc_repository.py`) folds everything,
  and spells a host `hostname` where memory spells it `host`. Both defang, by the
  one rule in `core/storage/defang.py`. Handing one subsystem's
  keys to the other returns no rows, which reads as an entity nobody has looked
  at rather than as a bad query. Resolved: they stay separate, because unifying
  them is a migration of a live table. Besides `defang`, the shared piece is the
  `entity_context` spelling map, which yields candidates in memory's vocabulary --
  `make_key` aliases `host` back to `hostname` on the way in, so the older keys
  are unchanged.
