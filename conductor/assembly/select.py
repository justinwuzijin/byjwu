"""Usable ranges ("units") and the editorial decisions made about them.

A unit is a source range the engine could place: a speech range built from
transcript cues (or from silence detection), or a b-roll chunk. Code finds
the units and computes a heuristic for each. Every question is an
:class:`conductor.router.Ask` on the shared :class:`~conductor.router.Router`.

Each question is tagged ``linear`` or ``creative``, and the tag has to match
the router's type table. Linear (bounded, clear criteria: subtitle line
breaks, take rules, cut gates, pacing picks) is a Jev type and is answered by
``Ask.rule``. Creative (structure, hero moments, montage, typography) is an
Opus type and is answered by ``Ask.mock``. Dry-run returns the heuristic.
Opus unavailable keeps the heuristic and marks the call for review. Jev
unavailable uses the rule at the router's fallback discount.

Question keys are ``<item id>_<field>``:

- speech ``uNNNN_section`` — creative ``story_structure``
- speech ``uNNNN_keep`` — creative ``key_moments``, stored as P(keep)
- b-roll ``uNNNN_use`` — creative ``broll_selection``
- dress: ``tcNN_text``, ``tcNN_treat``, ``sNNNN_treat`` — creative ``typography``
- dress: ``sNNNN_break`` — linear ``subtitle_break`` when a line has two or more legal breaks

Taste feedback pins a decision: an ``accept`` event with ``pass: assembly``
and ``candidate_id`` equal to the key sets the value to the event's
``action``; a ``reject`` sets it to ``drop`` / 0 / ``none``.
"""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass, field, replace
from fractions import Fraction

from cutmcp.extract import is_trivial_filler

from ..errors import ConductorError
from ..gates import Gates
from ..router import Ask, Router, classify
from ..style import StyleProfile
from ..taste import Taste
from ..transcript import Cue
from .media import Footage, Material

KEEP_OPTIONS = {
    "keep": "Keep: it is clean and serves the brief.",
    "lose": "Lose it: flub, repeat, filler, or weaker than the rest.",
}
HOOK = re.compile(
    r"\b(today|i'm going to|im going to|this is|here's|heres|why|how|what if|let me|you need)\b|\?",
    re.IGNORECASE,
)
OUTRO = re.compile(
    r"\b(thanks for watching|thank you for watching|subscribe|see you|that's it|thats it|"
    r"peace out|until next time|bye)\b",
    re.IGNORECASE,
)
SPEECH_SECTIONS = {
    "intro": "Hook. Opens the video; strongest, most intriguing line.",
    "talking": "Main body. Carries the story or explanation.",
    "outro": "Closing line: sign-off, call to action, wrap-up.",
    "drop": "Leave out: flub, repeat, filler, off-topic, or weaker take.",
}
BROLL_USES = {
    "intro": "Fast flash in the intro hook, cut on the beat.",
    "montage": "Beat-cut montage section.",
    "cutaway": "Cutaway over talking, covering a jump cut.",
    "drop": "Not usable: shaky, dark, redundant, or off-topic.",
}


@dataclass(frozen=True)
class Question:
    """One routed decision. ``probability`` stores P(first option) as the value."""

    key: str
    type: str
    options: dict[str, str]
    text: str
    probability: bool = False
    lane: str = ""

    def __post_init__(self) -> None:
        engine = classify(self.type).engine
        expected = "linear" if engine == "jev" else "creative"
        if self.lane != expected:
            raise ConductorError(
                f"{self.key} is tagged {self.lane or 'untagged'}, but {self.type} is a {expected} decision"
            )

    @property
    def item(self) -> str:
        return self.key.split("_", 1)[0]

    @property
    def field(self) -> str:
        return self.key.split("_", 1)[1]


@dataclass
class Unit:
    id: str
    footage_index: int
    footage: Footage
    kind: str
    start: Fraction
    end: Fraction
    text: str = ""
    cues: list[Cue] = field(default_factory=list)
    basis: str = ""

    @property
    def duration(self) -> Fraction:
        return self.end - self.start

    @property
    def signature(self) -> str:
        return f"{self.kind}:{self.footage.clip.name}@{self.start}-{self.end}"

    def to_state(self) -> dict:
        seconds = float(self.duration)
        words = len(self.text.split())
        return {
            "id": self.id,
            "kind": self.kind,
            "clip": self.footage.name,
            "clip_role": self.footage.role,
            "source_in_seconds": round(float(self.start), 3),
            "source_out_seconds": round(float(self.end), 3),
            "duration_seconds": round(seconds, 3),
            "text": _trim(self.text, 280),
            "words_per_second": round(words / seconds, 3) if seconds > 0 else 0.0,
            "basis": self.basis,
        }


@dataclass
class Decision:
    key: str
    item: str
    field: str
    value: object
    confidence: float
    reason: str
    source: str
    model: str
    review: bool
    signature: str = ""
    lane: str = ""

    def to_state(self) -> dict:
        return {
            "key": self.key,
            "signature": self.signature,
            "item": self.item,
            "field": self.field,
            "value": self.value,
            "confidence": round(self.confidence, 4),
            "reason": self.reason,
            "source": self.source,
            "model": self.model,
            "review": self.review,
            "lane": self.lane,
        }

    def label(self) -> str:
        return f"{self.key}={self.value} ({self.source} {self.confidence:.2f})"

    @classmethod
    def from_state(cls, data: dict) -> Decision:
        return cls(
            key=data["key"],
            item=data["item"],
            field=data["field"],
            value=data["value"],
            confidence=float(data["confidence"]),
            reason=data.get("reason", ""),
            source=data.get("source", "prior"),
            model=data.get("model", ""),
            review=bool(data.get("review", False)),
            signature=data.get("signature", ""),
            lane=data.get("lane", ""),
        )


def build_units(material: Material, profile: StyleProfile, *, talking_max: float) -> tuple[list[Unit], list[str]]:
    """Speech units from cues or voiced ranges; b-roll chunks for everything else."""
    frame = material.frame
    merge_gap = _f(profile.get("structure.speech_merge_gap_seconds"))
    handle = _f(profile.get("structure.speech_handle_seconds"))
    min_speech = _f(profile.get("structure.min_speech_seconds"))
    chunk = _f(profile.get("structure.broll_chunk_seconds"))
    min_shot = _f(profile.get("cuts.min_shot_seconds"))
    max_len = _f(max(talking_max, float(min_speech) * 2))
    units: list[Unit] = []
    warnings: list[str] = []
    counter = 0

    def next_id() -> str:
        nonlocal counter
        counter += 1
        return f"u{counter:04d}"

    for index, footage in enumerate(material.footage):
        clip = footage.clip
        duration = clip.duration
        if footage.role == "a_roll":
            ranges = _speech_ranges(footage, merge_gap, max_len, min_speech, profile)
            if not ranges:
                warnings.append(f"{clip.name}: A-roll with no usable speech range; treated as b-roll.")
            else:
                previous_end = Fraction(0)
                for start, end, cues, basis in ranges:
                    lo = max(previous_end, start - handle, Fraction(0))
                    hi = min(duration, end + handle)
                    lo, hi = _q(lo, frame), _q(hi, frame)
                    if hi - lo < frame:
                        continue
                    text = " ".join(cue.text for cue in cues)
                    units.append(Unit(next_id(), index, footage, "speech", lo, hi, text, cues, basis))
                    previous_end = hi
                continue
        edge = min(Fraction(1, 5), duration / 10)
        cursor = _q(edge, frame)
        end_limit = duration - edge
        while end_limit - cursor >= min_shot:
            stop = min(end_limit, cursor + chunk)
            if end_limit - stop < min_shot:
                stop = end_limit
            stop = _q(stop, frame)
            if stop - cursor < frame:
                break
            units.append(Unit(next_id(), index, footage, "visual", cursor, stop, basis=footage.role_reason))
            cursor = stop
    return units, warnings


def _suppress_earlier_takes(speech: list[Unit], hints: dict[str, dict]) -> None:
    """Drop an earlier line when a later one repeats it.

    Same clip, or the next clip when the wording is almost the same. A later
    complete sentence wins, matching the retake rule used on applied cuts.
    The window on one clip is 45 seconds. Fillers are ignored in the compare.
    """
    window = 45.0
    for index, unit in enumerate(speech):
        left = _take_tokens(unit.text)
        if len(left) < 3:
            continue
        for later in speech[index + 1 : index + 8]:
            same_clip = later.footage.clip.path == unit.footage.clip.path
            if same_clip and float(later.start - unit.end) > window:
                continue
            right = _take_tokens(later.text)
            if len(right) < 3:
                continue
            ratio = difflib.SequenceMatcher(None, left, right).ratio()
            prefix = len(right) > len(left) and right[: len(left)] == left
            # The later line has to be the complete one. A shorter echo is not a retake of a finished line.
            if len(right) + 1 < len(left) and not prefix:
                continue
            close = ratio >= 0.85 or (same_clip and (ratio >= 0.6 or prefix))
            if not close:
                continue
            hints[unit.id]["keep"] = {
                "value": 0.12,
                "confidence": 0.9,
                "reason": "earlier take; a later complete line matches",
            }
            break


def _take_tokens(text: str) -> list[str]:
    skip = {"um", "uh", "erm", "hmm", "like"}
    return [token for token in re.findall(r"[a-z0-9']+", text.lower()) if token not in skip]


def estimate_budgets(profile: StyleProfile, target: float) -> dict[str, float]:
    """Seconds per section kind (all instances together), before material limits."""
    order = list(profile.get("structure.order"))
    fixed = 0.0
    for card in ("title_card", "end_card"):
        fixed += order.count(card) * float(profile.get(f"structure.{card}.seconds"))
    budgets: dict[str, float] = {}
    for kind in ("intro", "montage", "outro"):
        if kind not in order:
            budgets[kind] = 0.0
            continue
        spec = profile.get(f"structure.sections.{kind}")
        value = float(spec["share"]) * target
        value = max(value, float(spec["min_seconds"]))
        if spec.get("max_seconds") is not None:
            value = min(value, float(spec["max_seconds"]))
        budgets[kind] = value
    budgets["talking"] = max(0.0, target - fixed - sum(budgets.values()))
    return budgets


def heuristics(units: list[Unit], profile: StyleProfile, budgets: dict[str, float], cover_scale: float = 1.0) -> dict[str, dict]:
    """Per-unit priors. The mock returns them; a live model sees them as priors."""
    hints: dict[str, dict] = {}
    speech = [u for u in units if u.kind == "speech"]
    visual = [u for u in units if u.kind == "visual"]
    min_speech = float(profile.get("structure.min_speech_seconds"))
    intro_speech_budget = budgets["intro"] * (1.0 - float(profile.get("structure.intro_flash_share")))
    hook_used = 0.0
    first_roll = speech[0].footage_index if speech else None
    for position, unit in enumerate(speech):
        seconds = float(unit.duration)
        text = unit.text
        section, reason, confidence = "talking", "main body, in source order", 0.78
        if unit.footage_index == first_roll and position < 3 and hook_used < intro_speech_budget and (
            HOOK.search(text) or position == 0
        ):
            section, reason, confidence = "intro", "opening line reads as a hook", 0.72
            hook_used += seconds
        elif position >= len(speech) - 3 and OUTRO.search(text):
            section, reason, confidence = "outro", "sign-off language", 0.82
        keep, keep_reason = 0.88, "clean speech"
        if text and all(is_trivial_filler(cue.text) for cue in unit.cues):
            keep, keep_reason = 0.12, "only filler"
        elif seconds < min_speech:
            keep, keep_reason = 0.35, f"shorter than {min_speech}s"
        elif not text:
            keep, keep_reason = 0.62, "voiced range without a transcript"
        hints[unit.id] = {
            "section": {"value": section, "confidence": confidence, "reason": reason},
            "keep": {"value": keep, "confidence": max(keep, 1 - keep), "reason": keep_reason},
        }
    _suppress_earlier_takes(speech, hints)
    intro_flash = budgets["intro"] * float(profile.get("structure.intro_flash_share")) if speech else budgets["intro"]
    intro_count = int(round(intro_flash / max(0.2, float(profile.pacing("intro")["asl_seconds"]))))
    cut = profile.get("cuts.cutaway")
    mean_cutaway = (float(cut["min_seconds"]) + float(cut["max_seconds"])) / 2
    cover = float(profile.pacing("talking").get("broll_cover", 0.0)) * cover_scale
    cutaway_count = int(round(budgets["talking"] * cover / max(0.2, mean_cutaway))) if speech else 0
    montage_count = int(round(budgets["montage"] / max(0.2, float(profile.pacing("montage")["asl_seconds"]))))
    for rank, unit in enumerate(_interleave(visual)):
        if rank < intro_count:
            use, reason = "intro", "early pick, varied clips for the hook flashes"
        elif rank < intro_count + montage_count or not speech:
            use, reason = "montage", "beat-cut montage material"
        elif rank < intro_count + montage_count + cutaway_count:
            use, reason = "cutaway", "covers talking at the profile's b-roll rate"
        else:
            use, reason = "cutaway", "surplus, kept as spare coverage"
        hints[unit.id] = {"use": {"value": use, "confidence": 0.7, "reason": reason}}
    return hints


def questions_for(unit: Unit) -> list[Question]:
    if unit.kind == "speech":
        return [
            Question(
                f"{unit.id}_section",
                "story_structure",
                SPEECH_SECTIONS,
                "Where does this speech range belong in the video, given the brief and the style?",
                lane="creative",
            ),
            Question(
                f"{unit.id}_keep",
                "key_moments",
                KEEP_OPTIONS,
                "Does this take earn a place in the cut?",
                probability=True,
                lane="creative",
            ),
        ]
    return [
        Question(
            f"{unit.id}_use",
            "broll_selection",
            BROLL_USES,
            "How should the edit use this b-roll chunk?",
            lane="creative",
        )
    ]


def decide(
    stage: str,
    items: list[dict],
    questions: list[Question],
    *,
    brief: str,
    profile: StyleProfile,
    taste: Taste,
    router: Router,
    gates: Gates,
    prior: dict[str, Decision] | None = None,
    signatures: dict[str, str] | None = None,
) -> tuple[dict[str, Decision], list[dict]]:
    """Route every question through the router. Prior and pinned answers are not re-asked.

    ``prior`` is keyed ``<signature>|<field>`` (see :func:`prior_index`), so a
    decision carries over only to the same source range or card, whatever id
    that item has in this round. The router's own content cache covers repeats
    within one run.
    """
    pins = taste_pins(taste)
    prior = prior or {}
    signatures = signatures or {}
    by_id = {item["id"]: item for item in items}
    decisions: dict[str, Decision] = {}
    asks: list[Ask] = []
    pending: dict[str, Question] = {}
    for question in questions:
        signature = signatures.get(question.item, "")
        carried = prior.get(f"{signature}|{question.field}") if signature else None
        if question.key in pins:
            decisions[question.key] = _pinned(question, pins[question.key])
        elif carried is not None:
            decisions[question.key] = replace(carried, key=question.key, item=question.item)
        else:
            item = by_id[question.item]
            hint = item["heuristic"][question.field]
            value, confidence = _hint_answer(question, hint)
            asks.append(
                Ask(
                    id=question.key,
                    type=question.type,
                    subject={key: value for key, value in item.items() if key != "id"},
                    options=dict(question.options),
                    question=question.text,
                    rule=(_bound(value, confidence) if question.lane == "linear" else None),
                    mock=(None if question.lane == "linear" else _bound(value, confidence)),
                )
            )
            pending[question.key] = question
    if not asks:
        return decisions, []
    context = {"stage": stage, "style": profile.summary(), "prefs": taste.to_state().get("prefs", {})}
    answered, receipts = router.decide(asks, brief=brief, context=context)
    for routed in answered:
        question = pending[routed.id]
        hint = by_id[question.item]["heuristic"][question.field]
        signature = signatures.get(question.item, "")
        source = f"{routed.engine}:{routed.source}"
        if routed.value is None or routed.source == "unavailable":
            decisions[routed.id] = Decision(
                key=routed.id,
                item=question.item,
                field=question.field,
                value=hint["value"],
                confidence=0.0,
                reason=f"{routed.detail or 'Claude unavailable'} Heuristic kept: {hint.get('reason', '')}".strip(),
                source=source,
                model=routed.model or "",
                review=True,
                signature=signature,
                lane=question.lane,
            )
            continue
        confidence = float(routed.confidence)
        value = routed.value
        if question.probability:
            first = next(iter(question.options))
            value = round(confidence if value == first else 1.0 - confidence, 4)
        reason = routed.rationale if routed.source == "live" else str(hint.get("reason", ""))
        decisions[routed.id] = Decision(
            key=routed.id,
            item=question.item,
            field=question.field,
            value=value,
            confidence=confidence,
            reason=reason,
            source=source,
            model=routed.model or "",
            review=confidence < gates.review_confidence,
            signature=signature,
            lane=question.lane,
        )
    return decisions, [{"stage": stage, **receipt} for receipt in receipts]


def _hint_answer(question: Question, hint: dict) -> tuple[object, float]:
    confidence = float(hint.get("confidence", 0.7))
    value = hint["value"]
    if question.probability:
        options = list(question.options)
        probability = float(value)
        value = options[0] if probability >= 0.5 else options[1]
        confidence = max(probability, 1.0 - probability)
    return value, confidence


def _bound(value: object, confidence: float):
    def answer(_ask: Ask | None = None):
        return value, confidence

    return answer


def prior_index(decisions: dict[str, Decision] | list[Decision]) -> dict[str, Decision]:
    rows = decisions.values() if isinstance(decisions, dict) else decisions
    return {f"{d.signature}|{d.field}": d for d in rows if d.signature and d.source != "taste"}


def select(
    units: list[Unit],
    hints: dict[str, dict],
    *,
    brief: str,
    profile: StyleProfile,
    taste: Taste,
    router: Router,
    prior: dict[str, Decision] | None = None,
) -> tuple[dict[str, Decision], list[dict]]:
    items: list[dict] = []
    questions: list[Question] = []
    for unit in units:
        state = unit.to_state()
        state["heuristic"] = hints[unit.id]
        items.append(state)
        questions.extend(questions_for(unit))
    return decide(
        "select",
        items,
        questions,
        brief=brief,
        profile=profile,
        taste=taste,
        router=router,
        gates=taste.gates,
        prior=prior,
        signatures={unit.id: unit.signature for unit in units},
    )


def taste_pins(taste: Taste) -> dict[str, object]:
    pins: dict[str, object] = {}
    for event in taste.log:
        if event.get("pass") != "assembly":
            continue
        key = str(event.get("candidate_id") or "")
        if "_" not in key:
            continue
        if event.get("event") == "accept" and event.get("action") not in (None, ""):
            pins[key] = event["action"]
        elif event.get("event") == "reject":
            pins[key] = REJECTED.get(key.split("_", 1)[1], "drop")
    return pins


REJECTED: dict[str, object] = {"section": "drop", "use": "drop", "keep": 0.0, "treat": "none"}


def _pinned(question: Question, value: object) -> Decision:
    if question.probability:
        try:
            value = max(0.0, min(1.0, float(value)))
        except (TypeError, ValueError):
            value = 1.0 if str(value).lower() in {"keep", "true", "yes"} else 0.0
    elif str(value) not in question.options:
        value = next(iter(question.options))
    return Decision(
        question.key, question.item, question.field, value, 1.0,
        "editor feedback (taste log)", "taste", "taste", False, lane=question.lane,
    )


def _speech_ranges(
    footage: Footage,
    merge_gap: Fraction,
    max_len: Fraction,
    min_speech: Fraction,
    profile: StyleProfile | None = None,
) -> list[tuple[Fraction, Fraction, list[Cue], str]]:
    signals = footage.signals
    if signals.has_transcript:
        cues = [cue for cue in signals.cues if cue.start < footage.clip.duration and not is_trivial_filler(cue.text)]
        grouped = _holds(footage, cues, max_len, profile, merge_gap) if profile is not None else _gap_groups(cues, merge_gap, max_len)
        return [(s, min(e, footage.clip.duration), rows, "transcript") for s, e, rows in grouped]
    voiced = signals.speech_ranges(footage.clip.duration, min_speech)
    if voiced:
        return [(s, e, [], "silencedetect") for s, e in voiced]
    if footage.role == "a_roll":
        return [(Fraction(0), footage.clip.duration, [], "whole clip; no signals")]
    return []


def _gap_groups(cues: list[Cue], merge_gap: Fraction, max_len: Fraction) -> list[tuple[Fraction, Fraction, list[Cue]]]:
    ranges: list[tuple[Fraction, Fraction, list[Cue]]] = []
    group: list[Cue] = []
    for cue in cues:
        if group and (cue.start - group[-1].end > merge_gap or cue.end - group[0].start > max_len):
            ranges.append((group[0].start, group[-1].end, group))
            group = []
        group.append(cue)
    if group:
        ranges.append((group[0].start, group[-1].end, group))
    return ranges


def _holds(
    footage: Footage,
    cues: list[Cue],
    max_len: Fraction,
    profile: StyleProfile,
    merge_gap: Fraction,
) -> list[tuple[Fraction, Fraction, list[Cue]]]:
    """Pack consecutive sentences from one topic into one hold.

    The target length is the talking average. A topic boundary from the
    segment index, or a gap wider than the profile's speech merge, starts
    a new hold. A false start and a sign-off stay their own units so a
    retake can still be dropped.
    """
    if not cues:
        return []
    target = _f(profile.pacing("talking")["asl_seconds"])
    segments = _topic_segments(footage, cues)
    holds: list[tuple[Fraction, Fraction, list[Cue]]] = []
    group: list[Cue] = []
    group_topic = ""

    def flush() -> None:
        nonlocal group, group_topic
        if group:
            holds.append((group[0].start, group[-1].end, group))
        group = []
        group_topic = ""

    for index, cue in enumerate(cues):
        topic = _topic_at(segments, cue)
        nxt = cues[index + 1] if index + 1 < len(cues) else None
        alone = _sign_off(cue.text) or (nxt is not None and _false_start(cue.text, nxt.text))
        gapped = bool(group) and cue.start - group[-1].end > merge_gap
        if alone or (group and topic != group_topic) or gapped:
            flush()
        if alone:
            holds.append((cue.start, cue.end, [cue]))
            continue
        if group and cue.end - group[0].start > max_len:
            flush()
        if not group:
            group_topic = topic
        group.append(cue)
        if group[-1].end - group[0].start >= target:
            flush()
    flush()
    return holds


def _topic_segments(footage: Footage, cues: list[Cue]):
    from ..segments import segment_words

    words = list(footage.signals.words)
    if not words:
        words = []
        for cue in cues:
            tokens = cue.text.split()
            if not tokens or cue.end <= cue.start:
                continue
            step = (cue.end - cue.start) / len(tokens)
            for index, token in enumerate(tokens):
                words.append(type("W", (), {
                    "text": token,
                    "start": cue.start + step * index,
                    "end": cue.start + step * (index + 1),
                })())
    if not words:
        return []
    return segment_words(words, source=footage.clip.stem)


def _topic_at(segments, cue: Cue) -> str:
    if not segments:
        return ""
    mid = (cue.start + cue.end) / 2
    for segment in segments:
        if segment.start <= mid < segment.end:
            return segment.id
    return segments[-1].id


def _sign_off(text: str) -> bool:
    return bool(OUTRO.search(text))


def _false_start(text: str, later: str) -> bool:
    left = _take_tokens(text)
    right = _take_tokens(later)
    if len(left) < 3 or len(right) <= len(left):
        return False
    return right[: len(left)] == left


def _interleave(units: list[Unit]) -> list[Unit]:
    """Round-robin across clips so early picks come from different shots."""
    by_clip: dict[int, list[Unit]] = {}
    for unit in units:
        by_clip.setdefault(unit.footage_index, []).append(unit)
    queues = [by_clip[key] for key in sorted(by_clip)]
    out: list[Unit] = []
    while any(queues):
        for queue in queues:
            if queue:
                out.append(queue.pop(0))
    return out


def _trim(text: str, limit: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _q(value: Fraction, frame: Fraction) -> Fraction:
    return Fraction(round(value / frame)) * frame


def _f(value) -> Fraction:
    return Fraction(str(value))

