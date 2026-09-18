"""Tier 2 — probabilistic judgment. Never measures anything.

Five typed questions per segment, batched one request per window of ten with
namespaced keys. Nothing here counts frames, adds durations or formats a
timecode: every number in the state was measured by tier 1 and is passed
through untouched, and every number out is a score tier 3 consumes.

The one formula that lives here is `Decision.value`. That is deliberate —
`value` and `cut_penalty` are the only two places taste is allowed to exist.
Editorial preference must never be smuggled into question text, because a
knob the user can slide beats a sentence they have to rewrite.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from math import ceil
from typing import Any

from . import jev
from .extract import Media, Segment, cache_dir

__all__ = [
    "WINDOW",
    "CONTEXT",
    "ENERGY_LEVELS",
    "CUT_LEVELS",
    "ROLES",
    "Decision",
    "decide",
    "run",
    "cost_estimate",
]

#: Segments judged per request. One request per window, never per question:
#: per-request overhead dominates, so fifty questions cost barely more than
#: one.
WINDOW = 10

#: Neighbours included in the state on either side of a window so the model
#: can judge continuity. They are shown, not judged.
CONTEXT = 2

ENERGY_LEVELS: tuple[str, ...] = ("flat", "low", "steady", "engaged", "electric")
CUT_LEVELS: tuple[str, ...] = ("mid-thought", "awkward", "acceptable", "clean", "perfect")

ROLES: dict[str, str] = {
    "hook": "opens or re-opens attention",
    "claim": "asserts the speaker's point",
    "evidence": "an example, anecdote or number backing a claim",
    "context": "setup or background the audience needs to follow what's next",
    "aside": "a tangent, caveat or digression",
    "dead": "contributes nothing",
}

#: Bumped whenever question wording changes, so cached tier-2 output for an
#: older question set is not silently reused.
_SCHEMA = "1"

#: Measured against a real request's reported `usage`: a 2,374-character
#: payload billed 1,059 input tokens. JSON tokenizes far denser than the
#: usual four-characters-per-token rule of thumb, which under-reported cost
#: by 1.8x — the wrong direction for a number you decide to spend on.
_CHARS_PER_TOKEN = 2.25


@dataclass
class Decision:
    """Every judgment Jev made about one segment."""

    idx: int
    keep: float
    filler: float
    energy: float
    cutq: float
    cutq_conf: float
    role: str
    role_conf: float

    @property
    def value(self) -> float:
        """The scalar tier 3's knapsack maximizes.

        Relevance, discounted by the odds it is filler, modulated by
        delivery. Energy moves value by at most ±17.5% — it breaks ties
        between comparable lines, it does not outrank relevance.
        """
        return (
            self.keep
            * (1.0 - self.filler)
            * (0.65 + 0.35 * self.energy / (len(ENERGY_LEVELS) - 1))
        )

    @property
    def cut_quality(self) -> str:
        """The nearest rubric label. `cutq` itself sits between levels."""
        return CUT_LEVELS[int(round(self.cutq))]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> Decision:
        return cls(**{k: d[k] for k in cls.__dataclass_fields__})


# --------------------------------------------------------------------------
# questions and state
# --------------------------------------------------------------------------


def _key(idx: int, name: str) -> str:
    return f"s{idx:05d}_{name}"


def _questions(judged: Sequence[Segment]) -> dict[str, dict[str, Any]]:
    """The five questions, for every segment under judgment in this window."""
    qs: dict[str, dict[str, Any]] = {}
    for seg in judged:
        i = seg.idx
        qs[_key(i, "keep")] = jev.noul(
            f"Line {i}: does this line belong in a cut that serves the brief?"
        )
        qs[_key(i, "filler")] = jev.noul(
            f"Line {i}: is this a false start, a restart, a verbal tic, or a "
            f"repetition of something already said in the window?"
        )
        qs[_key(i, "energy")] = jev.score(
            f"Line {i}: how energetic is the delivery?", ENERGY_LEVELS
        )
        qs[_key(i, "cutq")] = jev.score(
            f"Line {i}: how clean would a cut placed immediately AFTER this "
            f"line feel to a viewer?",
            CUT_LEVELS,
        )
        qs[_key(i, "role")] = jev.choice(
            f"Line {i}: what role does this line play?", ROLES, add_none=False
        )
    return qs


def _state(brief: str, window: Sequence[Segment], judged: set[int]) -> dict[str, Any]:
    """Shared state for one request. Every number here came from tier 1."""
    return {
        "brief": brief,
        "transcript_window": [
            {
                "id": seg.idx,
                "t": seg.start,
                "speaker": seg.speaker,
                "pause_before": seg.gap_before,
                "text": seg.text,
                "under_judgment": seg.idx in judged,
            }
            for seg in window
        ],
    }


def _batches(
    media: Media, brief: str
) -> list[tuple[dict[str, Any], dict[str, dict[str, Any]]]]:
    """One `(state, questions)` pair per window of judgeable segments.

    Trivially-filler segments are dropped before windowing, so they never
    appear in a request — not as a question, not as context.
    """
    judgeable = [s for s in media.segments if not s.is_trivial_filler]
    out = []
    for start in range(0, len(judgeable), WINDOW):
        window_segs = judgeable[start : start + WINDOW]
        lo = max(0, start - CONTEXT)
        hi = min(len(judgeable), start + WINDOW + CONTEXT)
        with_context = judgeable[lo:hi]
        judged_ids = {s.idx for s in window_segs}
        out.append((_state(brief, with_context, judged_ids), _questions(window_segs)))
    return out


# --------------------------------------------------------------------------
# running
# --------------------------------------------------------------------------


async def decide(media: Media, brief: str, use_cache: bool = True) -> list[Decision]:
    """Judge every segment of `media` against `brief`. One Decision each."""
    cache = _cache_path(media.media_id, brief)
    if use_cache and cache.exists():
        return [Decision.from_dict(d) for d in json.loads(cache.read_text())]

    batches = _batches(media, brief)
    results = await jev.ask_many(batches)

    answers: dict[str, jev.Answer] = {}
    for r in results:
        answers.update(r)

    decisions = [
        _synthetic(seg) if seg.is_trivial_filler else _from_answers(seg, answers)
        for seg in media.segments
    ]
    decisions.sort(key=lambda d: d.idx)
    if use_cache:
        cache.write_text(json.dumps([d.to_dict() for d in decisions], indent=2, sort_keys=True))
    return decisions


def run(media: Media, brief: str, use_cache: bool = True) -> list[Decision]:
    """Sync wrapper around `decide`."""
    return asyncio.run(decide(media, brief, use_cache=use_cache))


def _from_answers(seg: Segment, answers: Mapping[str, jev.Answer]) -> Decision:
    def a(name: str) -> jev.Answer:
        k = _key(seg.idx, name)
        if k not in answers:
            raise KeyError(f"Jev returned no answer for {k}")
        return answers[k]

    cutq = a("cutq")
    role = a("role")
    return Decision(
        idx=seg.idx,
        keep=float(a("keep").value),
        filler=float(a("filler").value),
        energy=float(a("energy").value),
        cutq=float(cutq.value),
        cutq_conf=float(cutq.confidence),
        role=str(role.value),
        role_conf=float(role.confidence),
    )


def _synthetic(seg: Segment) -> Decision:
    """A regex-certain verdict for regex-obvious filler — no request spent.

    A bare "um" is dead and cutting straight after it is clean, both at
    confidence 1.0, which also parks these at the bottom of the review queue
    where they belong.
    """
    return Decision(
        idx=seg.idx,
        keep=0.0,
        filler=1.0,
        energy=0.0,
        cutq=float(CUT_LEVELS.index("clean")),
        cutq_conf=1.0,
        role="dead",
        role_conf=1.0,
    )


# --------------------------------------------------------------------------
# cost
# --------------------------------------------------------------------------


def cost_estimate(media: Media, brief: str) -> dict[str, Any]:
    """What this brief will cost to decide, before spending anything.

    Token count is measured off the exact payloads `decide` would send, at a
    characters-per-token rate calibrated against real reported usage. Output
    tokens are free — Jev emits no text.
    """
    batches = _batches(media, brief)
    chars = sum(
        len(json.dumps(state, separators=(",", ":")))
        + len(json.dumps(questions, separators=(",", ":")))
        for state, questions in batches
    )
    tokens = ceil(chars / _CHARS_PER_TOKEN)
    return {
        "segments": len(media.segments),
        "requests": len(batches),
        "questions": sum(len(q) for _, q in batches),
        "approx_input_tokens": tokens,
        "approx_usd": round(tokens / 1_000_000 * jev.USD_PER_M_INPUT_TOKENS, 6),
    }


def _cache_path(media_id: str, brief: str):
    h = hashlib.blake2b(digest_size=8)
    h.update(brief.encode())
    h.update(f"|{_SCHEMA}|{WINDOW}|{int(jev.mock_enabled())}".encode())
    return cache_dir() / f"{media_id}.{h.hexdigest()}.decide.json"
