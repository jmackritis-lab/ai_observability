# Claude Code Usage Analytics

> Status: **Design / RFC** — for team review before any install.
> Goal: understand, at a low level, **for which project / story / epic** we spend
> Claude Code tokens and cost, and **which models, agents, skills, and tools** are involved —
> then aggregate it in a self-hosted observability stack (local → team → Kubernetes).

---

## 1. Summary

Claude Code has native **OpenTelemetry (OTEL)** support. It emits **metrics** (aggregatable
numbers) and **events/logs** (per-call detail) over standard OTLP to any collector. We attribute
usage to our own business dimensions — project, Jira epic, Jira story (user identity) — by injecting
**custom resource attributes** and enforcing them in an **OTel Collector**.

- **Metrics** → Prometheus → Grafana: "how much" dashboards (cost/tokens by model/agent/skill/project).
- **Events/logs** (full content) → Loki → Grafana: "what exactly happened" forensics and
  high-cardinality attribution (per-Jira-ticket cost, per-prompt breakdown).

---

## 2. What Claude Code emits

> **Full vocabulary lives in [`../spec/`](../spec/README.md)** — the exhaustive, DRY catalog of every
> config var, metric, event, span, and attribute (with upstream links to refresh from). The tables
> below are the *curated subset* this design relies on; consult `spec/` for anything not listed here.

### 2.1 Enable (env vars — placed in `.claude/settings.json` `env` block)

| Var                            | Value                               | Purpose                                                  |
|--------------------------------|-------------------------------------|----------------------------------------------------------|
| `CLAUDE_CODE_ENABLE_TELEMETRY` | `1`                                 | Master switch                                            |
| `OTEL_METRICS_EXPORTER`        | `otlp`                              | Metrics transport                                        |
| `OTEL_LOGS_EXPORTER`           | `otlp`                              | Events/logs transport                                    |
| `OTEL_EXPORTER_OTLP_PROTOCOL`  | `grpc`                              | OTLP protocol (`grpc` \| `http/protobuf` \| `http/json`) |
| `OTEL_EXPORTER_OTLP_ENDPOINT`  | `http://localhost:4317`             | Collector endpoint (local compose stack)                 |
| `OTEL_EXPORTER_OTLP_HEADERS`   | `Authorization=Bearer …`            | Auth (team stage)                                        |
| `OTEL_METRIC_EXPORT_INTERVAL`  | `10000`                             | Metrics flush (ms)                                       |
| `OTEL_LOGS_EXPORT_INTERVAL`    | `3000`                              | Logs flush (ms)                                          |

Content-capture gates — **PoC keeps prompts, responses, file contents, and raw API bodies OFF**
(privacy stance in the README). Only metadata is captured:
`OTEL_LOG_TOOL_DETAILS=1` (un-redacts skill/agent/MCP-tool names + bash commands and file paths),
while `OTEL_LOG_USER_PROMPTS=0`, `OTEL_LOG_ASSISTANT_RESPONSES=0`, `OTEL_LOG_TOOL_CONTENT=0`,
`OTEL_LOG_RAW_API_BODIES=0`. Flip these on only for a deliberate, isolated forensic session.

> **Note:** Claude Code does **not** propagate `OTEL_*` to subprocesses (Bash tool calls). Only the
> CLI process is instrumented.

### 2.2 Metrics (fixed names — cannot add new ones)

| Metric                                | Unit   | Key attributes                                                                                                                                            |
|---------------------------------------|--------|-----------------------------------------------------------------------------------------------------------------------------------------------------------|
| `claude_code.token.usage`             | tokens | `type` (input/output/cacheRead/cacheCreation), `model`, `agent.name`, `skill.name`, `mcp_server.name`, `mcp_tool.name`, `query_source`, `speed`, `effort` |
| `claude_code.cost.usage`              | USD    | same as above + `plugin.name`, `marketplace.name`                                                                                                         |
| `claude_code.session.count`           | count  | `start_type` (fresh/resume/continue/agents_view)                                                                                                          |
| `claude_code.lines_of_code.count`     | count  | `type` (added/removed), `model`                                                                                                                           |
| `claude_code.commit.count`            | count  | —                                                                                                                                                         |
| `claude_code.pull_request.count`      | count  | —                                                                                                                                                         |
| `claude_code.active_time.total`       | s      | `type` (user/cli)                                                                                                                                         |
| `claude_code.code_edit_tool.decision` | count  | `tool_name`, `decision` (accept/reject), `source`, `language`                                                                                             |

### 2.3 Events / logs (fixed names)

`user_prompt`, `assistant_response`, `tool_result`, `tool_decision`, `api_request`, `api_error`,
`api_refusal`, `api_request_body`/`api_response_body`, `skill_activated`, `mcp_server_connection`,
`plugin_installed`/`plugin_loaded`, `hook_*`, `compaction`, `at_mention`, `permission_mode_changed`,
`auth`, `feedback_survey`, `internal_error`.

Attribution-relevant:

- `api_request` → `model`, `cost_usd`, `input_tokens`, `output_tokens`, `cache_read_tokens`,
  `cache_creation_tokens`, `request_id`, `agent.name`, `skill.name`, `mcp_*`, correlated by `prompt.id`.
- `skill_activated` → `skill.name`, `invocation_trigger` (user-slash/claude-proactive/nested-skill),
  `skill.source`, `skill.kind`, `plugin.name`.
- `tool_decision` / `tool_result` → `tool_name`, `decision`, `duration_ms`, `success`, `error_type`.
- `workspace.host_paths` → host workspace directories (closest built-in "which repo" signal).

> Skills, subagents, and MCP tools are each **distinctly identifiable** — `skill.name`, `agent.name`,
> `mcp_server.name`/`mcp_tool.name`. Beta tracing adds a parent/child span tree for subagents.

---

## 3. Attribution model (the core design)

We enrich Claude Code telemetry with **custom business dimensions**. The only client injection
channel is `OTEL_RESOURCE_ATTRIBUTES` (comma-separated `key=value`), which is **read once at
`claude` startup and static for the whole session**. Discipline: **one Jira ticket ≈ one session**.

### 3.1 Custom attribute schema

Exactly three business dimensions as starting point:

| Key          | Example         | Source | Cardinality          | Home                                  |
|--------------|-----------------|--------|----------------------|---------------------------------------|
| `project`    | `krci-portal`   | manual | low                  | **metric**                            |
| `jira.epic`  | `EPMDEDP-15000` | manual | medium               | **metric**                            |
| `jira.story` | `EPMDEDP-17147` | manual | **high (unbounded)** | **logs** (drop from metrics at scale) |

`project` is the git repository name (a KRCI Codebase).

### 3.2 Cardinality rule (the "scalarity" concern)

In Prometheus every **unique label-value combination = one stored time series**. Cardinality grows
down the hierarchy: `project → jira.epic → jira.story`.

- **Low/bounded** (`model`, `type`, `agent.name`, `skill.name`, `tool_name`, `project`,
  `jira.epic`) → safe as **metric labels** → fast Grafana breakdowns.
- **High/unbounded** (`jira.story`, `session.id`, `prompt.id`) → expensive as metric labels;
  the long-range answer is **recording rules** (below) and **LogQL** over events, not raw series.

**Why `session.id` stays on the raw metrics (do not strip it in the collector).** `session.id`
is the *only* per-process identity Claude Code emits — there is no `service.instance.id`. Its
metrics are cumulative counters. If two concurrent sessions of the same user on the same
project/epic/story/model lose `session.id`, their counters collapse onto one label set in the
collector's Prometheus exporter and **overwrite each other (last write wins, not summed)** —
totals go silently wrong. The same applies to `OTEL_METRICS_INCLUDE_SESSION_ID=false` on the
client. So: keep `session.id` on raw series, bound the *cost* of it with retention and with
recording rules, and query the raw series only over short windows.

**Recording rules are the cardinality-safe layer** (`local/prometheus-rules.yml`, mirrored in
`deploy-templates/values.yaml → prometheus.serverFiles.recording_rules.yml`). They pre-aggregate
the raw counters into session-free series keyed only on bounded dimensions
(`claude_code:cost_usd:rate5m`, `claude_code:tokens:rate5m`, `claude_code:active_time_seconds:rate5m`,
`claude_code:cost_usd:increase1h`) plus two governance ratios
(`claude_code:cost_usd_unattributed:ratio_rate5m`, `claude_code:cost_usd_invalid_jira:ratio_rate5m`).
Dashboards that trend more than ~7 days should query these instead of the raw counters.

**`jira.story` is currently kept as a metric label** because every usage dashboard drills down on
it (template variable + `jira_story=~"$jira_story"` filters). Demoting it to logs-only is a
dashboard redesign, not just a collector change; revisit when
`count(count by (jira_story) (claude_code_token_usage_tokens_total))` grows past a few thousand.

Other built-in cardinality gates: `OTEL_METRICS_INCLUDE_ACCOUNT_UUID`,
`OTEL_METRICS_INCLUDE_ENTRYPOINT`, `OTEL_METRICS_INCLUDE_VERSION`,
`OTEL_METRICS_INCLUDE_RESOURCE_ATTRIBUTES`.

### 3.3 Running Claude Code with attribution (manual for now)

Set `OTEL_RESOURCE_ATTRIBUTES` before starting a session, then run `claude` as usual:

```bash
export OTEL_RESOURCE_ATTRIBUTES="project=krci-portal,jira.epic=EPMDEDP-15000,jira.story=EPMDEDP-17147"
claude
```

Use `none` for a dimension that doesn't apply (e.g. exploratory work with no story). Discipline:
one Jira ticket ≈ one session — the attributes are read once at startup and stay static.

> `OTEL_RESOURCE_ATTRIBUTES` value rules: comma-separated `key=value`, US-ASCII, no
> spaces/quotes/commas/semicolons/backslashes in values (percent-encode).

**Launcher wrapper (implemented):** `client/claude-attr.sh` / `client/claude-attr.ps1` derive
`project` from the `origin` remote, `jira.story` from the branch name, and `jira.epic` from
`git config claude.jiraEpic` (set once per repo), then `exec claude`. Env overrides:
`CLAUDE_PROJECT`, `CLAUDE_JIRA_STORY`, `CLAUDE_JIRA_EPIC`. Resolving the epic from Jira
automatically remains a later phase.

### 3.4 User identity — native, no injection needed

Claude Code emits user identity as **standard attributes on every metric and event**, so we do
**not** define a custom metric or attribute for it:

| Attribute           | What it is                                | Availability                                                |
|---------------------|-------------------------------------------|-------------------------------------------------------------|
| `user.email`        | OAuth email (e.g. `jane_doe@example.com`) | when OAuth-authenticated                                    |
| `user.id`           | stable hashed user / IdP subject id       | always                                                      |
| `user.account_uuid` | Anthropic account UUID                    | gated by `OTEL_METRICS_INCLUDE_ACCOUNT_UUID` (default true) |
| `organization.id`   | org id                                    | when authenticated                                          |

These already flow into Prometheus labels (via `resource_to_telemetry_conversion`) and Loki, so
tokens/cost break down by `user_email` out of the box — no launcher change. Only inject a custom
identity attribute if you need a *different* identifier than the auth email (e.g. an internal LDAP
uid); then add `user.ldap=<uid>` to `OTEL_RESOURCE_ATTRIBUTES` alongside the others.

> **DAU/WAU/MAU depend on `user_email` staying a metric label.** The Operations & Adoption
> dashboard counts active users with
> `count(count by (user_email) (present_over_time(claude_code_session_count[<window>])))`.
> Note `present_over_time`, *not* `increase(...) > 0`: `session_count` carries `session.id`, so each
> session is a distinct flat series whose `increase()` is always 0. If `user_email` is ever demoted
> to logs-only for cardinality reasons, move this count to LogQL over the session events instead.

---

## 4. Governance — making attributes mandatory

Claude Code **cannot** refuse to start without a given attribute. Enforcement is layered:

1. **Launcher wrapper** (`client/claude-attr.*`) — always populates every key (sentinel `none`
   when a dimension does not apply). Ensures *presence* on cooperating clients.
2. **OTel Collector — the authoritative enforcement point** (server-side, users cannot bypass).
   Implemented as the `transform/attribution` processor, identical for metrics and logs:
   - missing/empty `project`, `jira.epic`, `jira.story` → sentinel **`unattributed`** (distinct
     from the user-set `none`, so "forgot to tag" and "does not apply" are separable);
   - `project` lower-cased and restricted to `[a-z0-9._-]`; Jira keys upper-cased and validated
     against `^[A-Z][A-Z0-9]*-[0-9]+$`; anything else → **`invalid`** (a typo cannot mint a label);
   - a `filter` processor that *drops* untagged telemetry was considered and rejected: dropping
     loses spend, whereas stamping `unattributed` makes the gap a KPI
     (`claude_code:cost_usd_unattributed:ratio_rate5m`, target → 0).
3. **Managed `settings.json`** (MDM/GPO, highest precedence) — `client/managed-settings.json` pins
   telemetry ON, the endpoint, privacy toggles and export intervals; users cannot unset it.
   Windows `C:/Program Files/ClaudeCode/managed-settings.json`, macOS
   `/Library/Application Support/ClaudeCode/managed-settings.json`, Linux `/etc/claude-code/managed-settings.json`.
   OTLP auth goes in the same file via `OTEL_EXPORTER_OTLP_HEADERS` or `otelHeadersHelper`
   (a script printing a JSON header map, re-run every ~29 min).

> We cannot define **new metric names**, but we *can* mandate **dimensions** and derive new metrics
> downstream (Prometheus recording rules, LogQL).

---

## 5. Rollout path

1. **Local PoC** — collector + Prometheus + Loki + Grafana as a docker-compose stack (`local/`);
   single client; validate slicing by model/agent/skill/project/ticket; iterate dashboards.
2. **Team** — ship `client/managed-settings.json` via MDM/GPO and `client/claude-attr.*` on PATH;
   standardize the attribute schema; auth the OTLP endpoint (`OTEL_EXPORTER_OTLP_HEADERS` /
   `otelHeadersHelper`, refreshed ~29 min) — **collector-side auth (bearertokenauth extension +
   TLS) is the remaining open item**; attribute enforcement is already in the collector (§4).
3. **Kubernetes target state** — the self-contained Helm bundle (`deploy-templates/`): upstream
   collector + prometheus + loki + grafana subcharts, plain Deployments, **no operator/CRD or
   cluster-wide monitoring prerequisites**; add longer retention, PVCs, OTLP auth, and multi-tenancy
   by `team`. A later step can graduate it into `edp-cluster-add-ons` as a first-class KRCI add-on.

---

## References

- Claude Code — Monitoring usage (OTEL): <https://code.claude.com/docs/en/monitoring-usage>
- Grafana Cloud Claude Code integration: <https://grafana.com/docs/grafana-cloud/monitor-infrastructure/integrations/integration-reference/integration-claude-code/>
