# local/secrets — git-ignored (only this README is tracked)

Drop notification secrets here; `docker compose` mounts the directory read-only into Alertmanager at
`/etc/alertmanager/secrets/`.

| File            | Used by                                   | Content                                                  |
|-----------------|-------------------------------------------|----------------------------------------------------------|
| `slack-webhook` | all receivers in `local/alertmanager.yml` | one line: `https://hooks.slack.com/services/T…/B…/…`     |

Create it without echoing the value into a shell history:

```powershell
# Windows PowerShell — prompts, then writes the file with no trailing newline
Set-Content -NoNewline -Encoding ascii -Path local\secrets\slack-webhook -Value (Read-Host "Slack webhook URL")
```

```bash
# bash
read -rs -p "Slack webhook URL: " u; printf '%s' "$u" > local/secrets/slack-webhook; unset u; echo
```

Alertmanager reads the file at notification time, so no restart is needed after creating or rotating
it. Test end to end without waiting for a real alert:

```bash
curl -s -X POST localhost:9093/api/v2/alerts -H 'Content-Type: application/json' -d '[{"labels":{"alertname":"DemoTest","owner":"governance","severity":"warning"},"annotations":{"summary":"Test from the local stack","description":"If you can read this in Slack, routing and delivery work."}}]'
```

The Bot User OAuth Token from the same Slack app is **not** needed — Alertmanager posts through the
incoming webhook only.
