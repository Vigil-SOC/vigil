# Vigil Console, phase 1 — Agents & workflows

Rev 1.1-P1.1 · 2026-10-01 · follow-up to `PRD-phase1.md` (Rev 1.1-P1). Checked against `main` at #1377.

## How to read this

This document **supersedes section 12 of `PRD-phase1.md`**. Everything else there stands: placeholder classes (section 2), principles (section 3), vocabulary (section 1), and the definition of done (section 14), which applies here on top of section 4 below. Where this document and a board disagree on appearance, the board wins; on behaviour or scope, this document wins. How each requirement is built is left to grooming.

| Surface | Kind | Board |
|---|---|---|
| Workflows | tab | `Agents` |
| Agents | tab | `AgentEditor` |
| Skills | tab | `SkillEditor` |
| Commands | tab | none; the command bar's built-in set |
| Workflow reader | pane in the Workflows tab | `Agents` |
| Agent drawer | drawer over the Agents tab | `AgentEditor` |
| Skill drawer | drawer over the Skills tab | `SkillEditor` |
| Watch a run | page, opened from Watch it run | `WorkflowRun` |

Tool permissions is not built: no tab, no page.

## 1. Why section 12 is superseded

Section 12 kept today's graph builder and deferred the board's loop to phase 2. Read against the engine, that is backwards:

- **The builder draws the wrong thing.** It shows a workflow as a chain of steps run in order. The engine runs a lead agent that decides each turn which helpers to send, loops until an explanation is settled or a limit is hit, and has a reviewer check the result. No workflow has a step order.
- **The board's loop is a reader, not an editor.** "Click a stage to see who does it and what it may do." It explains how a run works; it does not ask to edit the loop.
- **Investigations and hunts are described differently.** A workflow's definition names what each helper may use, but hunt-like workflows name capabilities ("search telemetry") while investigations name specific tools, in the same field. When an investigation's tool is missing it is skipped silently; when a hunt's capability is missing it is recorded as a blind spot. The page cannot show every workflow the same way until the definitions agree.

## 2. Scope

**Tabs:** Workflows, Agents, Skills, Commands, each with its count.

**Later:** editing a workflow's stages; per-agent skill assignment; editing a skill's scripts; skill import; viewing or restoring an earlier version; "Describe a change and Vigil drafts it"; custom commands.

**Omitted:** Tool permissions; per-tool autonomy settings; authorship.

## 3. Requirements

### One definition for every kind of run

- **AW-D1. Every workflow is described the same way, whatever kind of run it is** — investigation, hunt, root cause or adjudication. What a helper may use is always named as a capability, never as a specific tool, so a workflow does not depend on which SIEM or intel source a deployment has connected.
- **AW-D2. A missing source is a blind spot on every kind of run.** When a capability cannot be bound, the run records that it could not look and why, and the operator sees it. Nothing is skipped silently. This is the distinction Vigil already draws for hunts — "we looked and it was not there" against "we could not look" — applied to investigations.
- **AW-D3. The existing definitions are made consistent** before any workflow gains helpers it does not have today.

### Workflows tab

- **AW-W1. Workflow list.** Each workflow with its kind, what starts it, its command, runs today and cost per run. Trust is **Not measured yet**. *Runs today, cost per run and Trust shipped (#1366); kind, trigger and command are new.*
- **AW-W2. Workflow reader.** A selected workflow shows its name, an enable toggle, its description, and "Edited N days ago · version N" ("Built in" for a built-in). Below it, **How it runs** draws the stages of its loop — Start, Frame the case, Gather evidence, Weigh and review, Decide, Hand off — and four panels that follow the selected stage, or describe the whole workflow when none is selected:
  - **Who does it** — the agent acting in that stage (lead, helpers or reviewer), its model, and the skills it is offered.
  - **What it may do on its own** — each capability, marked On its own or Asks you, from the approval rules Vigil already enforces.
  - **Stops when** — the workflow's objectives, its step limit and its budget. Per-stage stop conditions are **Not measured yet**.
  - **Checkpoints** — where the workflow pauses for a person, each marked auto or ask.
- **AW-W3. Single-agent workflows say so.** A workflow that runs as one agent shows that agent, its objectives and its instructions, and states that it runs alone. It never shows empty helper panels.
- **AW-W4. The graph builder is removed.** "Test on a sample alert" and "Generate with AI" stay; a generated workflow is labelled an AI draft until saved.
- **AW-W5. Watch a run** is a page that replays a run step by step from its record, opened from Watch it run or from the run history:
  - **Player** — step through each decision, with the version of the workflow the run used.
  - **What the lead agent did** — each decision, what it was aimed at, which helper it sent, its cost, and its reasoning labelled as model text.
  - **Explanations being tested** — each explanation with its status and evidence for and against.
  - **Limits used** — cost, steps and scope against their limits.
  - **Reviewer** — the reviewer's verdict.
  - **Blind spots hit** — every source the run could not reach, and why (AW-D2).

  A run that tests no explanations says so instead of showing an empty panel.

### Agents tab

- **AW-A1. Agent list.** Every built-in and custom agent, what it does, and its model with where that model comes from ("Set for this agent" or the default from Settings).
- **AW-A2. Agent drawer.** Name, specialization, instructions, model and fallback model, thinking, longest answer, and the tools it may use, each marked On its own or Asks you. The drawer states that workflow runs take their model from Settings › AI models. *Model, fallback model and that statement shipped (#1376); the rest is the board's layout.*
- **AW-A3. Editing a built-in agent saves a copy.** "Built in. Saving creates your own editable copy; the original stays available."
- **AW-A4. Skills on an agent are shown, not assigned.** An agent with skills enabled is offered the whole library; the drawer says so in one line. Assigning single skills: **Later**.

### Skills tab

- **AW-S1. Skill folders.** Every skill, built-in and the operator's own, with its description, file count, the workflows it is offered to, and "Built in" or "Yours". *The list and "Offered to" shipped (#1366) for built-in skills.* "Used by N agents" and the sources a skill touches are **Not measured yet**.
- **AW-S2. Skill drawer.** Name, when to use it, the steps, the files in the skill, Test with a sample, and Save new version.
  - The name and description limits the board states are enforced.
  - Editing a built-in skill saves a copy under a new name; the built-in is never changed.
  - Each save increments the skill's version.
  - Only the steps file is editable; other files open read-only.
  - Test with a sample runs the skill's own test cases and shows pass or fail per case; a skill with none says so.
  - A skill of one's own can be deleted, with press-and-hold.

### Commands tab

- **AW-C1. Commands.** The command bar's built-in set — name, arguments, what it runs. The five live commands, then `/hold`, `/isolate`, `/phish` and custom commands shown as **Later**. One list, shared with the command bar.

## 4. Definition of done

On top of `PRD-phase1.md` section 14, this document is done when:

1. Every workflow, of every kind, opens in the reader with all four panels filled or explicitly labelled; none is blank.
2. A run whose source is not connected shows that source under Blind spots hit, for an investigation as well as a hunt. A test proves no capability is dropped silently.
3. The built-in definitions name what helpers may use in one vocabulary, and a check fails the build if a definition mixes the two.
4. Selecting each stage in How it runs changes the four panels to that stage.
5. The graph builder and its saved layout are gone, and nothing else in the console referred to them.
6. Saving a built-in agent or skill creates a copy, and the built-in is unchanged afterwards.
7. A workflow's version changes when its definition changes and not when it is enabled or disabled; a replay shows the version the run used.
8. A skill saved in the drawer is offered to agents on the next run without a restart, and Test with a sample reports per case.
9. The Commands tab and the command bar show the same list from one source.

## 5. Decisions

1. **The reader replaces the builder.** The builder shows an order the engine does not follow.
2. **One vocabulary for every kind of run.** A workflow should not depend on which tools a deployment happens to have, and a missing source should always be visible.
3. **Single-agent workflows get a reduced reader, not added helpers.** Adding helpers changes how those workflows run and what they cost; that is a separate decision, made on its own merits.
4. **On its own / Asks you is derived from the approval rules, not set per tool.** Setting it per tool is tool permissions, which is omitted.
5. **Skills are files the operator owns; built-ins are never edited.** Editing a built-in agent or skill makes a copy.
6. **Version is shown; authorship is omitted.** The product is single-user. There is a version number, not a history.

## 6. Phase 2 map

Editing a workflow's stages. Per-agent skill assignment and per-skill usage. Editing a skill's scripts, and skill import. Version history with restore. Custom and role-gated commands. Tool permissions. Trust, tier and agreement as measured values. Adding helpers to single-agent workflows, if they should have them.
