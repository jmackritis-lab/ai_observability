# Demo data seeding (local stack only)

`seed_demo_data.py` fills the docker-compose stack with two weeks of realistic, correlated
Claude Code telemetry for a fictional 8-person team so every dashboard filter has depth:

| Level        | What the seed contains                                                                 |
|--------------|----------------------------------------------------------------------------------------|
| `project`    | 4 repos (`krci-portal`, `krci-operator`, `edp-cluster-add-ons`, `claude-code-telemetry`) |
| `jira_epic`  | 1–2 epics per project                                                                  |
| `jira_story` | 2–5 stories per epic, plus `none` (exploratory), `unattributed` (no wrapper), `invalid` (typo) |
| `user_email` | 8 `@example.com` engineers with different models, effort levels, terminals, versions   |
| `model`      | sonnet-5 / opus-5 / fable-5-1 as main models, haiku-4-5 as the auxiliary model          |

Sessions follow working hours on weekdays. Each session emits the full event vocabulary the
dashboards use (`api_request`, `tool_result`, `tool_decision`, `user_prompt`, `skill_activated`,
`plugin_loaded`, `mcp_server_connection`, `hook_execution_complete`, `permission_mode_changed`,
`api_error`, `api_retries_exhausted`, `api_refusal`, `compaction`, `auth`) and the metric counters
are **derived from those events**, so Loki and Prometheus agree per session.

## Run

```bash
cd local && docker compose up -d          # stack must be running
python demo/seed_demo_data.py             # ~2 min; restarts Prometheus twice
```

Then open Grafana → *Claude Code* folder and set the time range to **Last 14 days**.

Options: `--days N`, `--seed N` (different roster activity), `--skip-prometheus`, `--skip-loki`,
`--skip-rules` (don't backfill recording-rule series), `--via collector` (send logs through the
collector instead of Loki's OTLP endpoint), `--dry-run` (write `out/` only).

## How it works

- **Prometheus** cannot receive history through the collector (the Prometheus exporter only serves
  "now"), so the script writes cumulative counter samples as OpenMetrics text and imports them with
  `promtool tsdb create-blocks-from openmetrics` into the `prometheus-data` volume, then evaluates
  `local/prometheus-rules.yml` over the window with `promtool tsdb create-blocks-from rules`
  (every 4 min by default, `--rules-eval-interval` — must stay under Prometheus' 5 min lookback or
  rule-based panels show gaps; live evaluation is 1 min, so the most recent hour mixes both
  resolutions — fine for trends, don't `sum_over_time` the `increase1h` rule).
  Label sets mirror real post-collector series exactly, plus `collector_env="demo"`.
  Prometheus' TSDB lives at `/prometheus/data` inside the volume (the compose `command:` override
  drops the image's `--storage.tsdb.path`, so the default relative `data/` applies); the script
  reads the real path from the flags API rather than assuming.
- **Loki** accepts back-dated OTLP logs (`reject_old_samples: false`). Records are posted in strict
  global time order to a dedicated stream (`service_namespace="demo"`) so nothing is rejected as
  out-of-order, with the collector's `transform/attribution` sentinel rules applied client-side.

Real traffic from your own Claude Code keeps flowing alongside and is distinguishable by
`collector_env="local-poc"` (metrics) / absence of `service_namespace` (logs).

## Reset

```bash
# everything (real + demo)
cd local && docker compose down -v && docker compose up -d
# demo logs only (Loki delete API; compactor retention is enabled)
curl -s -X POST -G localhost:3100/loki/api/v1/delete \
  --data-urlencode 'query={service_namespace="demo"}' --data-urlencode 'start=2020-01-01T00:00:00Z'
```

Demo metric blocks cannot be deleted selectively without enabling Prometheus' admin API; use the
full reset.

The roster, projects, tool mix, prices and probabilities are plain constants at the top of the
script — edit them to match the story you want to tell.
