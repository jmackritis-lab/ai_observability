#!/usr/bin/env python3
"""
Seed the LOCAL docker-compose stack with two weeks of realistic, correlated demo data so every
dashboard filter (project -> jira.epic -> jira.story -> user) has something to show.

What it produces (stdlib only, no pip installs):
  * Prometheus: cumulative counter series for all 8 Claude Code metrics, one label set per
    synthetic session, mirroring the exact labels real traffic gets after the collector
    (collector_env, job/instance, otel_scope_*, user_*, project/jira_*, model/type/effort/...).
    History cannot be pushed through the collector (the Prometheus exporter only serves "now"),
    so the samples are written as OpenMetrics text and imported with
    `promtool tsdb create-blocks-from openmetrics` into the prometheus-data volume.
  * Loki: the matching events (api_request, tool_result, tool_decision, user_prompt,
    mcp_server_connection, plugin_loaded, hook_execution_complete, skill_activated,
    permission_mode_changed, api_error, api_retries_exhausted, api_refusal, compaction, auth)
    posted as OTLP/HTTP JSON straight to Loki's OTLP endpoint (:3100/otlp) in strict time order,
    with the collector's sentinel rules applied client-side (--via collector sends through the
    collector instead, but its concurrent exporter queue can reorder back-dated batches).
    Every demo record carries resource attribute service.namespace=demo -> Loki label
    `service_namespace="demo"` so it can be told apart from (and deleted separately to) real data.

Metric totals are DERIVED from the generated api_request events, so a session's cost in Session
Explorer (Loki) matches its cost in Usage Audit (Prometheus).

Usage (from repo root, stack running):
  python local/demo/seed_demo_data.py                 # 14 days, default roster
  python local/demo/seed_demo_data.py --days 21 --seed 7
  python local/demo/seed_demo_data.py --skip-prometheus | --skip-loki | --skip-rules
  python local/demo/seed_demo_data.py --dry-run       # generate + write files only

Reset: `cd local && docker compose down -v` wipes everything (real + demo). To drop only demo
logs: curl -X POST -G localhost:3100/loki/api/v1/delete --data-urlencode 'query={service_namespace="demo"}' \
      --data-urlencode 'start=2020-01-01T00:00:00Z'
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
LOCAL_DIR = os.path.dirname(HERE)
OUT_DIR = os.path.join(HERE, "out")
PROM_IMAGE = "prom/prometheus:v3.13.1"
COLLECTOR_HTTP = os.environ.get("DEMO_OTLP_HTTP", "http://localhost:4318")
PROM_URL = os.environ.get("DEMO_PROM_URL", "http://localhost:9090")
LOKI_URL = os.environ.get("DEMO_LOKI_URL", "http://localhost:3100")

# --------------------------------------------------------------------------------------------
# Roster — edit freely. Keep values US-ASCII, no spaces/commas.
# --------------------------------------------------------------------------------------------
ORG_ID = "5a6059f3-b473-4385-b842-d06e8562c391"

USERS = [
    # email, persona: (main model, effort, sessions/weekday, terminal, version)
    ("ana.ruiz@example.com",     "claude-sonnet-5", "medium", 2.2, "vscode",     "2.1.260"),
    ("ben.okafor@example.com",   "claude-opus-5",   "high",   1.6, "iTerm.app",  "2.1.260"),
    ("chloe.martin@example.com", "claude-sonnet-5", "medium", 1.9, "WarpTerminal","2.1.259"),
    ("diego.santos@example.com", "claude-sonnet-5", "high",   1.2, "vscode",     "2.1.259"),
    ("emma.li@example.com",      "claude-fable-5-1","high",   0.9, "vscode",     "2.1.260"),
    ("farid.hassan@example.com", "claude-sonnet-5", "medium", 1.4, "tmux",       "2.1.255"),
    ("grace.kim@example.com",    "claude-opus-5",   "medium", 1.1, "cursor",     "2.1.260"),
    ("hugo.weber@example.com",   "claude-sonnet-5", "low",    0.7, "Windows Terminal","2.1.259"),
]

# project -> {epic: [stories]}
PROJECTS = {
    "krci-portal": {
        "EPMDEDP-15000": ["EPMDEDP-15147", "EPMDEDP-15151", "EPMDEDP-15160", "EPMDEDP-15172", "EPMDEDP-15188"],
        "EPMDEDP-15200": ["EPMDEDP-15203", "EPMDEDP-15210", "EPMDEDP-15224"],
    },
    "edp-cluster-add-ons": {
        "EPMDEDP-16100": ["EPMDEDP-16102", "EPMDEDP-16115", "EPMDEDP-16131", "EPMDEDP-16140"],
    },
    "claude-code-telemetry": {
        "EPMDEDP-17184": ["EPMDEDP-17185", "EPMDEDP-17190", "EPMDEDP-17201"],
    },
    "krci-operator": {
        "EPMDEDP-14800": ["EPMDEDP-14811", "EPMDEDP-14822", "EPMDEDP-14830", "EPMDEDP-14845"],
        "EPMDEDP-14900": ["EPMDEDP-14903", "EPMDEDP-14917"],
    },
}
# Which projects each user mostly works in (weights).
USER_PROJECTS = {
    "ana.ruiz@example.com":     {"krci-portal": 0.8, "claude-code-telemetry": 0.2},
    "ben.okafor@example.com":   {"krci-operator": 0.7, "edp-cluster-add-ons": 0.3},
    "chloe.martin@example.com": {"krci-portal": 0.6, "krci-operator": 0.4},
    "diego.santos@example.com": {"edp-cluster-add-ons": 0.9, "krci-operator": 0.1},
    "emma.li@example.com":      {"claude-code-telemetry": 0.7, "krci-portal": 0.3},
    "farid.hassan@example.com": {"krci-portal": 0.5, "edp-cluster-add-ons": 0.5},
    "grace.kim@example.com":    {"krci-operator": 0.8, "claude-code-telemetry": 0.2},
    "hugo.weber@example.com":   {"krci-portal": 0.4, "edp-cluster-add-ons": 0.6},
}
P_STORY_NONE = 0.10        # explicit `none` story (exploratory work)
P_UNATTRIBUTED = 0.06      # session launched without the wrapper -> collector stamps `unattributed`
P_INVALID_STORY = 0.02     # typo'd Jira key -> collector stamps `invalid`

AUX_MODEL = "claude-haiku-4-5-20251001"
# USD per token (input, output, cacheRead, cacheCreation) — plausible list prices for the demo.
PRICE = {
    "claude-sonnet-5":            (3e-6,  15e-6,  0.3e-6,  3.75e-6),
    "claude-opus-5":              (15e-6, 75e-6,  1.5e-6,  18.75e-6),
    "claude-fable-5-1":           (25e-6, 125e-6, 2.5e-6,  31.25e-6),
    "claude-haiku-4-5-20251001":  (1e-6,  5e-6,   0.1e-6,  1.25e-6),
}
TOOLS = [("Read", 0.24), ("Bash", 0.20), ("Edit", 0.16), ("Grep", 0.12), ("Glob", 0.07),
         ("Write", 0.06), ("Agent", 0.05), ("WebFetch", 0.03), ("mcp__atlassian__getJiraIssue", 0.04),
         ("mcp__github__create_pull_request", 0.02), ("Skill", 0.01)]
LANGUAGES = [("TypeScript", 0.35), ("Go", 0.2), ("YAML", 0.15), ("Python", 0.12), ("Markdown", 0.1), ("JSON", 0.05), ("unknown", 0.03)]
SKILLS = [("commit", "bundled"), ("review-pr", "bundled"), ("helm-lint", "projectSettings"), ("krci-release", "plugin"), ("code-review", "bundled")]
PLUGINS = [("krci-tools", "1.4.2", "epam-marketplace", "org", "org-policy"),
           ("hypr", "0.9.1", "hypr-marketplace", "user-local", "user-install"),
           ("claude-code-guide", "2.1.260", "anthropic", "official", "default-enable")]
MCP_SERVERS = [("atlassian", "http", "user"), ("github", "stdio", "project"), ("grain", "http", "user")]
COMMANDS = [("commit", "builtin"), ("review", "builtin"), ("compact", "builtin"), ("krci:release", "custom"), ("plan", "builtin")]


def wchoice(rng: random.Random, pairs):
    items, weights = zip(*pairs)
    return rng.choices(items, weights=weights, k=1)[0]


def stable_hash(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()


@dataclass
class Session:
    sid: str
    user: str
    persona: tuple
    start: datetime
    end: datetime
    project: str
    epic: str
    story: str
    model: str
    effort: str
    start_type: str
    events: list = field(default_factory=list)          # (ts, event_name, attrs dict)
    # metric increments: (ts, metric, labels-tuple, delta)
    increments: list = field(default_factory=list)

    @property
    def resource(self) -> dict:
        email = self.user
        uid = stable_hash(email)
        return {
            "service.name": "claude-code",
            "service.namespace": "demo",
            "service.version": self.persona[5],
            "os.type": "linux" if "tmux" in self.persona[4] or "iTerm" in self.persona[4] else "windows",
            "os.version": "6.8.0" if "tmux" in self.persona[4] else "10.0.26200",
            "host.arch": "amd64",
            "organization.id": ORG_ID,
            "user.id": uid,
            "user.email": email,
            "user.account_id": "user_" + uid[:26].upper(),
            "user.account_uuid": str(uuid.UUID(uid[:32])),
            "session.id": self.sid,
            "terminal.type": self.persona[4],
            "project": self.project,
            "jira.epic": self.epic,
            "jira.story": self.story,
        }


def pick_attribution(rng: random.Random, user: str):
    r = rng.random()
    if r < P_UNATTRIBUTED:
        return ("__missing__", "__missing__", "__missing__")
    project = wchoice(rng, list(USER_PROJECTS[user].items()))
    epic = rng.choice(list(PROJECTS[project].keys()))
    r = rng.random()
    if r < P_STORY_NONE:
        story = "none"
    elif r < P_STORY_NONE + P_INVALID_STORY:
        story = rng.choice(["EPMDEDP_17190", "epmdedp-", "TBD", "story-42"])
    else:
        story = rng.choice(PROJECTS[project][epic])
    return project, epic, story


def gen_session(rng: random.Random, user: str, persona: tuple, start: datetime) -> Session:
    model, effort = persona[1], persona[2]
    minutes = int(rng.triangular(15, 150, 55))
    end = start + timedelta(minutes=minutes)
    project, epic, story = pick_attribution(rng, user)
    s = Session(sid=str(uuid.UUID(int=rng.getrandbits(128))), user=user, persona=persona, start=start, end=end,
                project=project, epic=epic, story=story, model=model, effort=effort,
                start_type=wchoice(rng, [("fresh", 0.7), ("resume", 0.2), ("continue", 0.1)]))
    t = start
    ev = s.events.append
    inc = s.increments.append
    inc((t, "session.count", (("start_type", s.start_type),), 1))

    # startup: plugins + MCP connections
    for name, ver, mkt, scope, via in PLUGINS:
        if rng.random() < 0.85:
            ev((t, "plugin_loaded", {"plugin.name": name, "plugin.version": ver, "marketplace.name": mkt,
                                     "plugin.scope": scope, "enabled_via": via, "has_hooks": "true" if name == "krci-tools" else "false",
                                     "has_mcp": "true" if name != "claude-code-guide" else "false"}))
    for name, transport, scope in MCP_SERVERS:
        if rng.random() < 0.8:
            failed = rng.random() < 0.07
            ev((t + timedelta(seconds=rng.randint(1, 4)), "mcp_server_connection",
                {"server_name": name, "status": "failed" if failed else "connected", "transport_type": transport,
                 "server_scope": scope, "duration_ms": rng.randint(120, 4000), "is_plugin": "false",
                 **({"error_code": "ECONNREFUSED"} if failed else {})}))
    if rng.random() < 0.04:
        ev((t, "auth", {"action": "login", "success": "false", "auth_method": "oauth", "error_category": "token_expired", "status_code": "401"}))
        ev((t + timedelta(seconds=20), "auth", {"action": "login", "success": "true", "auth_method": "oauth"}))
    if rng.random() < 0.08:
        to_mode = wchoice(rng, [("acceptEdits", 0.5), ("plan", 0.3), ("bypassPermissions", 0.2)])
        ev((t + timedelta(seconds=30), "permission_mode_changed", {"from_mode": "default", "to_mode": to_mode, "trigger": "shift_tab"}))

    # turns
    n_prompts = max(2, int(minutes / rng.uniform(5, 12)))
    human_active = 0.0
    cli_active = 0.0
    commits = prs = 0
    for _ in range(n_prompts):
        gap = rng.uniform(20, 240)
        t += timedelta(seconds=gap)
        if t >= end:
            break
        human_active += min(gap, 120)
        pid = str(uuid.UUID(int=rng.getrandbits(128)))
        cmd = None
        if rng.random() < 0.18:
            cmd = rng.choice(COMMANDS)
        ev((t, "user_prompt", {"prompt_length": rng.randint(20, 900), "prompt": "<REDACTED>", "prompt.id": pid,
                               **({"command_name": cmd[0], "command_source": cmd[1]} if cmd else {})}))
        if cmd and cmd[0] == "compact":
            ev((t + timedelta(seconds=3), "compaction", {"trigger": "manual", "success": "true", "duration_ms": rng.randint(800, 5000),
                                                         "pre_tokens": rng.randint(120000, 190000), "post_tokens": rng.randint(20000, 60000)}))
        if rng.random() < 0.12:
            sk = rng.choice(SKILLS)
            ev((t + timedelta(seconds=1), "skill_activated", {"skill.name": sk[0], "skill.source": sk[1],
                                                              "invocation_trigger": "user-slash" if cmd else "claude-proactive"}))
        # hooks on prompt submit
        if rng.random() < 0.6:
            blocking = 1 if rng.random() < 0.05 else 0
            ev((t + timedelta(seconds=1), "hook_execution_complete", {"hook_event": "UserPromptSubmit", "hook_name": "UserPromptSubmit",
                "num_hooks": 1, "num_success": 1 - blocking, "num_blocking": blocking, "num_non_blocking_error": 0, "num_cancelled": 0,
                "total_duration_ms": rng.randint(30, 900), "hook_source": "merged", "managed_only": "false", "safe_mode": "false"}))
        n_calls = rng.randint(1, 5)
        for c in range(n_calls):
            # main API request
            dur = rng.randint(2500, 45000)
            inp = rng.randint(2, 400)
            out = rng.randint(80, 2500)
            cr = rng.randint(20000, 160000)
            cc = rng.randint(200, 6000) if rng.random() < 0.7 else 0
            t += timedelta(milliseconds=dur)
            cli_active += dur / 1000
            if t >= end:
                break
            failed = rng.random() < 0.025
            if failed:
                code = wchoice(rng, [("529", 0.5), ("500", 0.2), ("429", 0.3)])
                ev((t, "api_error", {"model": s.model, "error": "overloaded_error" if code == "529" else "api_error", "status_code": code,
                                     "duration_ms": dur, "attempt": 3, "query_source": "repl_main_thread", "speed": "normal", "effort": effort}))
                if rng.random() < 0.3:
                    ev((t + timedelta(milliseconds=10), "api_retries_exhausted", {"model": s.model, "attempt": 3, "status_code": code,
                                                                                  "query_source": "repl_main_thread"}))
                continue
            price = PRICE[s.model]
            cost = inp * price[0] + out * price[1] + cr * price[2] + cc * price[3]
            ev((t, "api_request", {"model": s.model, "cost_usd": round(cost, 6), "cost_usd_micros": int(cost * 1e6), "duration_ms": dur,
                                   "input_tokens": inp, "output_tokens": out, "cache_read_tokens": cr, "cache_creation_tokens": cc,
                                   "request_id": "req_demo" + uuid.uuid4().hex[:18], "speed": "normal", "query_source": "repl_main_thread",
                                   "effort": effort, "prompt.id": pid}))
            base = (("model", s.model), ("query_source", "main"), ("effort", effort))
            for typ, n in (("input", inp), ("output", out), ("cacheRead", cr), ("cacheCreation", cc)):
                if n:
                    inc((t, "token.usage", base + (("type", typ),), n))
            inc((t, "cost.usage", base, cost))
            if rng.random() < 0.03:
                ev((t + timedelta(milliseconds=5), "api_refusal", {"model": s.model, "query_source": "repl_main_thread", "attempt": 1,
                    "has_category": "true", "category": wchoice(rng, [("cyber", 0.6), ("frontier_llm", 0.3), ("bio", 0.1)]), "effort": effort}))

            # auxiliary haiku call (title/summary) — cheap
            if rng.random() < 0.35:
                ainp, aout = rng.randint(300, 1500), rng.randint(10, 60)
                ap = PRICE[AUX_MODEL]
                acost = ainp * ap[0] + aout * ap[1]
                ev((t + timedelta(milliseconds=300), "api_request", {"model": AUX_MODEL, "cost_usd": round(acost, 6), "duration_ms": rng.randint(400, 1800),
                    "input_tokens": ainp, "output_tokens": aout, "cache_read_tokens": 0, "cache_creation_tokens": 0,
                    "request_id": "req_demo" + uuid.uuid4().hex[:18], "speed": "normal", "query_source": "auxiliary", "prompt.id": pid}))
                ab = (("model", AUX_MODEL), ("query_source", "auxiliary"))
                inc((t, "token.usage", ab + (("type", "input"),), ainp))
                inc((t, "token.usage", ab + (("type", "output"),), aout))
                inc((t, "cost.usage", ab, acost))

            # tool calls
            for _ in range(rng.randint(0, 4)):
                tool = wchoice(rng, TOOLS)
                t += timedelta(milliseconds=rng.randint(200, 2500))
                reject = rng.random() < (0.06 if tool in ("Bash", "Write", "Edit") else 0.01)
                src = wchoice(rng, [("config", 0.55), ("user_permanent", 0.2), ("user_temporary", 0.15), ("hook", 0.05), ("user_reject", 0.05)]) if not reject else wchoice(rng, [("user_reject", 0.7), ("hook", 0.2), ("user_abort", 0.1)])
                params = {}
                if tool == "Bash":
                    danger = rng.random() < 0.03
                    params = {"bash_command": rng.choice(["git", "helm", "kubectl", "npm", "go"]), "full_command": "<redacted-for-demo>",
                              "description": "demo", **({"dangerouslyDisableSandbox": True} if danger else {})}
                elif tool.startswith("mcp__"):
                    _, srv, name = tool.split("__", 2)
                    params = {"mcp_server_name": srv, "mcp_tool_name": name}
                elif tool == "Agent":
                    params = {"subagent_type": rng.choice(["Explore", "Plan", "general-purpose"])}
                elif tool == "Skill":
                    params = {"skill_name": rng.choice(SKILLS)[0]}
                dec = {"tool_name": tool, "tool_use_id": "toolu_demo" + uuid.uuid4().hex[:16], "decision": "reject" if reject else "accept",
                       "source": src, "tool_source": "mcp" if tool.startswith("mcp__") else "builtin", "prompt.id": pid}
                if params:
                    dec["tool_parameters"] = json.dumps(params)
                ev((t, "tool_decision", dec))
                if tool in ("Edit", "Write"):
                    lang = wchoice(rng, LANGUAGES)
                    inc((t, "code_edit_tool.decision", (("tool_name", tool), ("decision", "reject" if reject else "accept"), ("source", src), ("language", lang)), 1))
                    if not reject:
                        added, removed = rng.randint(2, 80), rng.randint(0, 40)
                        inc((t, "lines_of_code.count", (("type", "added"), ("model", s.model)), added))
                        if removed:
                            inc((t, "lines_of_code.count", (("type", "removed"), ("model", s.model)), removed))
                if reject:
                    continue
                dur_t = rng.randint(50, 12000) if tool != "Agent" else rng.randint(20000, 180000)
                t += timedelta(milliseconds=dur_t)
                ok = rng.random() > (0.08 if tool == "Bash" else 0.03)
                res = {"tool_name": tool, "tool_use_id": dec["tool_use_id"], "success": "true" if ok else "false", "duration_ms": dur_t,
                       "decision_type": "accept", "decision_source": src, "tool_input_size_bytes": rng.randint(40, 2000),
                       "tool_result_size_bytes": rng.randint(0, 20000), "prompt.id": pid}
                if params:
                    res["tool_parameters"] = json.dumps(params)
                if tool.startswith("mcp__"):
                    res["mcp_server_scope"] = "user"
                if not ok:
                    res["error_type"] = rng.choice(["ShellError", "Error:ENOENT", "TimeoutError", "Error:EACCES"])
                ev((t, "tool_result", res))
                if tool == "Agent":
                    agent = params["subagent_type"]
                    for _ in range(rng.randint(2, 6)):
                        sinp, sout, scr = rng.randint(2, 200), rng.randint(60, 1200), rng.randint(8000, 60000)
                        sp = PRICE[s.model]
                        scost = sinp * sp[0] + sout * sp[1] + scr * sp[2]
                        ts = t - timedelta(milliseconds=rng.randint(0, dur_t))
                        ev((ts, "api_request", {"model": s.model, "cost_usd": round(scost, 6), "duration_ms": rng.randint(1500, 15000),
                            "input_tokens": sinp, "output_tokens": sout, "cache_read_tokens": scr, "cache_creation_tokens": 0,
                            "request_id": "req_demo" + uuid.uuid4().hex[:18], "speed": "normal", "query_source": agent, "agent.name": agent,
                            "effort": effort, "prompt.id": pid}))
                        sb = (("model", s.model), ("query_source", "subagent"), ("agent.name", agent), ("effort", effort))
                        inc((ts, "token.usage", sb + (("type", "input"),), sinp))
                        inc((ts, "token.usage", sb + (("type", "output"),), sout))
                        inc((ts, "token.usage", sb + (("type", "cacheRead"),), scr))
                        inc((ts, "cost.usage", sb, scost))
                if tool == "Bash" and params.get("bash_command") == "git" and rng.random() < 0.5:
                    commits += 1
                    inc((t, "commit.count", (), 1))
                if tool.startswith("mcp__github__create_pull_request") or (tool == "Bash" and rng.random() < 0.04):
                    prs += 1
                    inc((t, "pull_request.count", (), 1))
        if rng.random() < 0.15:
            ev((t, "compaction", {"trigger": "auto", "success": "true", "duration_ms": rng.randint(800, 5000),
                                  "pre_tokens": rng.randint(150000, 195000), "post_tokens": rng.randint(20000, 60000)}))
        if rng.random() < 0.5:
            ev((t, "hook_execution_complete", {"hook_event": "Stop", "hook_name": "Stop", "num_hooks": 1, "num_success": 1, "num_blocking": 0,
                "num_non_blocking_error": 0, "num_cancelled": 0, "total_duration_ms": rng.randint(200, 2500), "hook_source": "merged",
                "managed_only": "false", "safe_mode": "false"}))
    s.end = min(s.end, t + timedelta(seconds=30))
    # active time: spread as increments at the end of each minute (good enough for rate panels)
    total_min = max(1, int((s.end - s.start).total_seconds() // 60))
    for i in range(total_min):
        ts = s.start + timedelta(minutes=i + 1)
        inc((ts, "active_time.total", (("type", "user"),), human_active / total_min))
        inc((ts, "active_time.total", (("type", "cli"),), cli_active / total_min))
    return s


def gen_sessions(rng: random.Random, days: int, now: datetime) -> list[Session]:
    sessions = []
    day0 = (now - timedelta(days=days)).replace(hour=0, minute=0, second=0, microsecond=0)
    for d in range(days + 1):
        day = day0 + timedelta(days=d)
        weekend = day.weekday() >= 5
        for user, *persona in USERS:
            rate = persona[2] * (0.15 if weekend else 1.0)
            n = 0
            # poisson-ish
            L, k, p = pow(2.718281828, -rate), 0, 1.0
            while True:
                k += 1
                p *= rng.random()
                if p <= L:
                    n = k - 1
                    break
            for _ in range(n):
                hour = rng.triangular(8, 19, 11 if rng.random() < 0.6 else 15)
                start = day + timedelta(hours=hour, minutes=rng.randint(0, 59))
                if start >= now - timedelta(minutes=15):
                    continue
                sessions.append(gen_session(rng, user, (user, *persona), start))
    sessions.sort(key=lambda s: s.start)
    return sessions


# --------------------------------------------------------------------------------------------
# Prometheus: OpenMetrics text -> promtool backfill
# --------------------------------------------------------------------------------------------
METRIC_FAMILIES = {
    # otel name -> (prometheus family name, sample suffix)
    "session.count": "claude_code_session_count",
    "lines_of_code.count": "claude_code_lines_of_code_count",
    "pull_request.count": "claude_code_pull_request_count",
    "commit.count": "claude_code_commit_count",
    "cost.usage": "claude_code_cost_usage_USD",
    "token.usage": "claude_code_token_usage_tokens",
    "code_edit_tool.decision": "claude_code_code_edit_tool_decision",
    "active_time.total": "claude_code_active_time_seconds",
}
STEP = 60  # seconds between samples


def esc(v) -> str:
    return str(v).replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def enforce_attribution(proj: str, epic: str, story: str):
    """Emulate the collector's transform/attribution processor for data that bypasses the collector."""
    import re
    if proj == "__missing__":
        return "unattributed", "unattributed", "unattributed"
    proj = proj.lower()
    if not re.match(r"^[a-z0-9._-]+$", proj):
        proj = "invalid"
    jira = r"^[A-Za-z][A-Za-z0-9]*-[0-9]+$"
    epic = epic.upper() if re.match(jira, epic) else (epic if epic in ("none", "unattributed") else "invalid")
    story = story.upper() if re.match(jira, story) else (story if story in ("none", "unattributed") else "invalid")
    return proj, epic, story


def prom_labels_for(s: Session) -> dict:
    r = s.resource
    proj, epic, story = enforce_attribution(s.project, s.epic, s.story)
    return {
        "collector_env": "demo", "exported_job": "claude-code", "instance": "otel-collector:8889", "job": "claude-code",
        "otel_scope_name": "com.anthropic.claude_code", "otel_scope_version": r["service.version"],
        "service_name": "claude-code", "service_namespace": "demo", "service_version": r["service.version"],
        "host_arch": r["host.arch"], "os_type": r["os.type"], "os_version": r["os.version"],
        "organization_id": ORG_ID, "user_id": r["user.id"], "user_email": r["user.email"],
        "user_account_id": r["user.account_id"], "user_account_uuid": r["user.account_uuid"],
        "session_id": s.sid, "terminal_type": r["terminal.type"],
        "project": proj, "jira_epic": epic, "jira_story": story,
    }


def write_openmetrics(sessions: list[Session], path: str) -> int:
    # family -> series-label-string -> sorted list of (ts, cumulative)
    fam_series: dict[str, dict[str, list]] = defaultdict(dict)
    total = 0
    for s in sessions:
        base = prom_labels_for(s)
        per_series: dict[tuple, list] = defaultdict(list)
        for ts, metric, extra, delta in s.increments:
            per_series[(metric, extra)].append((ts, delta))
        for (metric, extra), incs in per_series.items():
            incs.sort()
            labels = dict(base)
            for k, v in extra:
                labels[k.replace(".", "_")] = v
            lstr = ",".join(f'{k}="{esc(v)}"' for k, v in sorted(labels.items()))
            # sample on a STEP grid from session start to end (+1 step), cumulative
            samples = []
            cum = 0.0
            i = 0
            t = s.start.timestamp()
            end = s.end.timestamp() + STEP
            while t <= end:
                while i < len(incs) and incs[i][0].timestamp() <= t:
                    cum += incs[i][1]
                    i += 1
                samples.append((t, cum))
                t += STEP
            fam_series[METRIC_FAMILIES[metric]][lstr] = samples
            total += len(samples)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        for fam, series in fam_series.items():
            f.write(f"# TYPE {fam} counter\n")
            for lstr, samples in series.items():
                for t, v in samples:
                    val = f"{v:.6f}" if fam.endswith("USD") or fam.endswith("seconds") else str(int(v))
                    f.write(f"{fam}_total{{{lstr}}} {val} {t:.3f}\n")
        f.write("# EOF\n")
    return total


def docker(*args, check=True, capture=False):
    cmd = ["docker", *args]
    return subprocess.run(cmd, check=check, text=True, capture_output=capture)


def compose(*args, check=True):
    return subprocess.run(["docker", "compose", *args], cwd=LOCAL_DIR, check=check, text=True)


def find_volume(suffix: str) -> str:
    out = docker("volume", "ls", "-q", "--filter", f"name={suffix}", capture=True).stdout.split()
    if not out:
        sys.exit(f"volume matching {suffix} not found — is the compose stack created?")
    return out[0]


def wait_http(url: str, timeout=90):
    for _ in range(timeout):
        try:
            urllib.request.urlopen(url, timeout=2).read()
            return
        except Exception:
            time.sleep(1)
    sys.exit(f"timed out waiting for {url}")


def prom_data_dir() -> str:
    """Resolve the TSDB directory *inside the volume mount* (/prometheus). The compose file overrides the
    image CMD, so Prometheus falls back to its default relative path `data/` -> /prometheus/data."""
    try:
        with urllib.request.urlopen(f"{PROM_URL}/api/v1/status/flags", timeout=5) as r:
            path = json.load(r)["data"].get("storage.tsdb.path", "data/")
    except Exception:
        path = "data/"
    path = path.rstrip("/") or "/prometheus"
    if not path.startswith("/"):
        path = "/prometheus/" + path          # relative to the image WORKDIR
    if not path.startswith("/prometheus"):
        sys.exit(f"unexpected storage.tsdb.path {path!r}; expected it under the /prometheus volume")
    return path


def import_prometheus(om_path: str, data_dir: str):
    vol = find_volume("prometheus-data")
    host_dir = os.path.abspath(OUT_DIR)
    print(f"[prometheus] stopping server, importing blocks into volume {vol} at {data_dir} ...")
    compose("stop", "prometheus")
    docker("run", "--rm", "-v", f"{vol}:/prometheus", "-v", f"{host_dir}:/import:ro", "--entrypoint", "promtool", PROM_IMAGE,
           "tsdb", "create-blocks-from", "openmetrics", "--max-block-duration=24h", f"/import/{os.path.basename(om_path)}", data_dir)
    compose("start", "prometheus")
    wait_http(f"{PROM_URL}/-/ready")
    print("[prometheus] import done and server ready")


def backfill_rules(start: datetime, end: datetime, data_dir: str, eval_interval: str = "10m"):
    """Evaluate local/prometheus-rules.yml over the seeded window so recording-rule series exist for the past too."""
    vol = find_volume("prometheus-data")
    host_dir = os.path.abspath(OUT_DIR)
    rules_dir = os.path.abspath(LOCAL_DIR)
    net = docker("network", "ls", "-q", "--filter", "name=claude-code-telemetry_default", capture=True).stdout.split()
    if not net:
        print("[rules] compose network not found, skipping rules backfill")
        return
    out_sub = "rules-blocks"
    import shutil
    shutil.rmtree(os.path.join(host_dir, out_sub), ignore_errors=True)
    os.makedirs(os.path.join(host_dir, out_sub), exist_ok=True)
    print(f"[rules] evaluating recording rules over the seeded window every {eval_interval} (coarser than live 1m: "
          "each evaluation is a query against the running Prometheus, 1m over 14 days would be >100k queries) ...")
    docker("run", "--rm", "--network", net[0], "-v", f"{host_dir}:/out", "-v", f"{rules_dir}/prometheus-rules.yml:/rules.yml:ro",
           "--entrypoint", "promtool", PROM_IMAGE, "tsdb", "create-blocks-from", "rules", "--url", "http://prometheus:9090",
           f"--start={start.strftime('%Y-%m-%dT%H:%M:%SZ')}", f"--end={end.strftime('%Y-%m-%dT%H:%M:%SZ')}", f"--eval-interval={eval_interval}",
           f"--output-dir=/out/{out_sub}", "/rules.yml")
    compose("stop", "prometheus")
    # copy as the prometheus user (nobody, uid 65534) so the server can compact/delete them later
    docker("run", "--rm", "-v", f"{vol}:/prometheus", "-v", f"{host_dir}/{out_sub}:/blocks:ro", "alpine:3.20",
           "sh", "-c", f"cp -r /blocks/. {data_dir}/ && chown -R 65534:65534 {data_dir} && ls {data_dir} | wc -l")
    compose("start", "prometheus")
    wait_http(f"{PROM_URL}/-/ready")
    print("[rules] backfilled rule blocks installed")


# --------------------------------------------------------------------------------------------
# Loki: OTLP/HTTP JSON logs via the collector
# --------------------------------------------------------------------------------------------
def any_value(v):
    if isinstance(v, bool):
        return {"boolValue": v}
    if isinstance(v, int):
        return {"intValue": str(v)}
    if isinstance(v, float):
        return {"doubleValue": v}
    return {"stringValue": str(v)}


def kv(d: dict):
    return [{"key": k, "value": any_value(v)} for k, v in d.items()]


def build_log_records(sessions: list[Session], via: str):
    recs = []
    for s in sessions:
        res = dict(s.resource)
        if via == "loki":
            # Bypassing the collector: apply its sentinel rules + env tag ourselves.
            res["project"], res["jira.epic"], res["jira.story"] = enforce_attribution(s.project, s.epic, s.story)
            res["collector.env"] = "demo"
        elif s.project == "__missing__":
            for k in ("project", "jira.epic", "jira.story"):
                res.pop(k, None)
        seq = 0
        for ts, name, attrs in sorted(s.events, key=lambda e: e[0]):
            seq += 1
            # Loki drops entries that share timestamp AND body within a stream (e.g. three
            # plugin_loaded at session start) — make every event's timestamp unique per session.
            ts = ts + timedelta(microseconds=seq)
            a = {"event.name": name, "event.sequence": seq, "event.timestamp": ts.strftime("%Y-%m-%dT%H:%M:%S.") + f"{ts.microsecond // 1000:03d}Z"}
            a.update(attrs)
            recs.append((ts, res, {
                "timeUnixNano": str(int(ts.timestamp() * 1e9)),
                "observedTimeUnixNano": str(int(ts.timestamp() * 1e9)),
                "severityNumber": 9, "severityText": "INFO",
                "body": {"stringValue": f"claude_code.{name}"},
                "attributes": kv(a),
            }, s.persona[5]))
    recs.sort(key=lambda r: r[0])   # global time order -> no out-of-order rejections in Loki
    return recs


def post_json(url: str, payload: dict, retries=5):
    data = json.dumps(payload).encode()
    for attempt in range(retries):
        req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                r.read()
            return
        except urllib.error.HTTPError as e:
            body = e.read().decode(errors="replace")[:300]
            if e.code in (429, 503) and attempt < retries - 1:
                time.sleep(1.5 * (attempt + 1))
                continue
            sys.exit(f"OTLP post failed {e.code}: {body}")
        except urllib.error.URLError as e:
            if attempt < retries - 1:
                time.sleep(1.5)
                continue
            sys.exit(f"OTLP post failed: {e}")


def send_logs(recs, via: str, batch=250):
    # Default is Loki's own OTLP endpoint: one writer, strict time order, so back-dated entries are
    # never rejected as out-of-order. Going through the collector is possible (--via collector) but
    # its exporter queue sends batches concurrently, which can reorder hours of history.
    url = f"{LOKI_URL}/otlp/v1/logs" if via == "loki" else f"{COLLECTOR_HTTP}/v1/logs"
    sent = 0
    for i in range(0, len(recs), batch):
        chunk = recs[i:i + batch]
        # one resourceLogs entry per record keeps the global ordering intact
        payload = {"resourceLogs": [
            {"resource": {"attributes": kv(res)},
             "scopeLogs": [{"scope": {"name": "com.anthropic.claude_code.events", "version": ver}, "logRecords": [rec]}]}
            for _, res, rec, ver in chunk]}
        post_json(url, payload)
        sent += len(chunk)
        if sent % 3000 < batch:
            print(f"[loki] {sent}/{len(recs)} events sent")
        time.sleep(0.15)   # stay under Loki's default per-stream ingestion rate limit
    print(f"[loki] {sent} events sent via {url}")


# --------------------------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--days", type=int, default=14)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--skip-prometheus", action="store_true")
    ap.add_argument("--skip-loki", action="store_true")
    ap.add_argument("--skip-rules", action="store_true", help="don't backfill recording-rule series")
    ap.add_argument("--rules-only", action="store_true", help="only (re)backfill recording rules over the last --days")
    ap.add_argument("--rules-eval-interval", default="10m", help="evaluation step for the historical rule backfill (default 10m)")
    ap.add_argument("--via", choices=["loki", "collector"], default="loki", help="where to POST the OTLP logs (default: Loki directly)")
    ap.add_argument("--dry-run", action="store_true", help="generate files only, touch nothing")
    args = ap.parse_args()

    rng = random.Random(args.seed)
    now = datetime.now(timezone.utc)
    os.makedirs(OUT_DIR, exist_ok=True)
    if args.rules_only:
        backfill_rules(now - timedelta(days=args.days), now, prom_data_dir(), args.rules_eval_interval)
        return
    sessions = gen_sessions(rng, args.days, now)
    n_events = sum(len(s.events) for s in sessions)
    n_users = len({s.user for s in sessions})
    print(f"generated {len(sessions)} sessions / {n_events} events for {n_users} users over {args.days} days (seed {args.seed})")
    print("  attribution mix:",
          f"unattributed={sum(s.project == '__missing__' for s in sessions)}",
          f"story=none={sum(s.story == 'none' for s in sessions)}",
          f"invalid={sum(s.story not in ('none', '__missing__') and not s.story.startswith('EPMDEDP-') for s in sessions)}")

    om_path = os.path.join(OUT_DIR, "metrics.om")
    n_samples = write_openmetrics(sessions, om_path)
    print(f"wrote {n_samples} metric samples -> {os.path.relpath(om_path)}")
    with open(os.path.join(OUT_DIR, "sessions.json"), "w", encoding="utf-8") as f:
        json.dump([{"session_id": s.sid, "user": s.user, "start": s.start.isoformat(), "end": s.end.isoformat(),
                    "project": s.project, "jira_epic": s.epic, "jira_story": s.story, "model": s.model,
                    "events": len(s.events)} for s in sessions], f, indent=1)
    if args.dry_run:
        return

    data_dir = prom_data_dir() if not args.skip_prometheus else None
    if not args.skip_prometheus:
        import_prometheus(om_path, data_dir)
    if not args.skip_loki:
        wait_http(f"{LOKI_URL}/ready")
        send_logs(build_log_records(sessions, args.via), args.via)
    if not args.skip_prometheus and not args.skip_rules:
        try:
            backfill_rules(sessions[0].start - timedelta(minutes=5), now, data_dir, args.rules_eval_interval)
        except subprocess.CalledProcessError as e:
            print(f"[rules] backfill failed (non-fatal): {e}")
            compose("start", "prometheus", check=False)

    print("\nDone. Open Grafana -> Claude Code folder, set the time range to Last 14 days.")
    print("Real traffic from your own Claude Code keeps flowing alongside (collector_env=local-poc vs demo).")


if __name__ == "__main__":
    main()
