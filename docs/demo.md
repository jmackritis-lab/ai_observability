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
2. **Launcher on PATH** (optional - only for repos *without* a committed `.claude/settings.json`). PowerShell profile:
   ```powershell
   Set-Alias claude-attr C:\repos\claude-code-telemetry\client\claude-attr.ps1
   ```
   (`Set-ExecutionPolicy -Scope CurrentUser RemoteSigned` once if scripts are blocked.)
3. **Demo repo prepared.** This repo already commits `.claude/settings.json` (project + epic).
   Create the git-ignored story override — settings replace the shell variable, so all three keys:
   ```json
   // .claude/settings.local.json
   {"env": {"OTEL_RESOURCE_ATTRIBUTES": "project=claude-code-telemetry,jira.epic=EPMDEDP-17184,jira.story=EPMDEDP-17201"}}
   ```
   Any session opened here (VS Code or terminal) is now attributed; no launcher needed. Verify with
   `claude -p "reply ok" --model claude-haiku-4-5-20251001` and check Usage Audit shows project
   `claude-code-telemetry` / story `EPMDEDP-17201` within ~30 s.
   **Untagged sessions** for the alert arc must be started in a directory with *no* `.claude/settings.json`,
   e.g. an empty scratch folder.
4. **Slack webhook present** at `local/secrets/slack-webhook` (see `local/secrets/README.md`).

## The morning of

### 1. Reseed

Reseed so the last-24h tiles and the right edge of every trend are populated (the seed ends at seed
time; re-seeding on top would double-count, so the script wipes first):

```powershell
.\local\demo\reset_and_seed.ps1 -SlackTest        # ~5-6 min; posts a fresh Slack message at the end
```
(`./local/demo/reset_and_seed.sh --slack-test` from bash.)

Then pick the drill-down target: Epic Scorecard → note a high-spend engineer → My Usage → copy the
top *Session* id → paste into Session Explorer's dropdown → confirm the cost tile matches. Leave it open.

### 2. Trip the attribution alert and park coverage at 89.x%

The alert fires **before** the demo; the demo itself only shows the recovery. Goal: leave the
unattributed share of the last 24 h at **10.2-10.7%** (coverage 89.3-89.8%), i.e. just over the 10%
threshold, so that a couple of dollars of tagged spend during screen 3 push it back under and the
`[RESOLVED]` Slack message lands while you are still talking.

Watch the live number in Prometheus (http://localhost:9090) throughout:

```promql
100 * sum(increase(claude_code_cost_usage_USD_total{project="unattributed"}[1d])) / sum(increase(claude_code_cost_usage_USD_total[1d]))
```

| Step | What to do | Expect |
|------|------------|--------|
| a | Right after the seed the number reads ~7%. In an **empty scratch folder** (no `.claude/settings.json` anywhere above it) start `claude` with the default model and ask one question about a big file — about **$0.50-0.70** of spend. | Number moves within ~30 s (10 s export + 15 s scrape) |
| b | Re-run the query. Below 10.2%? Ask one more small question in the same untagged session. **Over 10.7%? Stop** — every extra untagged dollar costs 9 tagged dollars to undo during the demo. | Land in 10.2-10.7% |
| c | Close the untagged session. Wait ~11 min (10 min `for:` hold + 30 s group wait). | **Red** Slack message: `ClaudeCodeAttributionCoverageLow` firing. Alertmanager must have *sent* the firing notice or it will not send a resolved one later. |
| d | From now until screen 3: **no Claude Code activity in this repo** (terminal or VS Code). Any tagged spend clears the alert early and wastes the recovery moment. Untagged activity elsewhere is fine but pushes the number up. | http://localhost:9093 shows the alert firing |

Rule of thumb from the seeded baseline (total ≈ $38, untagged ≈ $2.8): about **$1.30** untagged lands
at ~10.5%, and clearing that then needs about **$2** tagged. Formula if the seed differs:
extra untagged to reach share *S* = (*S*·total − untagged) / (1 − *S*); tagged needed to clear = 10·untagged − total.

The number drifts during the day as seeded spend from 24 h ago slides out of the window (the live
untagged dollars stay, the seeded tagged dollars leave), so re-check it in the preflight — see there.

## The recovery arc during the demo (screen 2 → screen 3 → Slack)

| When | What happens | Timing |
|------|--------------|--------|
| Screen 2 | Attribution Health shows your e-mail as the top untagged launcher; Slack shows this morning's **red** message. "This fired at 09:xx on exactly this condition." | already there |
| Screen 3, first thing | From the repo root start the tagged fan-out (each session ~$1, all attributed to project/epic/story by the committed settings): `for i in 1 2 3; do claude -p "Write a detailed critical review of this document" < big_pdf.txt > /dev/null & done` — then ask your interactive question in this repo while they run. | Usage Audit numbers move within ~10 s |
| +1-2 min | Share drops under 10% → alert *inactive* on the next 15 s evaluation. Show it on http://localhost:9093 or the Attribution coverage tile if you like. | immediate |
| +1 to +5 min | Alertmanager's next 5-min group tick sends the **green** `[RESOLVED] ClaudeCodeAttributionCoverageLow` message; its body says *RESOLVED* and quotes the last reading while firing. Switch to Slack when you hear the ping — during screen 4 or Q&A is fine. | ≤ 5 min after clearing |

If the number was parked at ~10.5% three fan-out sessions are plenty; if the preflight shows it drifted
to 11%+, start five or six. Measured 2026-09-09: $1.80 untagged needed ~$7 tagged to clear, so parking
low matters.

## Five-minute preflight

- [ ] `docker info` succeeds; `docker compose ps` (in `local/`) shows five containers Up
- [ ] http://localhost:9090/targets — both targets UP
- [ ] http://localhost:9093 — `ClaudeCodeAttributionCoverageLow` **firing** (not pending) and this morning's red message in Slack
- [ ] Live unattributed share (PromQL above) reads 10.2-10.7%. Drifted to 11%+? Plan for 5-6 fan-out sessions in screen 3, or run one tagged `claude -p` now to trim it (stay above 10.1%!). Dropped under 10%? The alert has cleared — repeat step 2a/2b with a tiny untagged prompt and wait out the 11 min again.
- [ ] Grafana opens on Executive Summary; range **Last 14 days**; projector zoom set
- [ ] Tabs in order: Executive · Attribution Health · Usage Audit · Session Explorer · Slack channel
- [ ] `.claude/settings.local.json` present in the demo repo with the story; a scratch folder ready for the untagged session
- [ ] `big_pdf.txt` (git-ignored; regenerate with `pdftotext -layout big_pdf.pdf big_pdf.txt`) present in the repo root for the tagged fan-out; a terminal open at the repo root with the fan-out command pasted and ready; no other Claude Code session open in this repo
- [ ] No seeder / reload terminals still running

## The four screens (≈12 min)

| # | Screen | Show | Say |
|---|--------|------|-----|
| 1 | **Executive Summary** | Spend, active engineers, cost per commit, run-rate by project | "Claude Code's own cost counters, every developer, no prompts or code ever collected." Point at **Attribution coverage**: "that's the health of the rollout, not of the engineers." Click it. |
| 2 | **Attribution Health** | Who launches untagged sessions; the sentinel table | "Tagging is enforced server-side in the collector — nobody can opt out by editing local settings." Switch to Slack: "and this is the alert that fired this morning on exactly this condition — red, still open." Mention the window: alert = last 24 h, tile = selected range. |
| 3 | **Usage Audit** | Dropdowns project → epic → story; then the live sessions | Kick off the tagged fan-out from the repo root, then ask Claude Code something small in this repo (settings attribute both). Filter to that project/story; the numbers move within ~10 s. "One Jira ticket ≈ one session; that's the whole discipline we ask of engineers." The alert clears in the background; the green `[RESOLVED]` Slack message follows within 5 min — show it when it lands. |
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
  `unattributed` when started outside a repo with committed attribution settings — that is the
  live-fix moment in screen 3.
- Synthetic Bash events carry a redacted placeholder command; only real sessions show real commands.
- Resolved notifications are on: when the attribution alert clears in screen 3, Slack gets a green message titled `[RESOLVED] ...` whose body says *RESOLVED* explicitly. The quoted percentage is the last value seen while firing (e.g. 10.3%), not the current one — say so if asked.

## Recovery

| Symptom | Fix |
|---------|-----|
| `failed to connect to the docker API` | Docker Desktop died: relaunch it, `docker info`, then `docker compose up -d` in `local/` |
| Dashboards flat at the right edge | `reset_and_seed.ps1` (history is anchored at seed time) |
| Slack silent | `docker compose logs alertmanager --since 5m` — a notify error names the receiver / missing webhook file |
| Alert shows a different % than the tile | Alert = last 24 h; tile follows the time picker |
| Alert cleared before the demo (someone used Claude Code in this repo) | Tiny untagged prompt in the scratch folder to cross 10% again, then wait the 11 min for the red message — the resolved notice only goes out for an alert that was notified as firing |
| Green message has not arrived 6 min after the alert went inactive | Check http://localhost:9093 shows no active alert, then `docker compose logs alertmanager --since 10m`; a `notify` error names the receiver |
