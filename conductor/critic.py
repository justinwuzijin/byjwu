"""Critic gate. The taste model may approve, flag a span, or veto a cut.

It reads the compact plan summary plus the lint report. It cannot add clips.
At most two rounds. When the taste engine is not live, or the call comes back
unavailable, the critic is skipped and the lint result stands.

The critic uses the router's taste engine. The default is
``grok-4.7-medium`` (``CONDUCTOR_TASTE_MODEL``). A Claude id selects Opus.
A test may pass ``critic`` to return a fixed JSON.

Ideas: EditDuet's critic (feedback or render, never a new shot), VlogReward's
1–5 rubric on the JSON plan, CutClaw's reviewer gate. No code copied.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from . import markers
from .lint import Finding, LintReport, lint_plan
from .plan import EditPlan
from .router import Ask, Router
from .schema import SchemaError, validate

MAX_ROUNDS = 2
DECISION_TYPE = "timeline_critic"

CRITIC_SCHEMA: dict = {
    "type": "object",
    "additionalProperties": False,
    "required": ["action", "scores", "span", "cut_id", "reason"],
    "properties": {
        "action": {"type": "string", "enum": ["approve", "flag", "veto"]},
        "scores": {
            "type": "object",
            "additionalProperties": False,
            "required": ["pacing", "clip_selection", "visual_script", "story_arc"],
            "properties": {
                "pacing": {"type": "integer", "minimum": 1, "maximum": 5},
                "clip_selection": {"type": "integer", "minimum": 1, "maximum": 5},
                "visual_script": {"type": "integer", "minimum": 1, "maximum": 5},
                "story_arc": {"type": "integer", "minimum": 1, "maximum": 5},
            },
        },
        "span": {"type": "array", "items": {"type": "number"}, "maxItems": 2},
        "cut_id": {"type": "string"},
        "reason": {"type": "string", "maxLength": 240},
    },
}

_SCORE_KEYS = ("pacing", "clip_selection", "visual_script", "story_arc")
CriticFn = Callable[[dict], dict]


@dataclass
class CriticTurn:
    action: str
    scores: dict
    reason: str = ""
    span: tuple[float, float] | None = None
    cut_id: str = ""
    skipped: bool = False
    model: str | None = None

    def to_dict(self) -> dict:
        return {
            "action": self.action,
            "scores": self.scores,
            "reason": self.reason,
            "span": list(self.span) if self.span else [],
            "cut_id": self.cut_id,
            "skipped": self.skipped,
            "model": self.model,
        }


@dataclass
class Review:
    report: LintReport
    turns: list[CriticTurn] = field(default_factory=list)
    vetoed: list[str] = field(default_factory=list)
    flags: list[dict] = field(default_factory=list)
    blocked: bool = False
    blocked_reason: str = ""

    def to_dict(self) -> dict:
        return {
            "blocked": self.blocked,
            "blocked_reason": self.blocked_reason,
            "lint": self.report.to_dict(),
            "critic": [turn.to_dict() for turn in self.turns],
            "vetoed": list(self.vetoed),
            "flags": list(self.flags),
        }


def review_plan(
    plan: EditPlan,
    *,
    profile: dict | None = None,
    check_media: bool = True,
    baseline: LintReport | None = None,
    router: Router | None = None,
    critic: CriticFn | None = None,
    brief: str = "",
    on_veto: Callable[[str], EditPlan] | None = None,
) -> Review:
    """Lint, then ask the critic. Hard findings that are new block export.

    ``baseline`` is the lint of the timeline before this edit. A hard finding
    with the same code and clip that was already there does not block: the
    export did not introduce it. An assembled plan has no baseline, so every
    hard finding blocks.

    ``on_veto`` rebuilds the plan without that cut and returns it. The critic
    then sees the rebuilt plan, up to :data:`MAX_ROUNDS` turns.
    """
    report = lint_plan(plan, profile=profile, check_media=check_media)
    fresh = _introduced(report, baseline)
    review = Review(report=report, blocked=bool(fresh))
    if fresh:
        review.blocked_reason = fresh[0].message
    if review.blocked:
        return review
    current = plan
    for _round in range(MAX_ROUNDS):
        turn = _turn(current, review.report, router=router, critic=critic, brief=brief)
        review.turns.append(turn)
        if turn.skipped or turn.action == "approve":
            break
        if turn.action == "flag":
            review.flags.append(
                {"span": list(turn.span) if turn.span else [], "reason": turn.reason, "cut_id": turn.cut_id}
            )
            _mark(current, turn)
            break
        if turn.action == "veto" and turn.cut_id and on_veto is not None and turn.cut_id not in review.vetoed:
            review.vetoed.append(turn.cut_id)
            current = on_veto(turn.cut_id)
            review.report = lint_plan(current, profile=profile, check_media=check_media)
            fresh = _introduced(review.report, baseline)
            review.blocked = bool(fresh)
            review.blocked_reason = fresh[0].message if fresh else ""
            if review.blocked:
                break
            continue
        break
    return review


def best_of(plans: list[EditPlan], *, critic: CriticFn, profile: dict | None = None) -> int:
    """Index of the plan with the highest critic mean. Hard failures rank last.

    The critic only ranks plans that already exist. It does not add clips.
    """
    if not plans:
        raise ValueError("best_of needs at least one plan")
    ranked: list[tuple[int, float, int]] = []
    for index, plan in enumerate(plans):
        report = lint_plan(plan, profile=profile, check_media=False)
        if report.blocked:
            ranked.append((index, -1.0, len(report.hard)))
            continue
        turn = _from_payload(critic(_payload(plan, report)))
        mean = _mean(turn.scores)
        ranked.append((index, mean, len(report.soft)))
    ranked.sort(key=lambda item: (-item[1], item[2], item[0]))
    return ranked[0][0]


def _turn(plan, report, *, router, critic, brief) -> CriticTurn:
    if critic is not None:
        try:
            return _from_payload(critic(_payload(plan, report)))
        except (SchemaError, KeyError, TypeError, ValueError) as exc:
            return CriticTurn("approve", _neutral(), reason=f"critic skipped: {exc}", skipped=True)
    if router is None or not router.live:
        return CriticTurn("approve", {}, skipped=True, reason="no taste model available")
    from .router import classify

    dtype = classify(DECISION_TYPE)
    ask = Ask(
        id="critic",
        type=DECISION_TYPE,
        subject=_payload(plan, report),
        schema=CRITIC_SCHEMA,
        question=dtype.question,
    )
    decisions, _receipts = router.decide([ask], brief=brief)
    decision = decisions[0]
    if decision.source == "unavailable" or not isinstance(decision.value, dict):
        return CriticTurn(
            "approve",
            {},
            skipped=True,
            reason=decision.detail or "taste model unavailable",
            model=decision.model,
        )
    try:
        turn = _from_payload(decision.value)
    except SchemaError as exc:
        return CriticTurn("approve", {}, skipped=True, reason=str(exc), model=decision.model)
    turn.model = decision.model
    return turn


def _from_payload(payload: dict) -> CriticTurn:
    value = validate(payload, CRITIC_SCHEMA)
    action = value["action"]
    if action not in {"approve", "flag", "veto"}:
        raise SchemaError("the critic cannot add clips")
    span = tuple(value["span"]) if len(value["span"]) == 2 else None
    if action == "flag" and (span is None or not value["reason"].strip()):
        raise SchemaError("a flag needs a span and a reason")
    if action == "veto" and not value["cut_id"].strip():
        raise SchemaError("a veto needs a cut id")
    return CriticTurn(
        action=action,
        scores={key: int(value["scores"][key]) for key in _SCORE_KEYS},
        reason=value["reason"].strip(),
        span=span,
        cut_id=value["cut_id"].strip(),
    )


def _payload(plan: EditPlan, report: LintReport) -> dict:
    return {"plan": plan.summary(), "lint": report.to_dict()}


def _introduced(report: LintReport, baseline: LintReport | None) -> list[Finding]:
    if baseline is None:
        return list(report.hard)
    seen = {(item.code, item.message) for item in baseline.hard}
    return [item for item in report.hard if (item.code, item.message) not in seen]


def _mark(plan: EditPlan, turn: CriticTurn) -> None:
    if turn.span is None:
        return
    start, _end = turn.span
    host = None
    for clip in plan.clips:
        if clip.element is None:
            continue
        if float(clip.timeline_start) <= start < float(clip.timeline_end):
            host = clip
            break
    if host is None or host.element is None:
        return
    note = f"color=orange; disposition=review; critic: {turn.reason}"
    start = host.source_start if host.source_start is not None else host.timeline_start
    markers.add_marker(
        host.element,
        start=start,
        duration=plan.frame_duration,
        value="CC review critic",
        note=note[:900],
        completed="0",
    )


def _mean(scores: dict) -> float:
    if not scores:
        return 0.0
    return sum(int(scores[key]) for key in _SCORE_KEYS) / len(_SCORE_KEYS)


def _neutral() -> dict:
    return {key: 3 for key in _SCORE_KEYS}
