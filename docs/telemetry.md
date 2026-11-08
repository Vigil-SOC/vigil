# Telemetry

Vigil emits OpenTelemetry traces and metrics. This page is the schema a
collector, a dashboard or a headless client can rely on.

## Turning it on

Export runs if and only if `OTEL_EXPORTER_OTLP_ENDPOINT` is set to a non-empty
value (an OTLP/gRPC address such as `http://localhost:4317`). The same test is
made by the Python processes (`core/telemetry.py`) and the agent layer
(`services/agent/core/tracing.ts`). Empty or unset, no process opens a
connection to a collector and every span is a no-op. There is no other switch.

- `./start.sh --all` or `./start.sh --with otel-collector` starts the bundled
  collector and sets the endpoint to `http://localhost:4317` when you left it
  empty. A value you set in `.env` or the shell wins.
- Compose passes `OTEL_EXPORTER_OTLP_ENDPOINT` through to its services, empty by
  default. Inside the Compose network the collector is `http://otel-collector:4317`.
- Helm ships `config.OTEL_EXPORTER_OTLP_ENDPOINT: ""`. `otelCollector.enabled`
  points it at the in-chart collector.

Metrics do not depend on the endpoint. Every Python process installs a
Prometheus reader, so `GET /metrics` on the backend (with the metrics token)
and the daemon's `:9090` listener serve Vigil's instruments (`vigil_llm_*`,
`vigil_runs_*`, and the rest) either way. With an endpoint set, the same
instruments are also pushed over OTLP, which is how the llm-worker, which has
no HTTP port, reaches the collector's Prometheus exporter.

`OTEL_TRACES_SAMPLER` and `OTEL_TRACES_SAMPLER_ARG` are read by the SDKs as
usual. When Sentry is configured and an endpoint is set, Sentry's own tracing
is turned off so a request is not traced twice.

## Services

`service.name` on each process's resource, beside `service.version`
(`RELEASE_VERSION`) and `deployment.environment` (`ENVIRONMENT`):

| `service.name`       | Process                                     |
|----------------------|---------------------------------------------|
| `vigil-backend`      | API (`services/api`)                        |
| `vigil-daemon`       | SOC daemon (`services/daemon`)              |
| `vigil-llm-worker`   | ARQ LLM worker (`services/worker`)          |
| `vigil-agent-worker` | Agent run worker (`services/agent/worker.ts`) |
| `vigil-agent-serve`  | Agent chat server (`services/agent/serve.ts`) |

Trace context crosses processes as W3C `traceparent`: on the run job (so a
run's spans continue the request that started it), on tool calls from the agent
layer back to the backend, and on llm-worker jobs.

## Spans

Names and attributes follow the OpenTelemetry GenAI semantic conventions where
one applies.

| Span name                 | Where                       | Kind     | What it covers |
|---------------------------|-----------------------------|----------|----------------|
| `invoke_agent <run_kind>` | agent worker                | internal | One run job: a start, or a resume after a park. The run's root span. |
| `invoke_agent chat`       | agent serve                 | server   | One console chat turn. |
| `invoke_agent narrate`    | agent serve                 | server   | Writing a run's narrative. |
| `chat <model>`            | agent worker and serve      | client   | One model call, including time queued behind the rate limiter (an `admitted` event marks the end of the wait). |
| `execute_tool <tool id>`  | agent worker and serve      | internal | One tool call, local or remote. |
| `mcp.call_tool`           | backend (MCP client)        | client   | The backend's call to an MCP server, under the `execute_tool` span that asked for it. |
| `investigation`           | daemon                      | internal | One daemon investigation. |
| `llm_worker.execute`      | llm-worker                  | consumer | One queued LLM job. |

A run parked on an approval resumes as a second job, hours later: it gets its
own `invoke_agent` span in its own trace. `vigil.run_id` ties the two together.

The code-made searches of a root-cause trace (`root_cause` runs) go through the
same `execute_tool` span but carry no `gen_ai.tool.call.id`: they are journaled
after the call, under an id minted there.

### GenAI attributes

| Attribute                         | On |
|-----------------------------------|----|
| `gen_ai.operation.name`           | `invoke_agent`, `chat`, `execute_tool` spans |
| `gen_ai.agent.name`               | `invoke_agent`: the run kind, or `chat` / `narrate` |
| `gen_ai.agent.id`                 | `invoke_agent chat` and `execute_tool`, when the call names an agent |
| `gen_ai.provider.name`            | `chat` |
| `gen_ai.request.model`            | `chat`, `llm_worker.execute` |
| `gen_ai.response.model`           | `chat` |
| `gen_ai.response.finish_reasons`  | `chat` |
| `gen_ai.usage.input_tokens`       | `chat` |
| `gen_ai.usage.output_tokens`      | `chat` |
| `gen_ai.tool.name`                | `execute_tool` |
| `gen_ai.tool.call.id`             | `execute_tool`: the `call_id` of the call in the run's ledger |
| `gen_ai.system`                   | `llm_worker.execute` |

### Vigil attributes

| Attribute               | On | Value |
|-------------------------|----|-------|
| `vigil.run_id`          | `invoke_agent` | The run's id. |
| `vigil.job.reason`      | `invoke_agent <run_kind>` | `start` or `resume`. |
| `vigil.case_id`         | `invoke_agent <run_kind>` | The case the run was started on. Absent when it was started on none. |
| `vigil.workflow_id`     | `invoke_agent <run_kind>` | The workflow definition the run executes (`workflow:<id>` without the prefix). Absent for a run started from a playbook file. |
| `vigil.tool.name`       | `mcp.call_tool` | The MCP tool. |
| `vigil.tool.input_size` | `mcp.call_tool` | Bytes of JSON arguments. |
| `vigil.tool.output_size`| `mcp.call_tool` | Bytes of JSON result. |
| `vigil.tool.duration_ms`| `mcp.call_tool` | Time inside the call. |
| `vigil.tool.success`    | `mcp.call_tool` | Whether the call succeeded. |
| `vigil.investigation.*` | `investigation` | `id`, `workflow_id`, `trigger_type`, `priority`, `finding_count`. |

`mcp.call_tool` also carries `mcp.server.name` and `mcp.transport`.

## Joining a span to a run, a case and the trail

`vigil.run_id` is the key. It is the same id as:

- `agent_events.run_id`, the run's ledger. A tool call's ledger event carries
  `call_id`, which is the `gen_ai.tool.call.id` on its `execute_tool` span.
- `workflow_runs.run_id`, whose `trigger_context.case_id` is the case
  (also on the root span as `vigil.case_id`) and whose `workflow_id` is the
  definition (`vigil.workflow_id`).
- the `run_id` in the payload of the audit-trail entries for that run
  (`agent.run.start` and the rest, under Settings › Audit).

The JSON log lines of the Python processes carry `trace_id` and `span_id` of the
span active when they were written.

## Content capture

Spans carry no prompts, responses, tool arguments or tool results by default.
Python spans never do: `core/telemetry_sanitizer.py` redacts them and there is
no opt-in. On the agent layer, `VIGIL_OTEL_CAPTURE_CONTENT=true` adds:

| Attribute                    | On |
|------------------------------|----|
| `gen_ai.input.messages`      | `chat`: the messages added since the model last spoke |
| `gen_ai.output.messages`     | `chat`: the model's reply |
| `gen_ai.tool.call.arguments` | `execute_tool` |
| `gen_ai.tool.call.result`    | `execute_tool` |

Each value is capped at 16 KiB. That content is customer logs and IOCs, and the
agent layer does not scrub secrets from it, so leave it off outside
development. The ledger holds all of it anyway, under `vigil.run_id`.
