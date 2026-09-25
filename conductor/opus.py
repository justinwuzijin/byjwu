"""Creative and taste decisions.

The default model is the Cursor slug ``grok-4.7-medium``, from
``CONDUCTOR_TASTE_MODEL``. A Claude model id (``claude`` or ``opus`` in the
name) keeps the Anthropic Messages path, off unless that env var selects it.
``CONDUCTOR_OPUS_MODEL`` still overrides the Anthropic model when the taste
model is Claude. ``CONDUCTOR_OPUS_EFFORT`` sets ``output_config.effort``.

The answer is constrained with ``output_config.format`` (JSON schema), not a
forced tool call: Opus 5.5 rejects ``tool_choice`` ``tool``/``any`` and does
not accept disabled thinking. The wire schema drops the limits Anthropic does
not support; :func:`conductor.schema.validate` checks the full schema on the
way back.

Only :mod:`conductor.router` calls this. Dry-run never reaches it.
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from .errors import ConductorError
from .schema import validate, wire

ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_VERSION = "2023-06-01"
OPUS_MODEL = "claude-opus-5-5"
TASTE_MODEL = "grok-4.7-medium"
TASTE_URL = "https://api.x.ai/v1/chat/completions"
MOCK_MODEL = "conductor-opus-mock-1"
DEFAULT_EFFORT = "medium"
EFFORTS = frozenset({"low", "medium", "high", "xhigh", "max"})

#: USD per million (input, output) tokens, from Anthropic's model page. Only
#: models listed here get an estimated cost in the report.
USD_PER_M: dict[str, tuple[float, float]] = {OPUS_MODEL: (4.0, 20.0)}

_RETRY_STATUS = frozenset({408, 429, 500, 502, 503, 504, 529})
_BACKOFF = 0.5


@dataclass(frozen=True)
class Endpoint:
    url: str
    model: str
    effort: str
    headers: dict[str, str]
    provider: str = "anthropic"


@dataclass
class Reply:
    data: Any
    model: str
    request_id: str | None
    usage: dict[str, Any] | None
    stop_reason: str | None


def taste_model(environ: Mapping[str, str] | None = None) -> str:
    env = os.environ if environ is None else environ
    return env.get("CONDUCTOR_TASTE_MODEL", "").strip() or TASTE_MODEL


def uses_anthropic(model: str) -> bool:
    low = model.lower()
    return "claude" in low or "opus" in low


def has_key(environ: Mapping[str, str] | None = None) -> bool:
    env = os.environ if environ is None else environ
    model = taste_model(env)
    if uses_anthropic(model):
        return bool(env.get("ANTHROPIC_API_KEY", "").strip())
    return bool(env.get("XAI_API_KEY", "").strip() or env.get("CONDUCTOR_TASTE_KEY", "").strip())


def resolve_endpoint(environ: Mapping[str, str] | None = None) -> Endpoint:
    env = os.environ if environ is None else environ
    model = taste_model(env)
    effort = env.get("CONDUCTOR_OPUS_EFFORT", "").strip().lower() or DEFAULT_EFFORT
    if effort not in EFFORTS:
        raise ConductorError(f"CONDUCTOR_OPUS_EFFORT must be one of {sorted(EFFORTS)}")
    if uses_anthropic(model):
        key = env.get("ANTHROPIC_API_KEY", "").strip()
        if not key:
            raise ConductorError(
                "ANTHROPIC_API_KEY is unset. Creative decisions become review markers."
            )
        chosen = env.get("CONDUCTOR_OPUS_MODEL", "").strip() or model
        url = env.get("CONDUCTOR_ANTHROPIC_URL", "").strip() or ANTHROPIC_URL
        return Endpoint(
            url=url,
            model=chosen,
            effort=effort,
            headers={
                "x-api-key": key,
                "anthropic-version": ANTHROPIC_VERSION,
                "content-type": "application/json",
            },
            provider="anthropic",
        )
    key = env.get("XAI_API_KEY", "").strip() or env.get("CONDUCTOR_TASTE_KEY", "").strip()
    if not key:
        raise ConductorError(
            "No taste-model key. Set XAI_API_KEY or CONDUCTOR_TASTE_KEY, "
            "or set CONDUCTOR_TASTE_MODEL to a Claude id with ANTHROPIC_API_KEY."
        )
    url = env.get("CONDUCTOR_TASTE_URL", "").strip() or TASTE_URL
    return Endpoint(
        url=url,
        model=model,
        effort=effort,
        headers={"Authorization": f"Bearer {key}", "content-type": "application/json"},
        provider="grok",
    )


def complete(
    *,
    system: str,
    payload: Mapping[str, Any],
    schema: Mapping[str, Any],
    endpoint: Endpoint,
    client: Any,
    max_tokens: int,
) -> Reply:
    """One structured request. Raises :class:`ConductorError` on any failure."""
    if endpoint.provider == "grok":
        return _complete_grok(system=system, payload=payload, schema=schema, endpoint=endpoint, client=client)
    body = {
        "model": endpoint.model,
        "max_tokens": int(max_tokens),
        "system": system,
        "messages": [
            {"role": "user", "content": json.dumps(payload, sort_keys=True, ensure_ascii=False)}
        ],
        "output_config": {
            "effort": endpoint.effort,
            "format": {"type": "json_schema", "schema": wire(schema)},
        },
    }
    raw = _post(client, endpoint, body)
    stop = raw.get("stop_reason")
    if stop in {"refusal", "max_tokens"}:
        raise ConductorError(f"Opus stopped with {stop}; the answer is not usable")
    text = _text(raw)
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ConductorError(f"Opus returned non-JSON text: {text[:120]!r}") from exc
    usage = raw.get("usage")
    return Reply(
        data=validate(data, schema),
        model=str(raw.get("model") or endpoint.model),
        request_id=str(raw["id"]) if raw.get("id") else None,
        usage=dict(usage) if isinstance(usage, Mapping) else None,
        stop_reason=str(stop) if stop else None,
    )


def _complete_grok(*, system, payload, schema, endpoint: Endpoint, client) -> Reply:
    body = {
        "model": endpoint.model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": json.dumps(payload, sort_keys=True, ensure_ascii=False)},
        ],
        "response_format": {"type": "json_object"},
    }
    raw = _post(client, endpoint, body)
    choices = raw.get("choices")
    if not isinstance(choices, list) or not choices:
        raise ConductorError("taste model returned no choices")
    message = choices[0].get("message") if isinstance(choices[0], Mapping) else None
    text = str((message or {}).get("content") or "")
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ConductorError(f"taste model returned non-JSON text: {text[:120]!r}") from exc
    usage = raw.get("usage")
    return Reply(
        data=validate(data, schema),
        model=str(raw.get("model") or endpoint.model),
        request_id=str(raw["id"]) if raw.get("id") else None,
        usage=dict(usage) if isinstance(usage, Mapping) else None,
        stop_reason=None,
    )


def _post(client: Any, endpoint: Endpoint, body: Mapping[str, Any]) -> Mapping[str, Any]:
    last: Exception | None = None
    for attempt in range(3):
        try:
            response = client.post(endpoint.url, headers=endpoint.headers, json=body)
        except Exception as exc:  # httpx transport errors are plain Exceptions here
            last = exc
        else:
            if response.status_code < 300:
                try:
                    payload = response.json()
                except Exception as exc:
                    raise ConductorError(f"Opus returned non-JSON: {response.text[:200]}") from exc
                if not isinstance(payload, Mapping):
                    raise ConductorError("Opus returned a non-object JSON body")
                return payload
            message = f"Opus HTTP {response.status_code}: {response.text[:200]}"
            if response.status_code not in _RETRY_STATUS:
                raise ConductorError(message)
            last = ConductorError(message)
        time.sleep(_BACKOFF * (2**attempt))
    raise ConductorError(f"Opus unreachable after 3 attempts: {last}")


def _text(raw: Mapping[str, Any]) -> str:
    blocks = raw.get("content")
    if not isinstance(blocks, list):
        raise ConductorError("Opus response has no content list")
    texts = [
        str(block.get("text") or "")
        for block in blocks
        if isinstance(block, Mapping) and block.get("type") == "text"
    ]
    texts = [text for text in texts if text.strip()]
    if not texts:
        raise ConductorError("Opus response has no text block")
    return texts[-1]
