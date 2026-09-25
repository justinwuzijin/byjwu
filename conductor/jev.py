"""Decisions API client for Cut Conductor.

Live calls speak the Decisions request: ``model``, ``state``, ``questions``.
Two hosts:

- ``OPENROUTER_API_KEY`` → ``POST https://openrouter.ai/api/alpha/decisions``
  with model ``typesafe/jev-1.13``. This is the Decisions API.
- ``TYPESAFE_API_KEY`` → ``POST https://api.typesafe.ai/v1/systemone`` with
  model ``jev-1.13.0``. TypeSafe's own host serves the same question/answer
  contract on ``/v1/systemone`` and rejects the OpenRouter slug ``jev-1.13``.

OpenRouter wins when both keys are set. ``CONDUCTOR_JEV_PROVIDER`` forces one.

Dry-run is the default. With no ``--live`` flag, or with ``CONDUCTOR_DRY_RUN``
set, this module never opens a socket. The mock is calibrated to the candidate
heuristics (a long silence leans ``remove``, a flash frame leans ``review``)
so an editor can read a dry-run. It is not a claim that live Jev is calibrated,
and it is not the hash mock in ``cutmcp.jev``.

Jev still does not write. Questions are a choice plus a risk noul.
"""

from __future__ import annotations

import os
import re
import time
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from cutmcp.jev import Answer

from .errors import ConductorError

OPENROUTER_URL = "https://openrouter.ai/api/alpha/decisions"
OPENROUTER_MODEL = "typesafe/jev-1.13"
TYPESAFE_URL = "https://api.typesafe.ai/v1/systemone"
TYPESAFE_MODEL = "jev-1.13.0"
MOCK_MODEL = "conductor-mock-1"
MOCK_PROVIDER = "mock"

_RETRY_STATUS = frozenset({408, 429, 500, 502, 503, 504})
_TIMEOUT = 60.0

ACTIONS = ("keep", "tighten", "remove", "mark_review", "escalate")

ACTION_CRITERIA = {
    "keep": "Leave the timeline alone. The moment earns its length.",
    "tighten": "Trim the dead air or filler but keep the surrounding thought.",
    "remove": "Lift this region out. It does not earn its time.",
    "mark_review": "A human should look. The signal is real but the call is not safe to trust.",
    "escalate": "Stop and discuss. The moment may be load-bearing, or the risk is high.",
}

_XAI = re.compile(r"grok|x-ai|\bxai\b|x\.ai", re.IGNORECASE)


@dataclass(frozen=True)
class Endpoint:
    provider: str
    url: str
    model: str
    headers: dict[str, str]


@dataclass
class BatchResult:
    answers: dict[str, Answer]
    model: str
    provider: str
    request_id: str | None
    endpoint: str | None
    usage: dict[str, Any] | None
    dry_run: bool


def dry_run_forced(environ: Mapping[str, str] | None = None) -> bool:
    env = os.environ if environ is None else environ
    return env.get("CONDUCTOR_DRY_RUN", "").strip().lower() in {"1", "true", "yes"}


def refuse_xai(model: str, url: str) -> None:
    """Kept so older callers import. Grok is allowed; this does not reject."""
    return None


def has_key(environ: Mapping[str, str] | None = None) -> bool:
    """True when the provider ``resolve_endpoint`` would pick has a key."""
    env = os.environ if environ is None else environ
    provider = env.get("CONDUCTOR_JEV_PROVIDER", "").strip().lower()
    openrouter = bool(env.get("OPENROUTER_API_KEY", "").strip())
    typesafe = bool(env.get("TYPESAFE_API_KEY", "").strip())
    if provider == "typesafe":
        return typesafe
    if provider == "openrouter":
        return openrouter
    return openrouter or typesafe


def resolve_endpoint(environ: Mapping[str, str] | None = None) -> Endpoint:
    """Pick a host from the environment. Raises when a live call has no key."""
    env = os.environ if environ is None else environ
    provider = env.get("CONDUCTOR_JEV_PROVIDER", "").strip().lower()
    openrouter_key = env.get("OPENROUTER_API_KEY", "").strip()
    typesafe_key = env.get("TYPESAFE_API_KEY", "").strip()
    if provider not in {"", "openrouter", "typesafe"}:
        raise ConductorError(
            "CONDUCTOR_JEV_PROVIDER must be 'openrouter' or 'typesafe'"
        )
    if provider == "typesafe" or (provider == "" and not openrouter_key and typesafe_key):
        if not typesafe_key:
            raise ConductorError(
                "TYPESAFE_API_KEY is unset. Export it, or pass no --live flag to dry-run."
            )
        model = env.get("CONDUCTOR_JEV_MODEL", "").strip() or TYPESAFE_MODEL
        url = env.get("CONDUCTOR_TYPESAFE_URL", "").strip() or TYPESAFE_URL
        return Endpoint(
            provider="typesafe",
            url=url,
            model=model,
            headers={
                "Authorization": f"Bearer {typesafe_key}",
                "Content-Type": "application/json",
            },
        )
    if not openrouter_key:
        raise ConductorError(
            "No Jev key. Set OPENROUTER_API_KEY or TYPESAFE_API_KEY, "
            "or drop --live to dry-run with the local mock."
        )
    model = env.get("CONDUCTOR_JEV_MODEL", "").strip() or OPENROUTER_MODEL
    url = env.get("CONDUCTOR_OPENROUTER_URL", "").strip() or OPENROUTER_URL
    return Endpoint(
        provider="openrouter",
        url=url,
        model=model,
        headers={
            "Authorization": f"Bearer {openrouter_key}",
            "Content-Type": "application/json",
            "HTTP-Referer": "https://github.com/justinwuzijin/jevid",
            "X-OpenRouter-Title": "Cut Conductor",
        },
    )


def ask(
    state: Mapping[str, Any],
    questions: Mapping[str, Mapping[str, Any]],
    *,
    live: bool,
    client: Any | None = None,
) -> BatchResult:
    """Answer every question about one state. One request, never one per question."""
    if not questions:
        return BatchResult({}, MOCK_MODEL, MOCK_PROVIDER, None, None, None, True)
    if not live or dry_run_forced():
        return _mock_batch(state, questions)
    endpoint = resolve_endpoint()
    body = {"model": endpoint.model, "state": state, "questions": dict(questions)}
    owns_client = client is None
    if owns_client:
        import httpx

        client = httpx.Client(timeout=_TIMEOUT)
    try:
        payload = _post(client, endpoint, body)
    finally:
        if owns_client:
            client.close()
    return _parse_payload(payload, questions, endpoint)


def _post(client: Any, endpoint: Endpoint, body: Mapping[str, Any]) -> Mapping[str, Any]:
    last: Exception | None = None
    for attempt in range(3):
        try:
            response = client.post(endpoint.url, headers=endpoint.headers, json=body)
        except Exception as exc:  # network flake; httpx errors are Exception
            last = exc
        else:
            if response.status_code < 300:
                try:
                    payload = response.json()
                except Exception as exc:
                    raise ConductorError(
                        f"Jev returned non-JSON: {response.text[:200]}"
                    ) from exc
                if not isinstance(payload, Mapping):
                    raise ConductorError("Jev returned a non-object JSON body")
                return payload
            message = f"Jev HTTP {response.status_code}: {response.text[:200]}"
            if response.status_code not in _RETRY_STATUS:
                raise ConductorError(message)
            last = ConductorError(message)
        time.sleep(0.05 * (2**attempt))
    raise ConductorError(f"Jev unreachable after 3 attempts: {last}")


def _parse_payload(
    payload: Mapping[str, Any],
    questions: Mapping[str, Mapping[str, Any]],
    endpoint: Endpoint,
) -> BatchResult:
    raw = payload.get("answers")
    if raw is None:
        raw = payload.get("results")
    if not isinstance(raw, Mapping):
        raise ConductorError(
            f"no 'answers' object in Jev response: {list(payload)[:8]}"
        )
    answers: dict[str, Answer] = {}
    for key, spec in questions.items():
        if key not in raw:
            raise ConductorError(f"Jev did not answer {key!r}")
        answers[key] = _one(raw[key], spec, key)
    usage = payload.get("usage")
    usage_out = dict(usage) if isinstance(usage, Mapping) else None
    return BatchResult(
        answers=answers,
        model=str(payload.get("model") or endpoint.model),
        provider=str(payload.get("provider") or endpoint.provider),
        request_id=str(payload["id"]) if payload.get("id") else None,
        endpoint=endpoint.url,
        usage=usage_out,
        dry_run=False,
    )


def _one(raw: Any, spec: Mapping[str, Any], key: str) -> Answer:
    kind = spec.get("type")
    if kind == "noul":
        if isinstance(raw, Mapping):
            value = raw.get("noul", raw.get("value"))
        elif isinstance(raw, (int, float)) and not isinstance(raw, bool):
            value = raw
        else:
            raise ConductorError(f"unreadable noul for {key!r}")
        if value is None:
            raise ConductorError(f"noul {key!r} has no probability")
        probability = _clamp(float(value))
        return Answer(
            probability,
            max(probability, 1.0 - probability),
            {"true": probability, "false": round(1.0 - probability, 6)},
        )
    if kind == "choice":
        options = spec.get("criteria") or {}
        if isinstance(raw, str):
            picked, confidence, probs = raw, 1.0, None
        elif isinstance(raw, Mapping):
            picked = raw.get("choice", raw.get("value"))
            if picked is None:
                raise ConductorError(f"choice {key!r} has no selection")
            picked = str(picked)
            probs_raw = raw.get("probabilities")
            probs = (
                {str(k): float(v) for k, v in probs_raw.items()}
                if isinstance(probs_raw, Mapping)
                else None
            )
            if raw.get("confidence") is not None:
                confidence = _clamp(float(raw["confidence"]))
            elif probs and picked in probs:
                confidence = _clamp(float(probs[picked]))
            else:
                confidence = 1.0
        else:
            raise ConductorError(f"unreadable choice for {key!r}")
        if picked not in options:
            raise ConductorError(
                f"answer {picked!r} for {key!r} is not one of {list(options)}"
            )
        return Answer(picked, confidence, probs)
    raise ConductorError(f"Cut Conductor does not ask {kind!r} questions ({key})")


def _mock_batch(
    state: Mapping[str, Any], questions: Mapping[str, Mapping[str, Any]]
) -> BatchResult:
    by_id = {item["id"]: item for item in state.get("candidates") or [] if "id" in item}
    answers: dict[str, Answer] = {}
    for key, spec in questions.items():
        candidate_id, kind = _split_key(key)
        candidate = by_id.get(candidate_id)
        if candidate is None:
            raise ConductorError(f"mock judge has no candidate {candidate_id!r} in state")
        action, confidence, risk = policy(candidate, state.get("taste") or {})
        if kind == "action" and spec.get("type") == "choice":
            answers[key] = Answer(action, confidence, _distribution(action, confidence))
        elif kind == "risk" and spec.get("type") == "noul":
            answers[key] = Answer(
                risk,
                max(risk, 1.0 - risk),
                {"true": risk, "false": round(1.0 - risk, 6)},
            )
        else:
            raise ConductorError(f"mock judge has no branch for {key!r}")
    return BatchResult(answers, MOCK_MODEL, MOCK_PROVIDER, None, None, None, True)


def _split_key(key: str) -> tuple[str, str]:
    if key.endswith("_action"):
        return key[: -len("_action")], "action"
    if key.endswith("_risk"):
        return key[: -len("_risk")], "risk"
    raise ConductorError(f"unexpected question key {key!r}")


def policy(candidate: Mapping[str, Any], taste: Mapping[str, Any] | None = None) -> tuple[str, float, float]:
    """Return ``(action, confidence, risk)`` from heuristic signals.

    Strong, unambiguous signals get a high confidence and a low risk. Ambiguous
    ones land on ``mark_review``. The numbers are fixed so a dry-run is the
    same on every machine. Taste prefs nudge a few of those numbers; the
    default prefs are a no-op. This is a stand-in, not a trained model.

    It is also the deterministic rule set the router falls back to when live
    Jev is unavailable, with the confidence discounted.
    """
    kind = candidate.get("kind")
    signals = candidate.get("signals") or {}
    duration = float(candidate.get("duration_seconds") or 0.0)
    prefs = (taste or {}).get("prefs") or {}
    # Colour is a note, not a cut. Checked before the cold-open shortcut so a
    # "keep the opening" preference cannot hide a missing role or a bad frame.
    if kind == "colour_aspect":
        return "escalate", 0.58, 0.62
    if kind in {"colour_role", "colour_unseen"}:
        return "mark_review", 0.64, 0.41
    # Notes a person can read. A cold-open preference must not hide them, and
    # none of these is a range the mock is willing to lift.
    if kind == "covered_gap":
        return "mark_review", 0.72, 0.48
    if kind == "source_reuse":
        return "mark_review", 0.70, 0.44
    if kind == "rhythm_shift":
        return "mark_review", 0.66, 0.40
    if kind == "rate_mix":
        return "mark_review", 0.63, 0.36
    if kind == "untrimmed_run":
        return "mark_review", 0.68, 0.42
    if kind == "silent_card":
        return "mark_review", 0.74, 0.30
    if kind == "music_tail":
        return "mark_review", 0.67, 0.38
    if prefs.get("cold_open_bias") == "keep" and signals.get("is_cold_open"):
        return "keep", 0.90, 0.10
    if kind == "silence_gap":
        if prefs.get("target_pace") == "loose":
            return "tighten", 0.70, 0.30
        if duration >= 2.0 or prefs.get("target_pace") == "tight":
            return "remove", 0.86, 0.12
        return "tighten", 0.74, 0.28
    if kind == "short_clip":
        if signals.get("flash") or duration < 0.20:
            return "remove", 0.91, 0.14
        return "mark_review", 0.61, 0.42
    if kind == "long_static":
        wps = signals.get("words_per_second")
        if wps is None or float(wps) < 0.15:
            return "tighten", 0.73, 0.33
        return "mark_review", 0.57, 0.49
    if kind == "filler_pause":
        if signals.get("pure_filler"):
            return "tighten", 0.84, 0.18
        if signals.get("restart"):
            return "tighten", 0.72, 0.33
        if signals.get("adjacent_filler"):
            return "tighten", 0.71, 0.31
        return "mark_review", 0.60, 0.46
    return "mark_review", 0.55, 0.50


def _distribution(winner: str, mass: float) -> dict[str, float]:
    others = [action for action in ACTIONS if action != winner]
    share = (1.0 - mass) / len(others)
    rounded = {action: round(share, 4) for action in others}
    rounded[winner] = round(mass, 4)
    drift = round(1.0 - sum(rounded.values()), 4)
    rounded[winner] = round(rounded[winner] + drift, 4)
    return {action: rounded[action] for action in ACTIONS}


def _clamp(value: float) -> float:
    if value < 0.0:
        return 0.0
    if value > 1.0:
        return 1.0
    return value
