"""Cursor CLI client for taste decisions.

The binary is ``CURSOR_AGENT_BIN``, else ``cursor-agent`` on ``PATH``, else
``~/.local/bin/cursor-agent``. Auth is ``CURSOR_API_KEY`` only. One request
is one headless run::

    cursor-agent -p --mode ask --trust --model grok-4.7-medium \\
        --output-format json --workspace <empty temp dir> '<prompt>'

``-p`` can use tools, so the run is always ``--mode ask`` (read-only) in an
empty temporary workspace. The child never sees ``CURSOR_AUTH_TOKEN`` or the
other engines' keys. The slug is ``CONDUCTOR_TASTE_MODEL`` (``CONDUCTOR_OPUS_MODEL``
still works) and defaults to ``grok-4.7-medium``. Opus stays selectable
(``claude-opus-5-5-medium``); the plain name ``claude-opus-5-5`` is that slug.
Anything outside the two confirmed families is refused, including other Grok
or xAI ids. ``CONDUCTOR_TASTE_TIMEOUT`` (or ``CONDUCTOR_OPUS_TIMEOUT``) bounds
the run (default 180s: a run is about 50s wall-clock, mostly CLI startup and
cached agent context).

``--output-format json`` is documented as one JSON object with the reply as a
string in ``result``. That shape is not relied on: :func:`parse` takes the
outer object when it has one, finds the answer JSON inside ``result`` (fenced
or not), and otherwise takes the last JSON object on stdout. The answer is
checked with :func:`conductor.schema.validate` either way.

Only :mod:`conductor.router` and :mod:`conductor.doctor` call this.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .errors import ConductorError
from .opus import Reply, setting, timeout_seconds
from .schema import validate

BINARY = "cursor-agent"
MODEL = "grok-4.7-medium"
OPUS_CURSOR_MODEL = "claude-opus-5-5-medium"
#: Cursor has no plain ``claude-opus-5-5`` slug. That name means the base 1M model.
ALIASES = {"claude-opus-5-5": OPUS_CURSOR_MODEL}
DEFAULT_TIMEOUT = 180.0
_CURSOR_SLUG = re.compile(
    r"^(?:grok-4\.7-(?:low|medium|high|xhigh)|claude-opus-5-5-(?:low|medium|high|xhigh|max))(?:-fast)?$"
)
#: Linux caps one argv string at 128 KiB. Router windows stay well under this.
MAX_PROMPT_BYTES = 120_000

#: Never passed to the child. The host sets ``CURSOR_AUTH_TOKEN`` and it must not be used.
STRIPPED_ENV = (
    "CURSOR_AUTH_TOKEN",
    "ANTHROPIC_API_KEY",
    "OPENROUTER_API_KEY",
    "TYPESAFE_API_KEY",
)

_FENCE = re.compile(r"```[A-Za-z0-9_-]*\s*\n?(.*?)```", re.DOTALL)


@dataclass(frozen=True)
class Endpoint:
    binary: str
    model: str
    api_key: str
    timeout: float


def find_binary(environ: Mapping[str, str] | None = None) -> str | None:
    env = os.environ if environ is None else environ
    pinned = env.get("CURSOR_AGENT_BIN", "").strip()
    if pinned:
        path = Path(pinned).expanduser()
        return str(path) if _executable(path) else None
    found = shutil.which(BINARY, path=env.get("PATH"))
    if found:
        return found
    home = env.get("HOME", "").strip()
    fallback = Path(home) / ".local" / "bin" / BINARY if home else Path.home() / ".local" / "bin" / BINARY
    return str(fallback) if _executable(fallback) else None


def unavailable_reason(environ: Mapping[str, str] | None = None) -> str | None:
    """Why the Cursor backend cannot run, or ``None`` when it can."""
    env = os.environ if environ is None else environ
    missing = []
    if find_binary(env) is None:
        pinned = env.get("CURSOR_AGENT_BIN", "").strip()
        missing.append(
            f"CURSOR_AGENT_BIN ({pinned}) is not an executable file" if pinned
            else f"{BINARY} is not on PATH or in ~/.local/bin"
        )
    if not env.get("CURSOR_API_KEY", "").strip():
        missing.append("CURSOR_API_KEY is unset")
    return "; ".join(missing) or None


def cursor_model(slug: str) -> str:
    """A confirmed Cursor taste slug. ``claude-opus-5-5`` means ``-medium``."""
    model = ALIASES.get(slug.strip(), slug.strip())
    if not _CURSOR_SLUG.match(model):
        raise ConductorError(
            f"refusing {slug!r}: taste via Cursor takes grok-4.7-{{low,medium,high,xhigh}}[-fast] "
            f"or claude-opus-5-5-{{low,medium,high,xhigh,max}}[-fast]"
        )
    return model


def resolve_endpoint(environ: Mapping[str, str] | None = None) -> Endpoint:
    env = os.environ if environ is None else environ
    reason = unavailable_reason(env)
    if reason:
        raise ConductorError(f"Cursor CLI unavailable: {reason}")
    model = cursor_model(setting(env, "CONDUCTOR_TASTE_MODEL", "CONDUCTOR_OPUS_MODEL", default=MODEL))
    return Endpoint(
        binary=find_binary(env) or BINARY,
        model=model,
        api_key=env["CURSOR_API_KEY"].strip(),
        timeout=timeout_seconds(env, default=DEFAULT_TIMEOUT),
    )


def child_env(api_key: str, environ: Mapping[str, str] | None = None) -> dict[str, str]:
    env = dict(os.environ if environ is None else environ)
    for name in STRIPPED_ENV:
        env.pop(name, None)
    env["CURSOR_API_KEY"] = api_key
    return env


def run(
    endpoint: Endpoint,
    args: Sequence[str],
    *,
    prompt: str | None = None,
    timeout: float | None = None,
) -> str:
    """Run the CLI in an empty temp directory. Returns stdout, raises on any failure.

    With a ``prompt``, that directory is also the ``--workspace``.
    """
    limit = endpoint.timeout if timeout is None else timeout
    with tempfile.TemporaryDirectory(prefix="conductor-cursor-") as workspace:
        argv = [endpoint.binary, *args]
        if prompt is not None:
            argv += ["--workspace", workspace, prompt]
        try:
            done = subprocess.run(
                argv,
                cwd=workspace,
                env=child_env(endpoint.api_key),
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                timeout=limit,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise ConductorError(f"{BINARY} timed out after {limit:g}s") from exc
        except OSError as exc:
            raise ConductorError(f"{BINARY} could not start ({endpoint.binary}): {exc.strerror or exc}") from exc
    if done.returncode != 0:
        tail = (done.stderr or done.stdout or "").strip()[-300:]
        raise ConductorError(f"{BINARY} exited {done.returncode}: {tail}")
    return done.stdout or ""


def complete(
    *,
    system: str,
    payload: Mapping[str, Any],
    schema: Mapping[str, Any],
    endpoint: Endpoint,
) -> Reply:
    """One structured request. Raises :class:`ConductorError` on any failure."""
    prompt = prompt_for(system, payload, schema)
    if len(prompt.encode("utf-8")) > MAX_PROMPT_BYTES:
        raise ConductorError(f"prompt is over {MAX_PROMPT_BYTES} bytes; lower the Opus window")
    stdout = run(
        endpoint,
        ["-p", "--mode", "ask", "--trust", "--model", endpoint.model, "--output-format", "json"],
        prompt=prompt,
    )
    return parse(stdout, schema, model=endpoint.model)


def prompt_for(system: str, payload: Mapping[str, Any], schema: Mapping[str, Any]) -> str:
    return "\n\n".join(
        [
            system,
            "Do not use tools, read files, or run commands. Reply with exactly one JSON "
            "object and nothing else. It must match this JSON Schema:",
            json.dumps(schema, sort_keys=True, ensure_ascii=False),
            "Input:",
            json.dumps(payload, sort_keys=True, ensure_ascii=False),
        ]
    )


def parse(stdout: str, schema: Mapping[str, Any], *, model: str) -> Reply:
    """The validated answer from CLI stdout, whatever the outer shape turned out to be."""
    outer = _outer(stdout)
    if outer is not None:
        if outer.get("is_error") is True or str(outer.get("subtype") or "success") != "success":
            raise ConductorError(f"{BINARY} reported an error: {str(outer.get('result') or '')[:200]!r}")
        text = outer["result"]
    else:
        text = stdout
    data = _answer(text)
    if data is None:
        raise ConductorError(f"{BINARY} returned no JSON answer: {text.strip()[:120]!r}")
    usage = _usage(outer.get("usage")) if outer else None
    request_id = (outer or {}).get("request_id") or (outer or {}).get("session_id")
    return Reply(
        data=validate(data, schema),
        model=str((outer or {}).get("model") or model),
        request_id=str(request_id) if request_id else None,
        usage=usage,
        stop_reason=None,
    )


def list_models(endpoint: Endpoint, *, timeout: float = 30.0) -> str:
    return run(endpoint, ["models"], timeout=timeout)


def has_model(listing: str, slug: str) -> bool:
    return re.search(rf"(?<![\w.-]){re.escape(slug)}(?![\w.-])", listing) is not None


def _outer(stdout: str) -> dict | None:
    """The CLI's result object: the whole stdout, else the last object that carries ``result``."""
    try:
        whole = json.loads(stdout.strip())
    except json.JSONDecodeError:
        whole = None
    candidates = [whole] if whole is not None else list(reversed(_objects(stdout)))
    for item in candidates:
        if isinstance(item, dict) and isinstance(item.get("result"), str):
            return item
    return None


def _answer(text: str) -> Any:
    stripped = text.strip()
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        pass
    for block in reversed(_FENCE.findall(stripped)):
        try:
            return json.loads(block.strip())
        except json.JSONDecodeError:
            continue
    found = [item for item in _objects(stripped) if isinstance(item, dict)]
    return found[-1] if found else None


def _objects(text: str) -> list[Any]:
    """Top-level JSON objects in ``text``, in order."""
    decoder = json.JSONDecoder()
    found = []
    index = text.find("{")
    while index != -1:
        try:
            item, end = decoder.raw_decode(text, index)
        except json.JSONDecodeError:
            index = text.find("{", index + 1)
            continue
        found.append(item)
        index = text.find("{", end)
    return found


def _usage(raw: Any) -> dict | None:
    if not isinstance(raw, Mapping):
        return None
    names = {
        "inputTokens": "input_tokens",
        "outputTokens": "output_tokens",
        "promptTokens": "input_tokens",
        "completionTokens": "output_tokens",
        "cacheReadTokens": "cache_read_tokens",
        "cacheWriteTokens": "cache_write_tokens",
    }
    usage = {names.get(key, key): value for key, value in raw.items()}
    return usage or None


def _executable(path: Path) -> bool:
    return path.is_file() and os.access(path, os.X_OK)
