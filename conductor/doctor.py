"""``python -m conductor doctor``: are the live engines reachable from this machine?

Checks Jev (a key, then one one-question request) and the taste model through
the Cursor CLI (the binary, ``CURSOR_API_KEY``, ``cursor-agent models`` lists
the configured slug, then one tiny JSON round trip). It also says which taste
backend and model a ``--live`` run would pick. Keys are reported as set or unset, never printed, and every
error goes through :func:`conductor.router.redact`.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import asdict, dataclass, field
from typing import Any

from cutmcp.jev import noul

from . import cursor_agent, jev, opus
from .errors import ConductorError
from .router import redact

PING_SYSTEM = "Health check for a JSON pipeline. Set `answer` to \"pong\"."
PING_SCHEMA: dict = {
    "type": "object",
    "properties": {"answer": {"type": "string", "enum": ["pong"]}},
    "required": ["answer"],
    "additionalProperties": False,
}


@dataclass
class Check:
    name: str
    live: bool = False
    detail: str = ""
    facts: dict = field(default_factory=dict)


def run(*, jev_client: Any | None = None) -> dict:
    forced = jev.dry_run_forced()
    checks = [_skipped("jev"), _skipped("taste")] if forced else [check_jev(jev_client), check_cursor()]
    try:
        backend, missing = opus.choose_backend()
        model = _taste_model(backend)
    except ConductorError as exc:
        backend, missing, model = None, redact(str(exc)), None
    return {
        "ok": all(item.live for item in checks),
        "dry_run_forced": forced,
        "checks": {item.name: asdict(item) for item in checks},
        "taste": {
            "setting": opus.setting(None, "CONDUCTOR_TASTE_BACKEND", "CONDUCTOR_OPUS_BACKEND", default=opus.AUTO).lower(),
            "backend": backend,
            "model": model,
            "detail": missing,
        },
    }


def check_jev(client: Any | None = None) -> Check:
    check = Check("jev")
    check.facts = {
        "OPENROUTER_API_KEY": _set("OPENROUTER_API_KEY"),
        "TYPESAFE_API_KEY": _set("TYPESAFE_API_KEY"),
    }
    if not jev.has_key():
        check.detail = "no key: set OPENROUTER_API_KEY or TYPESAFE_API_KEY"
        return check
    started = time.monotonic()
    try:
        endpoint = jev.resolve_endpoint()
        check.facts.update(provider=endpoint.provider, model=endpoint.model)
        batch = jev.ask({"value": 1}, {"ping": noul("Is state.value equal to 1?")}, live=True, client=client)
    except ConductorError as exc:
        check.detail = redact(str(exc))
        return check
    check.live = True
    check.facts["round_trip_seconds"] = round(time.monotonic() - started, 2)
    check.detail = f"{endpoint.provider} {batch.model}, round trip {check.facts['round_trip_seconds']}s"
    return check


def check_cursor() -> Check:
    check = Check("taste")
    binary = cursor_agent.find_binary()
    check.facts = {
        "binary": binary,
        "CURSOR_API_KEY": _set("CURSOR_API_KEY"),
        "CURSOR_AUTH_TOKEN": "set on host, not passed to cursor-agent" if _set("CURSOR_AUTH_TOKEN") == "set" else "unset",
    }
    try:
        endpoint = cursor_agent.resolve_endpoint()
        check.facts["model"] = endpoint.model
        listing = cursor_agent.list_models(endpoint)
        if not cursor_agent.has_model(listing, endpoint.model):
            raise ConductorError(f"`{cursor_agent.BINARY} models` does not list {endpoint.model}")
        check.facts["model_listed"] = True
        started = time.monotonic()
        reply = cursor_agent.complete(
            system=PING_SYSTEM, payload={"ping": True}, schema=PING_SCHEMA, endpoint=endpoint
        )
    except ConductorError as exc:
        check.detail = redact(str(exc))
        return check
    check.live = True
    check.facts["round_trip_seconds"] = round(time.monotonic() - started, 2)
    check.facts["reply_model"] = reply.model
    check.detail = (
        f"{binary}, {endpoint.model} listed, JSON round trip {check.facts['round_trip_seconds']}s"
    )
    return check


def format_text(result: dict) -> str:
    lines = []
    if result["dry_run_forced"]:
        lines.append("CONDUCTOR_DRY_RUN is set: live engines are forced off. Unset it to check them.")
    for name, label in (("jev", "jev"), ("taste", "taste")):
        row = result["checks"][name]
        lines.append(f"{label:<16} {'live' if row['live'] else 'down':<5} {row['detail']}")
    taste = result["taste"]
    selected = taste["backend"] or "none (creative calls become review markers)"
    model = taste["model"] or "(unset)"
    line = f"{'resolved':<16} {selected} {model} (CONDUCTOR_TASTE_BACKEND={taste['setting']})"
    if taste["detail"]:
        line += f": {taste['detail']}"
    lines.append(line)
    return "\n".join(lines)


def format_json(result: dict) -> str:
    return json.dumps(result, indent=2, sort_keys=True)


def _taste_model(backend: str | None) -> str | None:
    if backend == opus.CURSOR:
        return cursor_agent.resolve_endpoint().model
    if backend in (opus.ANTHROPIC, opus.GROK):
        return opus.resolve_endpoint().model
    model = opus.taste_model()
    try:
        return cursor_agent.cursor_model(model)
    except ConductorError:
        return model


def _skipped(name: str) -> Check:
    return Check(name, detail="skipped: CONDUCTOR_DRY_RUN forces the mock")


def _set(name: str) -> str:
    return "set" if os.environ.get(name, "").strip() else "unset"
