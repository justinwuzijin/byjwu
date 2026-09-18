"""Client for TypeSafe's Jev (System One) model.

Checked against the live `https://api.typesafe.ai/openapi.json` (TypeSafe
0.2.0). Three things the launch-materials reconstruction got wrong, in case
you are holding an older copy of this file:

- every question carries its options under `criteria`, not `levels` or
  `options`, and `criteria` is required for score and choice
- a noul answers `{"type":"noul","noul":0.98}` and carries **no confidence**
- a score answers a *float* expected value — the probability-weighted mean
  of the rubric levels, so 1.7 is a real answer — not the winning index

Responses are still parsed forgivingly: `answers` or `results`, an object or
a bare scalar, and either the index or the label where a level is expected.

This module is the only place in cutmcp coupled to the API contract. If the
shape changes again, fix it here and nothing else should need to change.

Jev selects; it does not generate. Every question is a constrained pick from
options you define, which is why it structurally cannot invent an answer that
was not in the option set. Never design a flow that needs it to write text.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

__all__ = [
    "MAX_OPTIONS",
    "Answer",
    "JevError",
    "noul",
    "score",
    "choice",
    "ask",
    "ask_many",
    "mock_enabled",
]

#: Jev's cardinality cap. Above this, chunk the options and re-rank winners.
MAX_OPTIONS = 250

DEFAULT_BASE_URL = "https://api.typesafe.ai/v1"
MODEL = "jev-latest"
ENDPOINT = "/systemone"

#: $ per million input tokens. Output tokens are free — Jev emits no text.
USD_PER_M_INPUT_TOKENS = 0.042

_DEFAULT_TIMEOUT = 60.0
_DEFAULT_CONCURRENCY = 16
_RETRY_STATUS = frozenset({408, 429, 500, 502, 503, 504})
_MAX_RETRIES = 3


class JevError(RuntimeError):
    """Raised when the API rejects a request or returns an unusable body."""


# --------------------------------------------------------------------------
# answers
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Answer:
    """One typed judgment.

    `value` is a probability in [0, 1] for a noul, a float position along
    `levels` for a score, and an option key for a choice. `confidence` is
    Jev's own reliability estimate for the pick, which is what the review
    queue sorts on and what `eval/calibrate.py` checks against ground truth.

    A noul carries no confidence on the wire — the probability *is* the
    answer — so we fill in `max(p, 1-p)`: the model's implied odds that its
    own call is right.
    """

    value: float | int | str
    confidence: float
    probabilities: dict[str, float] | None = None

    def act(self, act_above: float, review_above: float) -> str:
        """Route this answer: ``"act"``, ``"review"`` or ``"abstain"``.

        Both bounds are exclusive, as the names say. Prefer this over
        hand-rolled threshold comparisons so routing stays consistent across
        question types.
        """
        if act_above < review_above:
            raise ValueError("act_above must be >= review_above")
        if self.confidence > act_above:
            return "act"
        if self.confidence > review_above:
            return "review"
        return "abstain"

    def to_dict(self) -> dict[str, Any]:
        return {
            "value": self.value,
            "confidence": self.confidence,
            "probabilities": self.probabilities,
        }

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> Answer:
        return cls(
            value=d["value"],
            confidence=float(d["confidence"]),
            probabilities=d.get("probabilities"),
        )


# --------------------------------------------------------------------------
# question constructors
# --------------------------------------------------------------------------


def noul(
    instructions: str, true: str | None = None, false: str | None = None
) -> dict[str, Any]:
    """Probability that a condition holds. Answer is a float in [0, 1].

    `true` and `false` optionally spell out what each verdict means, which
    is worth doing whenever "yes" could be read two ways.
    """
    q: dict[str, Any] = {"type": "noul", "instructions": instructions}
    if true is not None or false is not None:
        q["criteria"] = {"true": true, "false": false}
    return q


def score(instructions: str, levels: Sequence[str]) -> dict[str, Any]:
    """Rate against an *ordered* rubric.

    The answer is a float position along `levels`, not a winning index:
    Jev returns the probability-weighted mean, so a line sitting between
    "steady" and "engaged" comes back as 2.4 and keeps that resolution.
    """
    levels = list(levels)
    if len(levels) < 2:
        raise ValueError("score needs at least two levels")
    if len(levels) > MAX_OPTIONS:
        raise ValueError(f"score exceeds MAX_OPTIONS={MAX_OPTIONS}")
    return {"type": "score", "instructions": instructions, "criteria": levels}


def choice(
    instructions: str,
    options: Mapping[str, str] | Sequence[str],
    add_none: bool = True,
) -> dict[str, Any]:
    """Pick one unordered option key.

    Options are names mapped to descriptions of when each applies; a bare
    sequence of names works too. `add_none` injects a `none` option so the
    model can decline — pass False only when one option must always apply.
    """
    if isinstance(options, Mapping):
        opts = {str(k): str(v) for k, v in options.items()}
    else:
        opts = {str(k): str(k) for k in options}
    if not opts:
        raise ValueError("choice needs at least one option")
    if add_none:
        opts.setdefault("none", "none of these apply")
    if len(opts) > MAX_OPTIONS:
        raise ValueError(
            f"choice has {len(opts)} options, over MAX_OPTIONS={MAX_OPTIONS}; "
            "chunk the options and re-rank the winners"
        )
    return {"type": "choice", "instructions": instructions, "criteria": opts}


# --------------------------------------------------------------------------
# transport
# --------------------------------------------------------------------------


def mock_enabled() -> bool:
    """True when `JEV_MOCK=1`, which runs the whole pipeline with no key."""
    return os.environ.get("JEV_MOCK", "").strip() not in ("", "0", "false", "False")


def _base_url() -> str:
    return os.environ.get("TYPESAFE_BASE_URL", DEFAULT_BASE_URL).rstrip("/")


def _headers() -> dict[str, str]:
    key = os.environ.get("TYPESAFE_API_KEY", "").strip()
    if not key:
        raise JevError(
            "TYPESAFE_API_KEY is unset. Jev is early access and waitlisted; "
            "set JEV_MOCK=1 to run the pipeline against the local mock judge."
        )
    return {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}


async def ask(
    state: Mapping[str, Any],
    questions: Mapping[str, Mapping[str, Any]],
    client: Any | None = None,
) -> dict[str, Answer]:
    """Ask every question in `questions` about one shared `state`.

    One request, many questions — per-request overhead dominates, so fifty
    namespaced questions cost barely more than one. Never loop this per
    question.
    """
    if not questions:
        return {}
    if mock_enabled():
        return _mock(state, questions)

    body = {"model": MODEL, "state": state, "questions": dict(questions)}
    owned = client is None
    if owned:
        import httpx

        client = httpx.AsyncClient(timeout=_DEFAULT_TIMEOUT)
    try:
        payload = await _post(client, body)
    finally:
        if owned:
            await client.aclose()
    return _parse(payload, questions)


async def ask_many(
    batches: Sequence[tuple[Mapping[str, Any], Mapping[str, Mapping[str, Any]]]],
) -> list[dict[str, Answer]]:
    """Run many `(state, questions)` batches concurrently, order preserved.

    Bounded by `JEV_CONCURRENCY` (default 16) so a long interview does not
    open six hundred sockets at once.
    """
    if not batches:
        return []
    limit = max(1, int(os.environ.get("JEV_CONCURRENCY", _DEFAULT_CONCURRENCY)))
    sem = asyncio.Semaphore(limit)

    if mock_enabled():
        async def run_mock(batch):
            async with sem:
                return _mock(batch[0], batch[1])

        return list(await asyncio.gather(*(run_mock(b) for b in batches)))

    import httpx

    async with httpx.AsyncClient(timeout=_DEFAULT_TIMEOUT) as client:

        async def run(batch):
            async with sem:
                return await ask(batch[0], batch[1], client)

        return list(await asyncio.gather(*(run(b) for b in batches)))


async def _post(client: Any, body: Mapping[str, Any]) -> Mapping[str, Any]:
    url = _base_url() + ENDPOINT
    headers = _headers()
    last: Exception | None = None
    for attempt in range(_MAX_RETRIES):
        try:
            resp = await client.post(url, json=body, headers=headers)
        except Exception as exc:  # network flake
            last = exc
        else:
            if resp.status_code < 300:
                try:
                    return resp.json()
                except Exception as exc:
                    raise JevError(f"Jev returned non-JSON: {resp.text[:200]}") from exc
            if resp.status_code not in _RETRY_STATUS:
                raise JevError(f"Jev HTTP {resp.status_code}: {resp.text[:200]}")
            last = JevError(f"Jev HTTP {resp.status_code}: {resp.text[:200]}")
        await asyncio.sleep(0.5 * (2**attempt))
    raise JevError(f"Jev unreachable after {_MAX_RETRIES} attempts: {last}")


# --------------------------------------------------------------------------
# response parsing — deliberately forgiving, see module docstring
# --------------------------------------------------------------------------


def _parse(
    payload: Mapping[str, Any],
    questions: Mapping[str, Mapping[str, Any]],
) -> dict[str, Answer]:
    raw = payload.get("answers")
    if raw is None:
        raw = payload.get("results")
    if raw is None:
        raise JevError(f"no 'answers' or 'results' in Jev response: {list(payload)}")
    if isinstance(raw, list):  # some builds return a positional array
        raw = dict(zip(questions.keys(), raw))
    out: dict[str, Answer] = {}
    for key, spec in questions.items():
        if key not in raw:
            raise JevError(f"Jev did not answer question {key!r}")
        out[key] = _one(raw[key], spec, key)
    return out


def _one(raw: Any, spec: Mapping[str, Any], key: str) -> Answer:
    kind = spec.get("type")
    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        # bare scalar: a noul probability, or a score position
        if kind == "noul":
            p = _clamp01(float(raw))
            return Answer(p, max(p, 1.0 - p))
        if kind == "score":
            return Answer(_as_position(raw, spec["criteria"], key), 1.0)
        raise JevError(f"bare scalar answer for {kind!r} question {key!r}")
    if isinstance(raw, str):
        if kind == "score":
            return Answer(_as_position(raw, spec["criteria"], key), 1.0)
        if kind == "choice":
            return Answer(_as_option(raw, spec["criteria"], key), 1.0)
        raise JevError(f"string answer for {kind!r} question {key!r}")
    if not isinstance(raw, Mapping):
        raise JevError(f"unreadable answer for {key!r}: {raw!r}")

    probs = raw.get("probabilities") or raw.get("distribution")
    probs = {str(k): float(v) for k, v in probs.items()} if isinstance(probs, Mapping) else None
    conf = raw.get("confidence")
    confidence = _clamp01(float(conf)) if conf is not None else None

    if kind == "noul":
        val = _first_present(raw, ("noul", "value", "answer", "probability", "p"))
        if val is None:
            raise JevError(f"no value in answer for {key!r}: {raw!r}")
        p = _clamp01(float(val))
        # A noul ships no confidence: the probability is the whole answer.
        return Answer(p, confidence if confidence is not None else max(p, 1.0 - p), probs)

    if kind == "score":
        levels = list(spec["criteria"])
        val = _first_present(raw, ("score", "value", "answer", "index"))
        if val is None:
            raise JevError(f"no value in answer for {key!r}: {raw!r}")
        position = _as_position(val, levels, key)
        # probabilities come keyed by level index; relabel for readability
        probs = _relabel(probs, levels)
        return Answer(position, confidence if confidence is not None else 1.0, probs)

    if kind == "choice":
        val = _first_present(raw, ("choice", "value", "answer", "option"))
        if val is None:
            raise JevError(f"no value in answer for {key!r}: {raw!r}")
        picked = _as_option(val, spec["criteria"], key)
        if confidence is None:
            confidence = _clamp01(float(probs[picked])) if probs and picked in probs else 1.0
        return Answer(picked, confidence, probs)

    raise JevError(f"unknown question type {kind!r} for {key!r}")


def _first_present(d: Mapping[str, Any], names: Sequence[str]) -> Any:
    for n in names:
        if n in d and d[n] is not None:
            return d[n]
    return None


def _as_position(val: Any, levels: Sequence[str], key: str) -> float:
    """A float position along the rubric, clamped to it."""
    if isinstance(val, str):
        if val in levels:
            return float(levels.index(val))
        raise JevError(f"answer {val!r} for {key!r} is not one of {list(levels)}")
    return max(0.0, min(float(len(levels) - 1), float(val)))


def _as_option(val: Any, options: Mapping[str, str], key: str) -> str:
    keys = list(options)
    if isinstance(val, str):
        if val in options:
            return val
        raise JevError(f"answer {val!r} for {key!r} is not one of {keys}")
    i = int(round(float(val)))
    if 0 <= i < len(keys):
        return keys[i]
    raise JevError(f"option index {val!r} out of range for {key!r}")


def _relabel(probs: Mapping[str, float] | None, levels: Sequence[str]) -> dict[str, float] | None:
    """`{"0": 0.1, "2": 0.8}` → `{"flat": 0.1, "steady": 0.8}`."""
    if not probs:
        return None
    out = {}
    for k, v in probs.items():
        if k.isdigit() and int(k) < len(levels):
            out[levels[int(k)]] = float(v)
        else:
            out[k] = float(v)
    return out


def _clamp01(x: float) -> float:
    return 0.0 if x < 0.0 else 1.0 if x > 1.0 else x


# --------------------------------------------------------------------------
# mock judge — JEV_MOCK=1
# --------------------------------------------------------------------------
#
# Mock mode is not a test fixture bolted on the side; it is how this pipeline
# is developed, since Jev is waitlisted. Every new question type needs a
# branch here. Answers are derived from blake2b over the state, the question
# key and the instructions, so they are deterministic and stable across
# processes — two runs on the same footage give the same cut.


def _digest(state: Mapping[str, Any], key: str, spec: Mapping[str, Any]) -> bytes:
    h = hashlib.blake2b(digest_size=16)
    h.update(json.dumps(state, sort_keys=True, separators=(",", ":"), default=str).encode())
    h.update(b"\x00")
    h.update(key.encode())
    h.update(b"\x00")
    h.update(str(spec.get("instructions", "")).encode())
    return h.digest()


def _u01(digest: bytes, slot: int) -> float:
    """A stable float in [0, 1) from four bytes of `digest`."""
    chunk = digest[slot * 4 : slot * 4 + 4]
    return int.from_bytes(chunk, "big") / 2**32


def _mock(
    state: Mapping[str, Any],
    questions: Mapping[str, Mapping[str, Any]],
) -> dict[str, Answer]:
    out: dict[str, Answer] = {}
    for key, spec in questions.items():
        d = _digest(state, key, spec)
        kind = spec.get("type")
        # confidence lives in [0.55, 1.0): high enough to be usable, spread
        # enough that the review queue has a real ordering to expose.
        conf = 0.55 + 0.45 * _u01(d, 1)
        if kind == "noul":
            # matching the real shape, a noul states no confidence of its own
            p = _u01(d, 0)
            out[key] = Answer(p, max(p, 1.0 - p), {"true": p, "false": 1.0 - p})
        elif kind == "score":
            levels = list(spec["criteria"])
            position = _u01(d, 0) * (len(levels) - 1)
            out[key] = Answer(position, conf, _spread(levels, round(position), d))
        elif kind == "choice":
            keys = list(spec["criteria"])
            i = min(len(keys) - 1, int(_u01(d, 0) * len(keys)))
            out[key] = Answer(keys[i], conf, _spread(keys, i, d))
        else:
            raise JevError(f"mock judge has no branch for question type {kind!r}")
    return out


def _spread(labels: Sequence[str], winner: int, digest: bytes) -> dict[str, float]:
    """A plausible normalized distribution peaked on `winner`."""
    weights = [_u01(digest, 2 + (i % 2)) * 0.4 + 0.05 for i in range(len(labels))]
    weights[winner] = 1.0
    total = sum(weights)
    return {label: w / total for label, w in zip(labels, weights)}
