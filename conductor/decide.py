"""Map each candidate to one typed action, then run it through the gate.

No durations are computed here. :class:`conductor.router.Router` decides who
answers: Jev for linear kinds (a choice plus a risk noul), Opus for creative
ones (the same action and risk, as a validated JSON object). Confidence is
the engine's own. ``gates.route`` decides whether that call is an unattended
proposal, a review, or an escalation. The raw action stays on the proposal
either way, and so does the engine that made it.

A creative call Opus could not make is a review marker with no action.

Retake grouping follows Descript, Gling, Selects, and ButterCut; the taste
call only picks or vetoes, and word-boundary hygiene runs before apply.
"""

from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction

from .apply import Deletion
from .candidates import Candidate
from .errors import ConductorError
from .gates import Gates, route
from .passes import is_creative
from .router import Ledger, Router, Verdict, questions_for
from .taste import Taste
from .timeutil import short_clock

__all__ = ["Proposal", "deletions_for", "judge", "questions_for", "word_cuts"]

ACTION_COLOR = {
    "keep": "green",
    "tighten": "blue",
    "remove": "red",
    "mark_review": "orange",
    "escalate": "purple",
}

ACTION_TITLE = {
    "keep": "keep",
    "tighten": "tighten",
    "remove": "remove",
    "mark_review": "review",
    "escalate": "escalate",
}


@dataclass
class Proposal:
    candidate_id: str
    raw_action: str
    action: str
    disposition: str
    pass_name: str
    confidence: float
    risk: float
    color: str
    needs_human: bool
    eligible: bool
    marker_name: str | None
    marker_note: str | None
    confidence_raw: float = 0.0
    taste_reason: str | None = None
    engine: str = "jev"
    engine_source: str = "mock"
    decision_type: str = ""
    engine_why: str = ""
    engine_detail: str = ""
    engine_model: str | None = None
    rationale: str = ""
    cached: bool = False

    def attribution(self) -> dict:
        return {
            "engine": self.engine,
            "engine_source": self.engine_source,
            "decision_type": self.decision_type,
            "engine_why": self.engine_why,
            "engine_detail": self.engine_detail,
            "engine_model": self.engine_model,
            "rationale": self.rationale,
            "cached": self.cached,
        }


def judge(
    candidates: list[Candidate],
    brief: str,
    *,
    live: bool,
    taste: Taste,
    gates: Gates | None = None,
    router: Router | None = None,
    ledger: Ledger | None = None,
) -> tuple[list[Proposal], list[dict]]:
    """Judge candidates through the router. Returns proposals (input order) and receipts.

    Pass a ``router`` to share its cache and engine health (``iterate`` does).
    Otherwise one is opened for this call with ``live`` and closed after.
    """
    gates = gates or taste.gates
    owned = router is None
    if router is None:
        router = Router(live=live)
    try:
        verdicts, receipts = router.judge_candidates(
            candidates, brief=brief, taste=taste.to_state(), ledger=ledger
        )
    finally:
        if owned:
            router.close()
    proposals = [_proposal(item, verdicts[item.id], gates, taste) for item in candidates]
    return proposals, receipts


def deletions_for(
    proposals: list[Proposal],
    candidates: list[Candidate],
    *,
    accept: list[str] | None,
    min_confidence: float | None,
    passes: list[str] | None,
    hold: Fraction,
) -> list[Deletion]:
    """The cuts an apply is allowed to perform.

    ``--accept`` performs the raw tighten/remove for those ids, including
    review calls a person has signed. The confidence path performs only
    ``auto`` dispositions in the named passes. One of the two is required.
    """
    by_id = {item.candidate_id: item for item in proposals}
    candidates_by_id = {item.id: item for item in candidates}
    if accept:
        return [_accepted(cid, by_id, candidates_by_id, hold) for cid in accept]
    if min_confidence is None or not passes:
        raise ConductorError(
            "apply needs --accept, or both --min-confidence and --pass. "
            "Shadow mode is the default; cuts are never implied."
        )
    chosen: list[Deletion] = []
    for proposal in proposals:
        candidate = candidates_by_id[proposal.candidate_id]
        if candidate.pass_name not in passes:
            continue
        if proposal.disposition != "auto":
            continue
        if proposal.confidence < min_confidence:
            continue
        if proposal.raw_action not in {"tighten", "remove"}:
            continue
        chosen.append(_deletion(candidate, proposal.raw_action, hold))
    if not chosen:
        raise ConductorError(
            "no cuts matched the confidence gate. Creative passes are not "
            "auto-applied; pass --accept with candidate ids to cut a review call."
        )
    return chosen


def _accepted(candidate_id, proposals, candidates, hold: Fraction) -> Deletion:
    proposal = proposals.get(candidate_id)
    candidate = candidates.get(candidate_id)
    if proposal is None or candidate is None:
        known = ", ".join(sorted(candidates)) or "(none)"
        raise ConductorError(f"unknown candidate {candidate_id!r}. This run has: {known}")
    if proposal.raw_action not in {"tighten", "remove"}:
        raise ConductorError(
            f"{candidate_id} is a {proposal.raw_action} call, not a tighten or remove, "
            "so there is no cut to apply"
        )
    return _deletion(candidate, proposal.raw_action, hold)


def word_cuts(
    words: list,
    *,
    sequence: str,
    frame_duration: Fraction,
    router: Router | None = None,
    profile=None,
    vetoes: dict | None = None,
    picks: dict | None = None,
) -> tuple[list[Deletion], list[dict]]:
    """Retake and filler cuts after vetoes, then word-boundary hygiene.

    A clip with no word timings contributes nothing. Taste and veto go
    through the router when one is passed. ``vetoes`` and ``picks`` are
    fixtures the logic honors before the router.
    """
    from .hygiene import hygienize
    from .rules import apply_router, plan_retakes, retake_asks
    from .style import StyleProfile

    profile = profile or StyleProfile()
    if not words:
        return [], []
    plan = plan_retakes(words, profile=profile, vetoes=vetoes, picks=picks)
    if router is not None and not vetoes and not picks:
        asks = retake_asks(plan)
        if asks:
            decisions, _receipts = router.decide(asks)
            plan = apply_router(plan, decisions)
    raw = list(plan.cuts)
    raw.extend(item for item in plan.filler_proposals if item.get("auto"))
    cleaned = hygienize(raw, words, profile=profile, frame_duration=frame_duration)
    deletions = [
        Deletion(
            candidate_id=item["id"],
            sequence=sequence,
            start=item["start"],
            end=item["end"],
            action="remove",
            pass_name="dialogue",
            transition=item.get("transition"),
        )
        for item in cleaned
        if item["end"] > item["start"]
    ]
    return deletions, plan.markers


def _deletion(candidate: Candidate, action: str, hold: Fraction) -> Deletion:
    if candidate.span == "note":
        raise ConductorError(
            f"{candidate.id} is an editor's note ({candidate.kind}), not a range to lift"
        )
    start = candidate.timeline_start
    end = candidate.timeline_end
    if candidate.span == "clip" and action == "tighten":
        if candidate.duration <= hold:
            raise ConductorError(
                f"{candidate.id} is already within the hold ({hold}s); nothing to trim"
            )
        start = candidate.timeline_start + hold
    if end <= start:
        raise ConductorError(f"{candidate.id} has an empty cut")
    return Deletion(
        candidate_id=candidate.id,
        sequence=candidate.sequence,
        start=start,
        end=end,
        action=action,
        pass_name=candidate.pass_name,
    )


def _proposal(candidate: Candidate, verdict: Verdict, gates: Gates, taste: Taste) -> Proposal:
    raw = verdict.action
    risk = float(verdict.risk)
    if verdict.source == "unavailable":
        confidence = confidence_raw = float(verdict.confidence)
        taste_reason = None
        action, disposition = "mark_review", "review"
    else:
        adjustment = taste.adjust(candidate.kind, candidate.signals, float(verdict.confidence), gates)
        confidence = adjustment.confidence
        confidence_raw = adjustment.confidence_raw
        taste_reason = adjustment.reason
        kind_gates = Gates(
            auto_confidence=adjustment.auto_confidence,
            review_confidence=gates.review_confidence,
            auto_risk_max=gates.auto_risk_max,
        )
        action, disposition = route(
            raw,
            confidence,
            risk,
            creative=is_creative(candidate.pass_name or "mechanical"),
            gates=kind_gates,
        )
        if disposition == "auto" and verdict.source == "rules":
            action, disposition = "mark_review", "review"
    human = disposition in {"review", "escalate"}
    name = None
    note = None
    if action != "keep":
        name = (
            f"CC {ACTION_TITLE[action]} · {candidate.label} @ "
            f"{short_clock(candidate.timeline_start)}"
        )
        note = _note(
            candidate,
            raw,
            action,
            disposition,
            confidence,
            risk,
            confidence_raw=confidence_raw,
            taste_reason=taste_reason,
            verdict=verdict,
        )
    return Proposal(
        candidate_id=candidate.id,
        raw_action=raw,
        action=action,
        disposition=disposition,
        pass_name=candidate.pass_name,
        confidence=confidence,
        risk=risk,
        color=ACTION_COLOR[action],
        needs_human=human,
        eligible=disposition == "auto",
        marker_name=name,
        marker_note=note,
        confidence_raw=confidence_raw,
        taste_reason=taste_reason,
        engine=verdict.engine,
        engine_source=verdict.source,
        decision_type=verdict.decision_type,
        engine_why=verdict.why,
        engine_detail=verdict.detail,
        engine_model=verdict.model,
        rationale=verdict.rationale,
        cached=verdict.cached,
    )


def _note(
    candidate: Candidate,
    raw: str,
    action: str,
    disposition: str,
    confidence: float,
    risk: float,
    *,
    confidence_raw: float,
    taste_reason: str | None,
    verdict: Verdict,
) -> str:
    from .timeutil import clock

    parts = [
        "Cut Conductor shadow proposal. No edit was applied.",
        f"color={ACTION_COLOR[action]}",
        f"action={action}",
        f"raw_action={raw}",
        f"disposition={disposition}",
        f"pass={candidate.pass_name}",
        f"confidence={confidence:.2f}",
        f"risk={risk:.2f}",
        f"id={candidate.id}",
        f"kind={candidate.kind}",
        f"range={clock(candidate.timeline_start)}-{clock(candidate.timeline_end)}",
        f"why={_trim(candidate.reason, 160)}",
        f"engine={verdict.engine}/{verdict.source}{'/cached' if verdict.cached else ''}",
        f"decision={verdict.decision_type}",
        f"routed={_trim(verdict.why, 120)}",
    ]
    if abs(confidence_raw - confidence) > 1e-9:
        parts.append(f"confidence_raw={confidence_raw:.2f}")
    if taste_reason:
        parts.append(f"taste={_trim(taste_reason.replace('|', '/'), 180)}")
    if verdict.rationale:
        parts.append(f"{verdict.engine}_says={_trim(verdict.rationale, 160)}")
    if verdict.detail:
        parts.append(f"engine_note={_trim(verdict.detail, 160)}")
    if disposition in {"review", "escalate"}:
        parts.append("needs_human=yes")
    return " | ".join(parts)


def _trim(text: str, limit: int) -> str:
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"
