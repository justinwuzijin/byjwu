"""Deterministic retake, false-start, and filler rules.

Ideas from Descript Remove Retakes, Gling bad takes, Selects (group takes,
keep the best), and ButterCut restatement trimming. Reimplemented from
public descriptions; no code copied.

A clip with no word timings is skipped and nothing is marked. The logic
picks the last complete take. A model may only veto a cut, or, when two or
more complete takes score within the style-profile margin, pick one of the
existing takes. It cannot invent a span.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from fractions import Fraction

from .style import StyleProfile

_FILLERS = frozenset({"um", "uh", "erm", "hmm"})
_AMBIGUOUS = ("you know", "basically", "like", "so")
_TOKEN = re.compile(r"[a-z0-9']+")
_SENTENCE_END = frozenset(".!?")


@dataclass(frozen=True)
class TakeSignal:
    """Optional per-take measurements. Missing values stay neutral."""

    filler_count: int | None = None
    loudness_variance: float | None = None
    clipping: bool | None = None
    speech_rate: float | None = None


@dataclass
class Utterance:
    clip_id: str
    start: Fraction
    end: Fraction
    text: str
    tokens: tuple[str, ...]
    words: tuple
    cutoff: bool = False

    @property
    def duration(self) -> Fraction:
        return self.end - self.start


@dataclass
class TakeGroup:
    clip_id: str
    takes: list[Utterance]
    complete: list[int]
    keep: int
    close: bool
    scores: list[float]
    cuts: list[dict] = field(default_factory=list)


@dataclass
class RulePlan:
    groups: list[TakeGroup]
    cuts: list[dict]
    markers: list[dict]
    filler_proposals: list[dict]


def plan_retakes(
    words: list,
    *,
    profile: StyleProfile | None = None,
    signals: dict | None = None,
    vetoes: dict | None = None,
    picks: dict | None = None,
) -> RulePlan:
    """Group retakes and mark the cuts. ``vetoes`` and ``picks`` are fixtures.

    ``vetoes`` maps a cut id to a reason. A veto drops that cut and records a
    review marker. ``picks`` maps a group index to a take index and is used
    only when that group is close. Both default to the logic answer.
    """
    profile = profile or StyleProfile()
    signals = signals or {}
    vetoes = vetoes or {}
    picks = picks or {}
    if not words or not _timed(words):
        return RulePlan([], [], [], propose_fillers([], profile=profile))
    groups: list[TakeGroup] = []
    for clip_id, clip_words in _by_clip(words).items():
        if not _timed(clip_words):
            continue
        utterances = _utterances(clip_words, profile)
        groups.extend(_groups(clip_id, utterances, profile, signals))
    cuts: list[dict] = []
    markers: list[dict] = []
    for index, group in enumerate(groups):
        if group.close and index in picks:
            chosen = picks[index]
            if chosen in group.complete:
                group.keep = chosen
        _mark_cuts(group, profile)
        kept_cuts = []
        for cut in group.cuts:
            reason = vetoes.get(cut["id"])
            if reason:
                markers.append(
                    {
                        "id": cut["id"],
                        "kind": "retake_veto",
                        "sequence": cut.get("sequence", ""),
                        "clip_id": cut["clip_id"],
                        "start": cut["start"],
                        "end": cut["end"],
                        "value": "Retake kept",
                        "note": f"Both takes kept. {reason}",
                    }
                )
                continue
            kept_cuts.append(cut)
            markers.append(
                {
                    "id": cut["id"],
                    "kind": "retake",
                    "sequence": cut.get("sequence", ""),
                    "clip_id": cut["clip_id"],
                    "start": cut["start"],
                    "end": cut["end"],
                    "value": "Retake",
                    "note": "Earlier take removed. The last complete take was kept.",
                }
            )
        group.cuts = kept_cuts
        cuts.extend(kept_cuts)
    return RulePlan(groups, cuts, markers, propose_fillers(words, profile=profile))


def propose_fillers(words: list, *, profile: StyleProfile | None = None) -> list[dict]:
    """Auto-cut um/uh/erm beside silence. Ambiguous fillers stay proposals."""
    profile = profile or StyleProfile()
    ordered = sorted(_by_clip_flat(words), key=lambda item: (item[0], item[1].start))
    proposals: list[dict] = []
    for index, (clip_id, word) in enumerate(ordered):
        token = _token(word.text)
        phrase = _phrase_at(ordered, index)
        kind = None
        auto = False
        if token in _FILLERS or token == "uh":
            kind = "filler_auto"
            auto = _silence_beside(ordered, index, profile.filler_silence)
        elif phrase:
            kind = "filler_proposal"
            auto = False
        if kind is None:
            continue
        if kind == "filler_auto" and not auto:
            continue
        start, end = _span(ordered, index, phrase)
        proposals.append(
            {
                "id": f"filler-{clip_id}-{index}",
                "kind": kind,
                "clip_id": clip_id,
                "start": start,
                "end": end,
                "text": phrase or token,
                "auto": auto if kind == "filler_auto" else False,
                "sequence": getattr(word, "sequence", ""),
            }
        )
    return proposals


def retake_asks(plan: RulePlan):
    """Router asks. Taste picks a close group. Each cut may be vetoed.

    The dry-run answer is the logic choice: last complete take, and no veto.
    """
    from .router import Ask

    asks = []
    for index, group in enumerate(plan.groups):
        if not group.close:
            continue
        options = {
            f"t{take_index}": " ".join(group.takes[take_index].tokens)
            for take_index in group.complete
        }
        keep = f"t{group.keep}"

        def _rule(ask, keep=keep):
            return keep, 0.9

        asks.append(
            Ask(
                id=f"retake-pick-{index}",
                type="retake_pick",
                subject={
                    "group": index,
                    "takes": [
                        {
                            "id": f"t{take_index}",
                            "text": " ".join(group.takes[take_index].tokens),
                            "score": group.scores[take_index],
                        }
                        for take_index in group.complete
                    ],
                },
                options=options,
                question="Pick one existing complete take. Do not invent a span.",
                rule=_rule,
                mock=lambda keep=keep: (keep, 0.9),
            )
        )
    for cut in plan.cuts:
        def _allow(ask):
            return "allow", 0.9

        asks.append(
            Ask(
                id=f"veto-{cut['id']}",
                type="retake_veto",
                subject={"cut_id": cut["id"], "text": cut.get("text", "")},
                options={
                    "allow": "The earlier take is a retake or false start and should be cut.",
                    "veto": "The repeat is intentional. Keep both takes.",
                },
                question="Veto this retake cut, or allow it.",
                rule=_allow,
                mock=lambda: ("allow", 0.9),
            )
        )
    for proposal in plan.filler_proposals:
        if proposal["auto"]:
            continue

        def _keep(ask):
            return "keep", 0.9

        asks.append(
            Ask(
                id=f"approve-{proposal['id']}",
                type="filler_approve",
                subject={"text": proposal["text"], "start": str(proposal["start"])},
                options={
                    "keep": "Leave the word. It may carry meaning.",
                    "cut": "It is disposable filler.",
                },
                question="Approve cutting this filler, or keep it.",
                rule=_keep,
                mock=lambda: ("keep", 0.9),
            )
        )
    return asks


def apply_router(plan: RulePlan, decisions: list) -> RulePlan:
    """Fold router answers into the plan. A veto becomes a review marker."""
    by_id = {item.id: item for item in decisions}
    for index, group in enumerate(plan.groups):
        decision = by_id.get(f"retake-pick-{index}")
        if decision is None or decision.value is None or not group.close:
            continue
        chosen = str(decision.value)
        for take_index in group.complete:
            if f"t{take_index}" == chosen:
                group.keep = take_index
        _mark_cuts(group, StyleProfile())
    cuts = []
    markers = []
    for cut in plan.cuts:
        decision = by_id.get(f"veto-{cut['id']}")
        if decision is not None and decision.value == "veto":
            note = decision.rationale or "The repeat looks intentional."
            markers.append(
                {
                    "id": cut["id"],
                    "kind": "retake_veto",
                    "clip_id": cut["clip_id"],
                    "start": cut["start"],
                    "end": cut["end"],
                    "value": "Retake kept",
                    "note": f"Both takes kept. {note}",
                    "sequence": cut.get("sequence", ""),
                }
            )
            continue
        cuts.append(cut)
        markers.append(
            {
                "id": cut["id"],
                "kind": "retake",
                "clip_id": cut["clip_id"],
                "start": cut["start"],
                "end": cut["end"],
                "value": "Retake",
                "note": "Earlier take removed. The last complete take was kept.",
                "sequence": cut.get("sequence", ""),
            }
        )
    approved = []
    for proposal in plan.filler_proposals:
        if proposal["auto"]:
            approved.append(proposal)
            continue
        decision = by_id.get(f"approve-{proposal['id']}")
        if decision is not None and decision.value == "cut":
            approved.append({**proposal, "auto": True})
    plan.cuts = cuts
    plan.markers = markers
    plan.filler_proposals = approved
    for group in plan.groups:
        group.cuts = [cut for cut in group.cuts if any(cut["id"] == kept["id"] for kept in cuts)]
    return plan


def _mark_cuts(group: TakeGroup, profile: StyleProfile) -> None:
    """Each discarded take, plus the gap up to the next kept word."""
    keep_take = group.takes[group.keep]
    ordered = sorted(range(len(group.takes)), key=lambda index: group.takes[index].start)
    cuts = []
    for position, index in enumerate(ordered):
        if index == group.keep:
            continue
        take = group.takes[index]
        end = take.end
        for later in ordered[position + 1 :]:
            nxt = group.takes[later]
            if later == group.keep:
                end = nxt.start
            else:
                end = nxt.start
            break
        else:
            end = take.end
        if end < take.end:
            end = take.end
        cuts.append(
            {
                "id": f"retake-{group.clip_id}-{_stamp(take.start)}",
                "kind": "retake",
                "reason": "retake",
                "clip_id": group.clip_id,
                "sequence": getattr(take.words[0], "sequence", "") if take.words else "",
                "start": take.start,
                "end": end,
                "text": take.text,
                "explicit_trim": True,
            }
        )
    group.cuts = cuts
    # profile is accepted so callers can pass it; spans are word-bounded.
    del profile
    del keep_take


def _groups(clip_id: str, utterances: list[Utterance], profile: StyleProfile, signals: dict) -> list[TakeGroup]:
    parent = list(range(len(utterances)))

    def find(index: int) -> int:
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    for left in range(len(utterances)):
        for right in range(left + 1, len(utterances)):
            if utterances[right].start > utterances[left].end + profile.group_window:
                break
            if _linked(utterances[left], utterances[right], profile):
                parent[find(right)] = find(left)
    buckets: dict[int, list[int]] = {}
    for index in range(len(utterances)):
        buckets.setdefault(find(index), []).append(index)
    groups = []
    for indexes in buckets.values():
        if len(indexes) < 2:
            continue
        takes = [utterances[index] for index in indexes]
        _flag_incomplete(takes, profile)
        complete = [index for index, take in enumerate(takes) if not getattr(take, "incomplete", False)]
        if not complete:
            keep = max(range(len(takes)), key=lambda index: takes[index].start)
            complete_for_score = list(range(len(takes)))
        else:
            keep = max(complete, key=lambda index: takes[index].start)
            complete_for_score = complete
        scored = [_score(take, signals.get(_key(take)), profile) for take in takes]
        close_pool = [scored[index] for index in complete_for_score]
        close = (
            len(complete) >= 2
            and max(close_pool) - min(close_pool) < profile.take_score_margin
        )
        groups.append(
            TakeGroup(clip_id, takes, complete, keep, close, scored)
        )
    return groups


def _flag_incomplete(takes: list[Utterance], profile: StyleProfile) -> None:
    longest = max(takes, key=lambda take: (len(take.tokens), take.start))
    long_tokens = longest.tokens
    for index, take in enumerate(takes):
        later = takes[index + 1 :]
        prefix = any(
            take.tokens
            and len(take.tokens) < len(other.tokens)
            and other.tokens[: len(take.tokens)] == take.tokens
            for other in later
        )
        coverage = 1.0
        if long_tokens:
            have = list(take.tokens)
            matched = 0
            for token in long_tokens:
                if token in have:
                    have.remove(token)
                    matched += 1
            coverage = matched / len(long_tokens)
        short = coverage < profile.incomplete_coverage and take is not longest
        take.incomplete = bool(prefix or take.cutoff or short)  # type: ignore[attr-defined]


def _score(take: Utterance, signal: TakeSignal | None, profile: StyleProfile) -> float:
    signal = signal or TakeSignal()
    tokens = max(len(take.tokens), 1)
    fillers = signal.filler_count
    if fillers is None:
        fillers = sum(1 for token in take.tokens if token in _FILLERS)
    filler_score = 1.0 - min(1.0, fillers / tokens)
    if signal.loudness_variance is None:
        level_score = 0.5
    else:
        level_score = 1.0 / (1.0 + max(0.0, signal.loudness_variance))
    if signal.clipping:
        clip_score = 0.0
    else:
        clip_score = 1.0
    rate = signal.speech_rate
    if rate is None and take.duration > 0:
        rate = len(take.tokens) / float(take.duration)
    median = profile.speech_rate_median or 1.0
    if rate is None:
        rate_score = 0.5
    else:
        rate_score = 1.0 - min(1.0, abs(rate - median) / median)
    return round(0.35 * filler_score + 0.25 * level_score + 0.20 * clip_score + 0.20 * rate_score, 4)


def _linked(left: Utterance, right: Utterance, profile: StyleProfile) -> bool:
    if (
        len(left.tokens) >= profile.prefix_tokens
        and right.tokens[: len(left.tokens)] == left.tokens
    ):
        return True
    if not left.tokens or not right.tokens:
        return False
    return SequenceMatcher(a=left.tokens, b=right.tokens).ratio() >= profile.similarity


def _utterances(words: list, profile: StyleProfile) -> list[Utterance]:
    ordered = sorted(words, key=lambda word: (word.start, word.end))
    groups: list[list] = []
    bucket: list = []
    for word in ordered:
        if not str(word.text).strip():
            continue
        if bucket:
            gap = word.start - bucket[-1].end
            if gap >= profile.utterance_pause or _ends_sentence(bucket[-1].text):
                groups.append(bucket)
                bucket = []
        bucket.append(word)
    if bucket:
        groups.append(bucket)
    utterances = []
    for group in groups:
        text = " ".join(str(word.text).strip() for word in group)
        tokens = tuple(_normalize(text))
        cutoff = _ends_on_cutoff(group, profile)
        clip_id = str(getattr(group[0], "clip_id", "") or "")
        utterances.append(
            Utterance(clip_id, group[0].start, group[-1].end, text, tokens, tuple(group), cutoff)
        )
    return utterances


def _ends_on_cutoff(words: list, profile: StyleProfile) -> bool:
    end = words[-1].end
    for word in words:
        if end - word.end > profile.cutoff_window:
            continue
        if _is_cutoff(word, profile):
            return True
    return False


def _is_cutoff(word, profile: StyleProfile) -> bool:
    if getattr(word, "partial", False):
        return True
    text = str(word.text).strip()
    if text.endswith("-") or text.endswith("—"):
        return True
    confidence = getattr(word, "confidence", None)
    return confidence is not None and float(confidence) < profile.cutoff_confidence


def _normalize(text: str) -> list[str]:
    tokens = _TOKEN.findall(text.lower())
    return [token for token in tokens if token not in _FILLERS]


def _ends_sentence(text: str) -> bool:
    stripped = str(text).rstrip()
    return bool(stripped) and stripped[-1] in _SENTENCE_END


def _by_clip(words: list) -> dict[str, list]:
    grouped: dict[str, list] = {}
    for word in words:
        clip_id = str(getattr(word, "clip_id", "") or "")
        grouped.setdefault(clip_id, []).append(word)
    return grouped


def _timed(words: list) -> bool:
    return any(getattr(word, "start", None) is not None and getattr(word, "end", None) is not None for word in words)


def _by_clip_flat(words: list) -> list[tuple[str, object]]:
    return [(str(getattr(word, "clip_id", "") or ""), word) for word in words if str(getattr(word, "text", "")).strip()]


def _token(text: str) -> str:
    tokens = _TOKEN.findall(str(text).lower())
    return tokens[0] if tokens else ""


def _phrase_at(ordered, index: int) -> str | None:
    clip_id, word = ordered[index]
    token = _token(word.text)
    if token == "you" and index + 1 < len(ordered) and ordered[index + 1][0] == clip_id:
        nxt = _token(ordered[index + 1][1].text)
        if nxt == "know":
            return "you know"
    if token in {"like", "so", "basically"}:
        return token
    return None


def _silence_beside(ordered, index: int, minimum: Fraction) -> bool:
    _clip, word = ordered[index]
    before = Fraction(0)
    after = Fraction(0)
    if index > 0 and ordered[index - 1][0] == ordered[index][0]:
        before = word.start - ordered[index - 1][1].end
    if index + 1 < len(ordered) and ordered[index + 1][0] == ordered[index][0]:
        after = ordered[index + 1][1].start - word.end
    else:
        after = minimum
    if index == 0:
        before = minimum
    return before >= minimum or after >= minimum


def _span(ordered, index: int, phrase: str | None) -> tuple[Fraction, Fraction]:
    word = ordered[index][1]
    end = word.end
    if phrase == "you know" and index + 1 < len(ordered):
        end = ordered[index + 1][1].end
    return word.start, end


def _key(take: Utterance) -> tuple:
    return (take.clip_id, take.start, take.end)


def _stamp(moment: Fraction) -> str:
    return str(moment).replace("/", "_").replace(" ", "")
