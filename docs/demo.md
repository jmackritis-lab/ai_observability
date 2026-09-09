# Demo runbook — Claude Code usage analytics (local stack)

A 12-minute walk from board level to a single terminal, on the docker-compose stack with seeded
history. Everything here runs on one laptop; nothing leaves it except Slack alert notifications.

## The day before

1. **Docker Desktop headroom.** The Helm bundle on docker-desktop Kubernetes is not part of the demo
   and competes for memory (Docker Desktop has crashed mid-session twice on this laptop):
   ```powershell
   helm uninstall claude-code-telemetry -n claude-code-telemetry --kube-context docker-desktop
   ```
   then Docker Desktop → Settings → Kubernetes → untick *Enable Kubernetes* → Apply. Re-enable it
   later for the Helm work.
2. **Launcher on PATH** (for the live tagged session). PowerShell profile:
   ```powershell
   Set-Alias claude-attr C:\repos\claude-code-telemetry\client\claude-attr.ps1
   ```
   (`Set-ExecutionPolicy -Scope CurrentUser RemoteSigned` once if scripts are blocked.)
3. **Demo repo prepared** — a branch carrying a Jira key and the epic pinned:
   ```powershell
   git checkout -b EPMDEDP-17201-demo
   git config claude.jiraEpic EPMDEDP-17184
   claude-attr --version        # first line must show project / jira.epic / jira.story resolved
   ```
4. **Slack webhook present** at `local/secrets/slack-webhook` (see `local/secrets/README.md`).

## The morning of

Reseed so the last-24h tiles and the right edge of every trend are populated (the seed ends at seed
time; re-seeding on top would double-count, so the script wipes first):

```powershell
.\local\demo\reset_and_seed.ps1 -SlackTest        # ~5-6 min; posts a fresh Slack message at the end
```
(`./local/demo/reset_and_seed.sh --slack-test` from bash.)

Then pick the drill-down target: Epic Scorecard → note a high-spend engineer → My Usage → copy the
top *Session* id → paste into Session Explorer's dropdown → confirm the cost tile matches. Leave it open.

## Five-minute preflight

- [ ] `docker info` succeeds; `docker compose ps` (in `local/`) shows five containers Up
- [ ] http://localhost:9090/targets — both targets UP
- [ ] http://localhost:9093 — attribution alert visible (expected until you launch tagged sessions)
- [ ] Grafana opens on Executive Summary; range **Last 14 days**; projector zoom set
- [ ] Tabs in order: Executive · Attribution Health · Usage Audit · Session Explorer · Slack channel
- [ ] `claude-attr --version` in the demo repo prints the expected tags
- [ ] No seeder / reload terminals still running

## The four screens (≈12 min)

| # | Screen | Show | Say |
|---|--------|------|-----|
| 1 | **Executive Summary** | Spend, active engineers, cost per commit, run-rate by project | "Claude Code's own cost counters, every developer, no prompts or code ever collected." Point at **Attribution coverage**: "that's the health of the rollout, not of the engineers." Click it. |
| 2 | **Attribution Health** | Who launches untagged sessions; the sentinel table | "Tagging is enforced server-side in the collector — nobody can opt out by editing local settings." Switch to Slack: "and this is the alert that fired on exactly this condition." Mention the window: alert = last 24 h, tile = selected range. |
| 3 | **Usage Audit** | Dropdowns project → epic → story; then the live session | Run `claude-attr` in the demo repo, ask it something small. Filter to that project/story; the numbers move within ~10 s. "One Jira ticket ≈ one session; that's the whole discipline we ask of engineers." |
| 4 | **Session Explorer** | The pre-selected session: cost, tool sequence, prompts panel | "Every tool call, its duration and whether it was allowed — and the prompt itself is `<REDACTED>` by design." |

Keep **Epic Scorecard** (chargeback), **My Usage** (per-engineer transparency), **Governance &
Security** (permissions, MCP, plugins) and **Collector Health** open for questions.

## Talking points that land

- Spend is an *input*; commits, PRs and lines changed sit next to it on purpose.
- The `unattributed` / `invalid` sentinels turn a rollout gap into a KPI with an alert, instead of
  silently missing data.
- Same pipeline, two deployments: `local/` is the laptop testbed, `deploy-templates/` the Helm bundle
  for the team cluster — dashboards, rules and routing are byte-identical.
- What is **not** here: prompts, responses, file contents, raw API bodies — disabled in the managed
  settings and never emitted.

## Known quirks

- Seeded working hours are UTC (8–19), so a *1-day* view in US time zones looks early. Invisible on 14 days.
- Your own sessions appear with your real e-mail among the `example.com` roster, and as
  `unattributed` until launched via `claude-attr` — that is the live-fix moment in screen 3.
- Synthetic Bash events carry a redacted placeholder command; only real sessions show real commands.
- Resolved notifications are on: when the attribution alert clears mid-demo, Slack gets a green message.

## Recovery

| Symptom | Fix |
|---------|-----|
| `failed to connect to the docker API` | Docker Desktop died: relaunch it, `docker info`, then `docker compose up -d` in `local/` |
| Dashboards flat at the right edge | `reset_and_seed.ps1` (history is anchored at seed time) |
| Slack silent | `docker compose logs alertmanager --since 5m` — a notify error names the receiver / missing webhook file |
| Alert shows a different % than the tile | Alert = last 24 h; tile follows the time picker |
