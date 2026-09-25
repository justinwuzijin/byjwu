"""Map each candidate to one typed action, then run it through the gate.

No durations are computed here. The choice is the edit Jev would consider.
The noul is the probability that acting would damage the story. Confidence
is the choice's own confidence. ``gates.route`` decides whether that call is
an unattended proposal, a review, or an escalation. The raw action stays on
the proposal either way.
"""

from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction

from cutmcp.jev import choice, noul

from .apply import Deletion
from .candidates import Candidate
from .errors import ConductorError
from .gates import Gates, route
from .jev import ACTIONS, BatchResult, ask
from .passes import is_creative
from .taste import Taste
from .timeutil import short_clock

WINDOW = 8

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


def questions_for(candidate_id: str) -> dict:
    """The two questions for one candidate. ``none`` is not an option."""
    action = choice(
        f"Candidate {candidate_id} is one object in state.candidates. "
        "Which edit should the editor consider for that candidate, given the brief "
        "and state.taste? A person will see this as a marker. "
        "Nothing is applied automatically.",
        {
            "keep": "Leave the timeline alone. The moment earns its length.",
            "tighten": "Trim the dead air or filler but keep the surrounding thought.",
            "remove": "Lift this region out. It does not earn its time.",
            "mark_review": "A human should look. The signal is real but the call is not safe to trust.",
            "escalate": "Stop and discuss. The moment may be load-bearing, or the risk is high.",
        },
        add_none=False,
    )
    risk = noul(
        f"Candidate {candidate_id}: would cutting or tightening this region "
        "damage the story or clip off a thought the brief still needs?",
        true="Cutting risks losing meaning, a reaction, or a breath the edit needs.",
        false="Cutting is safe. The region is dead air, a flash frame, or disposable filler.",
    )
    return {f"{candidate_id}_action": action, f"{candidate_id}_risk": risk}


def judge(
    candidates: list[Candidate],
    brief: str,
    *,
    live: bool,
    taste: Taste,
    gates: Gates | None = None,
) -> tuple[list[Proposal], list[dict]]:
    """Judge candidates in windows. Returns proposals (input order) and receipts."""
    gates = gates or taste.gates
    proposals: list[Proposal] = []
    receipts: list[dict] = []
    for window in _windows(candidates, WINDOW):
        state = {
            "brief": brief,
            "taste": taste.to_state(),
            "candidates": [item.to_state() for item in window],
        }
        questions: dict = {}
        for item in window:
            questions.update(questions_for(item.id))
        batch = ask(state, questions, live=live)
        receipts.append(_receipt(batch, state, questions))
        for item in window:
            proposals.append(_proposal(item, batch, gates, taste))
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


def _proposal(candidate: Candidate, batch: BatchResult, gates: Gates, taste: Taste) -> Proposal:
    action_answer = batch.answers[f"{candidate.id}_action"]
    risk_answer = batch.answers[f"{candidate.id}_risk"]
    raw = str(action_answer.value)
    if raw not in ACTIONS:
        raw = "mark_review"
    adjustment = taste.adjust(candidate.kind, candidate.signals, float(action_answer.confidence), gates)
    confidence = adjustment.confidence
    risk = float(risk_answer.value)
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
            confidence_raw=adjustment.confidence_raw,
            taste_reason=adjustment.reason,
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
        confidence_raw=adjustment.confidence_raw,
        taste_reason=adjustment.reason,
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
    ]
    if abs(confidence_raw - confidence) > 1e-9:
        parts.append(f"confidence_raw={confidence_raw:.2f}")
    if taste_reason:
        parts.append(f"taste={_trim(taste_reason.replace('|', '/'), 180)}")
    if disposition in {"review", "escalate"}:
        parts.append("needs_human=yes")
    return " | ".join(parts)


def _receipt(batch: BatchResult, state: dict, questions: dict) -> dict:
    return {
        "dry_run": batch.dry_run,
        "provider": batch.provider,
        "model": batch.model,
        "endpoint": batch.endpoint,
        "request_id": batch.request_id,
        "usage": batch.usage,
        "state": state,
        "questions": questions,
        "answers": {key: answer.to_dict() for key, answer in batch.answers.items()},
    }


def _windows(items: list[Candidate], size: int):
    for start in range(0, len(items), size):
        yield items[start : start + size]


def _trim(text: str, limit: int) -> str:
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"
