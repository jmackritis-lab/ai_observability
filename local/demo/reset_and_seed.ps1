<#
.SYNOPSIS
  Wipe the local stack and reseed it with fresh demo history - run this the morning of a demo.

  Steps: docker sanity check -> docker compose down -v -> up -d -> wait for Prometheus/Loki/Grafana
  -> python local/demo/seed_demo_data.py <extra args> -> (optional) post a Slack test alert.
  Takes ~5-6 minutes; most of it is the recording-rule backfill.

.PARAMETER SeedArgs
  Passed through to seed_demo_data.py, e.g. -SeedArgs "--days 21 --seed 7".
.PARAMETER SlackTest
  After seeding, post a synthetic 'DemoTest' alert to Alertmanager so the Slack channel shows a
  fresh message (requires local/secrets/slack-webhook).
.EXAMPLE
  .\local\demo\reset_and_seed.ps1
  .\local\demo\reset_and_seed.ps1 -SeedArgs "--days 21" -SlackTest
#>
param(
  [string]$SeedArgs = "",
  [switch]$SlackTest
)
$ErrorActionPreference = 'Stop'
$repo = Resolve-Path (Join-Path $PSScriptRoot '..\..')
$local = Join-Path $repo 'local'

# Native tools (docker, python) write progress to stderr; under $ErrorActionPreference='Stop' PowerShell 5.1
# turns that into a terminating NativeCommandError whenever stderr is redirected. Run them through cmd.exe
# so stderr is merged before PowerShell sees it, and check the exit code explicitly.
function Run-Native([string]$cmdline) {
  cmd /c "$cmdline 2>&1"
  if ($LASTEXITCODE -ne 0) { throw "command failed (exit $LASTEXITCODE): $cmdline" }
}

function Wait-Http($url, $seconds = 120) {
  for ($i = 0; $i -lt $seconds; $i++) {
    try { Invoke-WebRequest -UseBasicParsing -Uri $url -TimeoutSec 3 | Out-Null; return } catch { Start-Sleep 1 }
  }
  throw "timed out waiting for $url"
}

Write-Host "[1/5] docker daemon" -ForegroundColor Cyan
cmd /c "docker info >nul 2>&1"
if ($LASTEXITCODE -ne 0) {
  throw "Docker is not running. Start Docker Desktop ($env:LOCALAPPDATA\Programs\DockerDesktop\Docker Desktop.exe), wait for 'docker info' to succeed, then re-run."
}

Write-Host "[2/5] wiping stack (docker compose down -v)" -ForegroundColor Cyan
Push-Location $local
try {
  Run-Native "docker compose down -v"
  Write-Host "[3/5] starting stack" -ForegroundColor Cyan
  Run-Native "docker compose up -d"
} finally { Pop-Location }

Write-Host "[4/5] waiting for Prometheus, Loki, Grafana, Alertmanager" -ForegroundColor Cyan
Wait-Http 'http://localhost:9090/-/ready'
Wait-Http 'http://localhost:3100/ready'
Wait-Http 'http://localhost:3000/api/health'
Wait-Http 'http://localhost:9093/-/ready'

Write-Host "[5/5] seeding demo data $SeedArgs" -ForegroundColor Cyan
$seeder = Join-Path $PSScriptRoot 'seed_demo_data.py'
Run-Native "python `"$seeder`" $SeedArgs"

if ($SlackTest) {
  if (-not (Test-Path (Join-Path $local 'secrets\slack-webhook'))) {
    Write-Warning "local/secrets/slack-webhook missing - Alertmanager will route the test alert but Slack delivery will fail."
  }
  $body = '[{"labels":{"alertname":"DemoTest","owner":"governance","severity":"warning"},"annotations":{"summary":"Demo stack reseeded","description":"Fresh 14-day history loaded; alert routing to Slack verified."}}]'
  Invoke-RestMethod -Method Post -Uri 'http://localhost:9093/api/v2/alerts' -ContentType 'application/json' -Body $body | Out-Null
  Write-Host "Slack test alert posted (arrives within ~30s, auto-resolves in ~5 min)." -ForegroundColor Green
}

Write-Host ""
Write-Host "Done. Grafana  http://localhost:3000  (home = Executive Summary; set range to Last 14 days)" -ForegroundColor Green
Write-Host "      Alerts   http://localhost:9093   Prometheus http://localhost:9090/alerts"
Write-Host "Remember: your own Claude Code sessions land as 'unattributed' unless launched via client/claude-attr."
