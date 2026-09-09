<#
.SYNOPSIS
  Launch `claude` with OTEL_RESOURCE_ATTRIBUTES derived from the current git checkout.
  PowerShell twin of claude-attr.sh — see that file's header for the derivation rules.

  Overrides (env): CLAUDE_PROJECT, CLAUDE_JIRA_STORY, CLAUDE_JIRA_EPIC.
  Tip: put this on PATH and `Set-Alias claude-attr claude-attr.ps1`, or set
  `git config claude.jiraEpic EPMDEDP-15000` once per repo so the epic is always filled.
#>
$ErrorActionPreference = 'Stop'
$jiraRe = '[A-Za-z][A-Za-z0-9]*-[0-9]+'

$projectDefault = Split-Path -Leaf (Get-Location)
$storyDefault = ''
$epicDefault = ''

git rev-parse --is-inside-work-tree *> $null
if ($LASTEXITCODE -eq 0) {
  $origin = (git remote get-url origin 2>$null)
  if (-not $origin) { $origin = (git rev-parse --show-toplevel) }
  $projectDefault = [IO.Path]::GetFileName($origin.TrimEnd('/')) -replace '\.git$', ''
  $branch = (git rev-parse --abbrev-ref HEAD 2>$null)
  $m = [regex]::Match([string]$branch, $jiraRe)
  if ($m.Success) { $storyDefault = $m.Value }
  $epicDefault = (git config --get claude.jiraEpic 2>$null)
}

$project = if ($env:CLAUDE_PROJECT) { $env:CLAUDE_PROJECT } else { $projectDefault }
$story   = if ($env:CLAUDE_JIRA_STORY) { $env:CLAUDE_JIRA_STORY } elseif ($storyDefault) { $storyDefault } else { 'none' }
$epic    = if ($env:CLAUDE_JIRA_EPIC) { $env:CLAUDE_JIRA_EPIC } elseif ($epicDefault) { $epicDefault } else { 'none' }

$attrs = "project=$project,jira.epic=$epic,jira.story=$story"
if ($env:OTEL_RESOURCE_ATTRIBUTES) { $attrs = "$($env:OTEL_RESOURCE_ATTRIBUTES),$attrs" }

# Scope the env change to the claude child process: $env: writes are process-wide in
# PowerShell and would otherwise leak into the caller's session (and stack up on the next run).
$prev = $env:OTEL_RESOURCE_ATTRIBUTES
try {
  $env:OTEL_RESOURCE_ATTRIBUTES = $attrs
  if (-not $env:CLAUDE_ATTR_QUIET) { Write-Host "claude-attr: OTEL_RESOURCE_ATTRIBUTES=$attrs" -ForegroundColor DarkGray }
  & claude @args
  $code = $LASTEXITCODE
} finally {
  if ($null -eq $prev) { Remove-Item Env:OTEL_RESOURCE_ATTRIBUTES -ErrorAction SilentlyContinue } else { $env:OTEL_RESOURCE_ATTRIBUTES = $prev }
}
exit $code
