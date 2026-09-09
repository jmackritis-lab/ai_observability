#!/usr/bin/env bash
# Launch `claude` with OTEL_RESOURCE_ATTRIBUTES derived from the current git checkout
# (docs/analytics.md §3.3 — attributes are read once at startup, so: one Jira ticket ≈ one session).
#
#   project    = basename of the `origin` remote (falls back to the repo directory name)
#   jira.story = first Jira key (ABC-123) found in the current branch name, else `none`
#   jira.epic  = `git config claude.jiraEpic` (set once per repo), else `none`
#
# Overrides (env): CLAUDE_PROJECT, CLAUDE_JIRA_STORY, CLAUDE_JIRA_EPIC.
# Anything already in OTEL_RESOURCE_ATTRIBUTES is preserved; derived keys are appended.
# NOTE: a repo whose .claude/settings.json sets OTEL_RESOURCE_ATTRIBUTES overrides this wrapper
# entirely (settings env replaces the shell variable) - use .claude/settings.local.json there.
# Values must be US-ASCII with no spaces/quotes/commas/semicolons/backslashes — the collector
# lower-cases `project`, upper-cases Jira keys, and stamps `invalid` on anything malformed.
set -euo pipefail

jira_re='[A-Za-z][A-Za-z0-9]*-[0-9]+'

if git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  origin="$(git remote get-url origin 2>/dev/null || true)"
  project_default="$(basename -s .git "${origin:-$(git rev-parse --show-toplevel)}")"
  branch="$(git rev-parse --abbrev-ref HEAD 2>/dev/null || true)"
  story_default="$(grep -oE "$jira_re" <<<"$branch" | head -n1 || true)"
  epic_default="$(git config --get claude.jiraEpic 2>/dev/null || true)"
else
  project_default="$(basename "$PWD")"
  story_default=""
  epic_default=""
fi

project="${CLAUDE_PROJECT:-$project_default}"
story="${CLAUDE_JIRA_STORY:-${story_default:-none}}"
epic="${CLAUDE_JIRA_EPIC:-${epic_default:-none}}"

attrs="project=${project},jira.epic=${epic},jira.story=${story}"
if [[ -n "${OTEL_RESOURCE_ATTRIBUTES:-}" ]]; then
  attrs="${OTEL_RESOURCE_ATTRIBUTES},${attrs}"
fi

export OTEL_RESOURCE_ATTRIBUTES="$attrs"
[[ -n "${CLAUDE_ATTR_QUIET:-}" ]] || echo "claude-attr: OTEL_RESOURCE_ATTRIBUTES=$attrs" >&2
exec claude "$@"
