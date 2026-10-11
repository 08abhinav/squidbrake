"""
What an agent does when Squidbrake can't be reached: each rule says (rules.yaml `when_unreachable: block | allow`,
default block), and the agent's computer keeps a copy of those answers so it can follow them while the gateway is down.

  rules.yaml
    when_unreachable: block          # everything not listed below: blocked while the gateway is down (the default)
    rules:
      - id: reads
        action: allow
        when_unreachable: allow      # reading files is fine without the gateway
        match: { name: "Read" }

The hooks (claude_hook.py, agent_hook.py), the MCP connectors (gw_async.py) and client.py fetch the copy from
GET /v1/policy/offline at most every REFRESH_SECONDS while the gateway answers, and keep it in
~/.squidbrake/offline-policy-<gateway>.json. The first matching rule decides, exactly as on the gateway; with no copy
yet, everything is blocked. GATEWAY_FAIL_OPEN=1 on a machine still lets everything through there (the old switch).

What runs while the gateway is down is never recorded by it, so allow only what you'd accept unrecorded.
Standard library only, so it works wherever the hooks do.
"""
from __future__ import annotations

import fnmatch
import hashlib
import json
import os
import re
import time
from pathlib import Path
from typing import Any

REFRESH_SECONDS = 600
MATCH_FIELDS = ("kind", "name", "source", "client", "session_id")
OPS = {"gt": lambda a, b: a > b, "gte": lambda a, b: a >= b, "lt": lambda a, b: a < b, "lte": lambda a, b: a <= b,
       "eq": lambda a, b: a == b, "ne": lambda a, b: a != b}


def _home() -> Path:
    return Path(os.getenv("SQUIDBRAKE_HOME") or Path.home() / ".squidbrake")


def cache_path(url: str) -> Path:
    return Path(os.getenv("SQUIDBRAKE_OFFLINE_DIR") or _home()) / f"offline-policy-{hashlib.sha256(url.rstrip('/').encode()).hexdigest()[:10]}.json"


def load(url: str) -> dict | None:
    try:
        return json.loads(cache_path(url).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def save(url: str, policy: dict) -> None:
    path = cache_path(url)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps({**policy, "saved_at": time.time()}), encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        pass


def due(url: str) -> bool:
    """Is the saved copy missing or older than REFRESH_SECONDS?"""
    try:
        return time.time() - cache_path(url).stat().st_mtime > REFRESH_SECONDS
    except OSError:
        return True


def refresh(http, url: str) -> None:
    """With the hook's own httpx client, right after the gateway answered: keep the copy fresh. Never raises."""
    if not due(url):
        return
    try:
        r = http.get("/v1/policy/offline", timeout=5)
        if r.status_code == 200:
            save(url, r.json())
    except Exception:
        pass


async def arefresh(http, url: str) -> None:
    if not due(url):
        return
    try:
        r = await http.get("/v1/policy/offline", timeout=5)
        if r.status_code == 200:
            save(url, r.json())
    except Exception:
        pass


def _number(input: Any, field: str) -> float | None:
    if isinstance(input, str):
        try:
            input = json.loads(input)
        except ValueError:
            return None
    for part in field.split("."):
        if not isinstance(input, dict) or part not in input:
            return None
        input = input[part]
    if isinstance(input, bool):
        return None
    try:
        n = float(input)
    except (TypeError, ValueError):
        return None
    return n if n == n else None


def matches(rule: dict, values: dict, input: Any) -> bool:
    m = rule.get("match") or {}
    for f in MATCH_FIELDS:
        if f in m:
            pats = [m[f]] if isinstance(m[f], str) else list(m[f])
            if not any(fnmatch.fnmatchcase((values.get(f) or "").lower(), str(p).lower()) for p in pats):
                return False
    if m.get("input_regex"):
        text = input if isinstance(input, str) else json.dumps(input, default=str, ensure_ascii=False)
        try:
            if not re.search(m["input_regex"], text or "", re.I | re.S):
                return False
        except re.error:
            return False
    for field, ops in (m.get("input") or {}).items():
        n = _number(input, field)
        if n is None or not all(op in OPS and OPS[op](n, v) for op, v in ops.items()):
            return False
    return True


def decide(url: str, *, name: str, kind: str = "tool_call", source: str | None = None, session_id: str | None = None,
           input: Any = None, fail_open: bool = False) -> tuple[bool, str]:
    """(run it?, why) while the gateway at `url` can't be reached."""
    if fail_open:
        return True, "GATEWAY_FAIL_OPEN is set on this computer, so it ran unrecorded"
    pol = load(url)
    if not pol:
        return False, "no copy of the rules' offline settings on this computer yet, so it was blocked"
    values = {"kind": kind, "name": name, "source": source, "client": pol.get("client"), "session_id": session_id}
    for rule in pol.get("rules") or []:
        if matches(rule, values, input):
            if rule.get("when_unreachable") == "allow":
                return True, f"rule '{rule.get('id')}' says when_unreachable: allow, so it ran unrecorded"
            return False, f"rule '{rule.get('id')}' says to block it while Squidbrake is unreachable"
    if pol.get("default") == "allow":
        return True, "the rules say when_unreachable: allow, so it ran unrecorded"
    return False, "the rules block everything while Squidbrake is unreachable (when_unreachable: block)"
