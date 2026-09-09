#!/usr/bin/env bash
# Wipe the local stack and reseed it with fresh demo history — run this the morning of a demo.
# Bash twin of reset_and_seed.ps1. Extra args are passed to seed_demo_data.py; --slack-test posts a
# synthetic alert afterwards (needs local/secrets/slack-webhook).
#   ./local/demo/reset_and_seed.sh
#   ./local/demo/reset_and_seed.sh --days 21 --slack-test
set -euo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
local_dir="$(cd "$here/.." && pwd)"

slack_test=0; seed_args=()
for a in "$@"; do [[ "$a" == "--slack-test" ]] && slack_test=1 || seed_args+=("$a"); done

wait_http() { for _ in $(seq 1 120); do curl -sf -m 3 "$1" >/dev/null 2>&1 && return; sleep 1; done; echo "timed out waiting for $1" >&2; exit 1; }

echo "[1/5] docker daemon"
docker info >/dev/null 2>&1 || { echo "Docker is not running — start Docker Desktop and retry." >&2; exit 1; }

echo "[2/5] wiping stack (docker compose down -v)"
( cd "$local_dir" && docker compose down -v )
echo "[3/5] starting stack"
( cd "$local_dir" && docker compose up -d )

echo "[4/5] waiting for Prometheus, Loki, Grafana, Alertmanager"
wait_http http://localhost:9090/-/ready
wait_http http://localhost:3100/ready
wait_http http://localhost:3000/api/health
wait_http http://localhost:9093/-/ready

echo "[5/5] seeding demo data ${seed_args[*]:-}"
python "$here/seed_demo_data.py" "${seed_args[@]}"

if (( slack_test )); then
  [[ -f "$local_dir/secrets/slack-webhook" ]] || echo "WARN: local/secrets/slack-webhook missing — routing works, Slack delivery will fail." >&2
  curl -s -X POST localhost:9093/api/v2/alerts -H 'Content-Type: application/json' \
    -d '[{"labels":{"alertname":"DemoTest","owner":"governance","severity":"warning"},"annotations":{"summary":"Demo stack reseeded","description":"Fresh 14-day history loaded; alert routing to Slack verified."}}]'
  echo "Slack test alert posted (arrives within ~30s, auto-resolves in ~5 min)."
fi

cat <<EOF

Done. Grafana  http://localhost:3000  (home = Executive Summary; set range to Last 14 days)
      Alerts   http://localhost:9093   Prometheus http://localhost:9090/alerts
Remember: your own Claude Code sessions land as 'unattributed' unless launched via client/claude-attr.
EOF
