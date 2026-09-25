"""Transcript-keyword B-roll slots, matched by caption text.

Logic chooses where a cutaway starts and which source range fills it.
The taste model may pick among the top three descriptions or veto the
slot. It cannot add a slot or invent a time. Dry-run and a missing
router keep the logical best.

Slot times come from nouns and proper nouns with high TF-IDF, plus the
first content word after a long hold. Density (spacing, hold, duration)
comes from the style profile. Coverage-level presets follow the same
idea as Mosaic: low, moderate, and high map to spacing. Captions follow
the LAVE idea of a short visual narration, embedded as text. Sampling
density follows the Descript idea of fewer frames on a static shot.
No code was copied from those projects.

A live caption backend is optional (``CONDUCTOR_BROLL_VLM``). Tests use
the mock: the caller-supplied description, or a deterministic reading of
the clip name, plus a bag-of-words cosine.
"""

from __future__ import annotations

import math
import os
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from fractions import Fraction

from ..router import Ask, Router
from ..style import StyleProfile
from .media import Word
from .select import Decision, Unit
from .timeline import Item, Media

HARD_MIN = 0.5
HARD_MAX = 8.0
TARGET_LOW = 2.0
TARGET_HIGH = 4.0
VETO = "veto"
TOP_K = 3
HOLD_GAP = 1.25
LEVEL_SPACING = {"low": 14.0, "moderate": 9.0, "high": 5.0}
LEVEL_HOLD = {"low": 30.0, "moderate": 20.0, "high": 12.0}

_TOKEN = re.compile(r"[A-Za-z][A-Za-z'-]*|[A-Za-z]")
_STOP = frozenset(
    """
    a an the of to in on for with at from by as into over after before
    and or but if so than then that this these those it its
    i you we they he she me my our your their
    is am are was were be been being do does did have has had
    will would can could should not no nor
    """.split()
)
_VERBS = frozenset(
    """
    took take takes taking going go goes went make makes made feel feels
    felt fix fixes fixed wait waits waited land lands landed say says said
    want wants wanted need needs needed look looks looked see sees saw
    get gets got come comes came know knows knew think thinks thought
    use uses used try tries tried
    """.split()
)


@dataclass(frozen=True)
class Density:
    """B-roll spacing and duration. Durations are hard-clamped to 0.5–8 s."""

    min_seconds: float = HARD_MIN
    max_seconds: float = HARD_MAX
    target_seconds: float = 3.0
    spacing_seconds: float = 9.0
    max_hold_seconds: float = 20.0
    lead_seconds: float = 1.5
    query_before: float = 2.0
    query_after: float = 4.0
    punchlines: tuple[str, ...] = ()
    coverage_level: str = "moderate"

    def clamped_duration(self, seconds: float) -> float:
        low = min(max(self.min_seconds, HARD_MIN), HARD_MAX)
        high = min(max(self.max_seconds, low), HARD_MAX)
        return min(max(seconds, low), high)


@dataclass(frozen=True)
class SpokenWord:
    """One word on the sequence clock. ``host_*`` is its on-camera spine item."""

    text: str
    start: Fraction
    end: Fraction
    host_start: Fraction
    host_end: Fraction
    line: str = ""


@dataclass(frozen=True)
class BrollClip:
    """One candidate source range and the text that describes it."""

    id: str
    name: str
    media_key: str
    source_start: Fraction
    source_end: Fraction
    description: str = ""
    motion: tuple[tuple[Fraction, float], ...] = ()
    has_audio: bool = False
    text_heavy: bool = False
    static: bool = False

    @property
    def duration(self) -> Fraction:
        return self.source_end - self.source_start


@dataclass(frozen=True)
class Slot:
    id: str
    keyword: str
    start: Fraction
    score: float
    window: str
    host_start: Fraction
    host_end: Fraction
    line: str


@dataclass(frozen=True)
class ScoredClip:
    clip: BrollClip
    score: float
    title: str
    summary: str
    source_start: Fraction
    source_end: Fraction


@dataclass
class Placement:
    slot: Slot
    clip: BrollClip
    timeline_start: Fraction
    duration: Fraction
    source_start: Fraction
    source_end: Fraction
    score: float
    on_beat: bool | None
    reason: str
    vetoed: bool = False


@dataclass
class CoverResult:
    """``applied`` is False when keyword slotting had nothing to match."""

    items: list[Item] = field(default_factory=list)
    decisions: dict[str, Decision] = field(default_factory=dict)
    receipts: list[dict] = field(default_factory=list)
    placements: list[Placement] = field(default_factory=list)
    applied: bool = False


def is_punchline(text: str, phrases: Sequence[str]) -> bool:
    """True when a line is marked face-to-camera and must stay uncovered."""
    lowered = " ".join(text.lower().split())
    if not lowered:
        return False
    return any(phrase.lower() in lowered for phrase in phrases if phrase and phrase.strip())


def density_from_profile(profile: StyleProfile | None) -> Density:
    block = {}
    if profile is not None:
        found = profile.get("cuts.broll", None)
        if isinstance(found, dict):
            block = found
    level = str(block.get("coverage_level") or "moderate")
    if level in LEVEL_SPACING:
        spacing = LEVEL_SPACING[level]
        hold = LEVEL_HOLD[level]
    else:
        spacing = float(block.get("spacing_seconds", 9.0))
        hold = float(block.get("max_hold_seconds", 20.0))
    if "spacing_seconds" in block and "coverage_level" not in block:
        spacing = float(block["spacing_seconds"])
    if "max_hold_seconds" in block and "coverage_level" not in block:
        hold = float(block["max_hold_seconds"])
    low = min(max(float(block.get("min_seconds", HARD_MIN)), HARD_MIN), HARD_MAX)
    high = min(max(float(block.get("max_seconds", HARD_MAX)), low), HARD_MAX)
    target = float(block.get("target_seconds", (TARGET_LOW + TARGET_HIGH) / 2))
    target = min(max(target, low), high)
    lead = min(max(float(block.get("lead_seconds", 1.5)), 1.0), 2.0)
    phrases = tuple(str(p) for p in (block.get("punchlines") or []) if str(p).strip())
    return Density(
        min_seconds=low,
        max_seconds=high,
        target_seconds=target,
        spacing_seconds=max(0.0, spacing),
        max_hold_seconds=max(spacing, hold),
        lead_seconds=lead,
        punchlines=phrases,
        coverage_level=level if level in LEVEL_SPACING else "moderate",
    )


def sample_times(duration: Fraction, *, text_heavy: bool = False, static: bool = False) -> list[Fraction]:
    """Frame times at about 1 fps, denser on text, sparser on a static shot."""
    seconds = float(duration)
    if seconds <= 0:
        return []
    rate = 2.0 if text_heavy else (0.25 if static else 1.0)
    step = 1.0 / rate
    times: list[Fraction] = []
    cursor = 0.0
    while cursor < seconds - 1e-9:
        times.append(Fraction(cursor).limit_denominator(1000))
        cursor += step
    return times or [Fraction(0)]


class MockCaptioner:
    """Offline narration. Uses the supplied description, else the clip name."""

    def narrate(self, clip: BrollClip, frames: Sequence[Fraction]) -> tuple[str, str]:
        del frames
        text = " ".join((clip.description or _name_words(clip.name)).split())
        if not text:
            text = "untitled coverage"
        title = " ".join(text.split()[:6])
        return title, text


class LiveCaptioner:
    """Optional VLM hook. Without a configured backend it records the mock text.

    Set ``CONDUCTOR_BROLL_VLM`` to request it. This build does not call a
    network: a missing backend falls back to :class:`MockCaptioner`.
    """

    def narrate(self, clip: BrollClip, frames: Sequence[Fraction]) -> tuple[str, str]:
        return MockCaptioner().narrate(clip, frames)


def captioner_from_env() -> MockCaptioner | LiveCaptioner:
    flag = os.environ.get("CONDUCTOR_BROLL_VLM", "mock").strip().lower()
    if flag in {"", "mock", "0", "off", "false"}:
        return MockCaptioner()
    return LiveCaptioner()


def embed(text: str) -> dict[str, float]:
    """L2-normalised bag of words. Deterministic, no model."""
    counts: dict[str, float] = {}
    for token in _tokens(text, keep_stops=False):
        counts[token] = counts.get(token, 0.0) + 1.0
    if not counts:
        return {}
    norm = math.sqrt(sum(value * value for value in counts.values()))
    return {key: value / norm for key, value in counts.items()}


def cosine(left: dict[str, float], right: dict[str, float]) -> float:
    if not left or not right:
        return 0.0
    if len(left) > len(right):
        left, right = right, left
    return sum(value * right.get(key, 0.0) for key, value in left.items())


def propose_slots(words: Sequence[SpokenWord], density: Density, *, frame: Fraction) -> list[Slot]:
    """Greedy keyword slots. Starts stay on the word, at least ``spacing`` apart."""
    ordered = sorted(words, key=lambda word: (word.start, word.end, word.text))
    if not ordered:
        return []
    candidates = _candidates(ordered, density)
    candidates.sort(key=lambda row: (-row[0], row[1].start, row[2]))
    chosen: list[Slot] = []
    gap = Fraction(str(density.spacing_seconds))
    lead = Fraction(str(density.lead_seconds))
    counter = 0

    def accept(score: float, word: SpokenWord, keyword: str) -> None:
        nonlocal counter
        if word.start < word.host_start + lead:
            return
        if is_punchline(word.line, density.punchlines):
            return
        if any(abs(word.start - slot.start) < gap for slot in chosen):
            return
        counter += 1
        start = _snap(word.start, frame)
        window = _window_text(ordered, start, density)
        chosen.append(
            Slot(
                id=f"s{counter:04d}",
                keyword=keyword,
                start=start,
                score=score,
                window=window,
                host_start=word.host_start,
                host_end=word.host_end,
                line=word.line,
            )
        )

    for score, word, keyword in candidates:
        accept(score, word, keyword)
    _fill_holds(ordered, chosen, density, frame, accept)
    chosen.sort(key=lambda slot: slot.start)
    return chosen


def rank_clips(
    slot: Slot,
    clips: Sequence[BrollClip],
    *,
    density: Density,
    used: Sequence[tuple[str, Fraction, Fraction]],
    captioner: MockCaptioner | LiveCaptioner | None = None,
) -> list[ScoredClip]:
    """Cosine rank. Near-duplicate source overlaps collapse to one range."""
    captioner = captioner or MockCaptioner()
    query = embed(f"{slot.window} {slot.keyword}")
    scored: list[ScoredClip] = []
    for clip in clips:
        if clip.duration <= 0:
            continue
        frames = sample_times(clip.duration, text_heavy=clip.text_heavy, static=clip.static)
        title, summary = captioner.narrate(clip, frames)
        vector = embed(f"{title} {summary}")
        score = cosine(query, vector)
        if score <= 0:
            continue
        length = Fraction(str(density.clamped_duration(density.target_seconds)))
        src_start, src_end = active_range(clip, length, clip.motion)
        if not source_free(used, clip.media_key, src_start, src_end):
            continue
        scored.append(ScoredClip(clip, score, title, summary, src_start, src_end))
    return _dedupe(scored)[:TOP_K]


def active_range(
    clip: BrollClip, duration: Fraction, motion: Sequence[tuple[Fraction, float]] | None
) -> tuple[Fraction, Fraction]:
    """The highest-energy subrange of ``duration``, or the head when no motion exists."""
    span = min(duration, clip.duration)
    if span <= 0:
        return clip.source_start, clip.source_start
    samples = [(t, energy) for t, energy in (motion or ()) if clip.source_start <= t <= clip.source_end]
    best_start = clip.source_start
    best = -1.0
    if samples:
        for time, _energy in samples:
            end = time + span
            if end > clip.source_end:
                continue
            total = sum(energy for sample, energy in samples if time <= sample < end)
            if total > best:
                best = total
                best_start = time
    end = min(clip.source_end, best_start + span)
    if end - best_start < span and clip.source_end - span >= clip.source_start:
        best_start = clip.source_end - span
        end = clip.source_end
    return best_start, end


def source_free(
    used: Sequence[tuple[str, Fraction, Fraction]], media_key: str, start: Fraction, end: Fraction
) -> bool:
    """False when ``[start, end)`` overlaps a range already taken on ``media_key``."""
    for key, lo, hi in used:
        if key == media_key and lo < end and start < hi:
            return False
    return True


def route_slots(
    ranked: Sequence[tuple[Slot, list[ScoredClip]]],
    router: Router | None,
    *,
    brief: str = "",
    mock_value: str | None = None,
) -> tuple[list[tuple[Slot, ScoredClip | None]], dict[str, Decision], list[dict]]:
    """Taste picks a top-3 id or ``veto``. No router keeps the logical best."""
    kept: list[tuple[Slot, ScoredClip | None]] = []
    decisions: dict[str, Decision] = {}
    if not ranked:
        return kept, decisions, []
    if router is None:
        for slot, options in ranked:
            kept.append((slot, options[0] if options else None))
        return kept, decisions, []
    asks: list[Ask] = []
    by_id: dict[str, tuple[Slot, list[ScoredClip]]] = {}
    for slot, options in ranked:
        if not options:
            kept.append((slot, None))
            continue
        labels = {row.clip.id: f"{row.title}. {row.summary}" for row in options}
        labels[VETO] = "Veto this slot. Leave the A-roll uncovered. Do not invent another slot or time."
        best = options[0].clip.id
        forced = mock_value if mock_value in labels else best

        def _mock(value: str = forced) -> tuple[str, float]:
            return value, 0.8

        ask_id = f"broll_{slot.id}"
        asks.append(
            Ask(
                id=ask_id,
                type="broll_slot",
                subject={
                    "keyword": slot.keyword,
                    "transcript": slot.window,
                    "candidates": [{"id": row.clip.id, "description": labels[row.clip.id]} for row in options],
                },
                options=labels,
                question=(
                    "Pick one of the listed descriptions for this slot, or veto the slot. "
                    "You cannot add a slot or change its time."
                ),
                mock=_mock,
            )
        )
        by_id[ask_id] = (slot, options)
    if not asks:
        return kept, decisions, []
    answered, receipts = router.decide(asks, brief=brief, context={"stage": "broll"})
    for routed in answered:
        slot, options = by_id[routed.id]
        choice = routed.value
        picked = next((row for row in options if row.clip.id == choice), None)
        vetoed = choice == VETO
        if picked is None and not vetoed:
            picked = options[0]
            choice = picked.clip.id
        if vetoed:
            picked = None
        review = routed.source == "unavailable" or float(routed.confidence) < 0.55
        if routed.source == "unavailable":
            picked = options[0]
            choice = picked.clip.id
            vetoed = False
        decisions[routed.id] = Decision(
            key=routed.id,
            item=slot.id,
            field="slot",
            value=VETO if vetoed else choice,
            confidence=float(routed.confidence),
            reason=routed.rationale or routed.why,
            source=f"{routed.engine}:{routed.source}",
            model=routed.model or "",
            review=review,
            signature=f"broll:{slot.keyword}@{slot.start}",
            lane="creative",
        )
        kept.append((slot, None if vetoed else picked))
    return kept, decisions, [{"stage": "broll", **receipt} for receipt in receipts]


def place_matches(
    chosen: Sequence[tuple[Slot, ScoredClip | None]],
    *,
    density: Density,
    frame: Fraction,
    beats: Sequence[Fraction] | None = None,
    snap_tolerance: Fraction | None = None,
    beat_mode: str = "off",
) -> list[Placement]:
    """Duration comes from the slot, clamped. The in point stays on the word."""
    used: list[tuple[str, Fraction, Fraction]] = []
    placed: list[Placement] = []
    beats = list(beats or [])
    for slot, scored in chosen:
        if scored is None:
            placed.append(
                Placement(
                    slot=slot,
                    clip=BrollClip("", "", "", Fraction(0), Fraction(0)),
                    timeline_start=slot.start,
                    duration=Fraction(0),
                    source_start=Fraction(0),
                    source_end=Fraction(0),
                    score=0.0,
                    on_beat=None,
                    reason="taste veto; slot left on the A-roll",
                    vetoed=True,
                )
            )
            continue
        if not source_free(used, scored.clip.media_key, scored.source_start, scored.source_end):
            continue
        duration, on_beat = _duration_for(slot, scored, density, frame, beats, snap_tolerance, beat_mode)
        if duration < Fraction(str(HARD_MIN)):
            continue
        high = Fraction(str(min(density.max_seconds, HARD_MAX)))
        if duration > high:
            duration = high
        used.append((scored.clip.media_key, scored.source_start, scored.source_end))
        beat_note = "out snapped to a beat" if on_beat else "in stays on the start word"
        placed.append(
            Placement(
                slot=slot,
                clip=scored.clip,
                timeline_start=slot.start,
                duration=duration,
                source_start=scored.source_start,
                source_end=scored.source_end,
                score=scored.score,
                on_beat=on_beat,
                reason=(
                    f"keyword {slot.keyword!r} matched {scored.title!r} "
                    f"(cosine {scored.score:.2f}); {beat_note}"
                ),
            )
        )
    return placed


def plan_cutaways(
    words: Sequence[SpokenWord],
    clips: Sequence[BrollClip],
    *,
    density: Density | None = None,
    frame: Fraction = Fraction(1, 24),
    beats: Sequence[Fraction] | None = None,
    snap_tolerance: Fraction | None = None,
    beat_mode: str = "off",
    router: Router | None = None,
    brief: str = "",
    mock_value: str | None = None,
    captioner: MockCaptioner | LiveCaptioner | None = None,
) -> tuple[list[Placement], dict[str, Decision], list[dict], bool]:
    """Full slot-and-match pass. The bool is False when nothing scored above zero."""
    density = density or Density()
    slots = propose_slots(words, density, frame=frame)
    if not slots or not clips:
        return [], {}, [], False
    captioner = captioner or captioner_from_env()
    used: list[tuple[str, Fraction, Fraction]] = []
    ranked: list[tuple[Slot, list[ScoredClip]]] = []
    any_score = False
    for slot in slots:
        options = rank_clips(slot, clips, density=density, used=used, captioner=captioner)
        if options:
            any_score = True
            ranked.append((slot, options))
            top = options[0]
            used.append((top.clip.media_key, top.source_start, top.source_end))
    if not any_score:
        return [], {}, [], False
    chosen, decisions, receipts = route_slots(ranked, router, brief=brief, mock_value=mock_value)
    # Re-check source ranges in placement order. Taste may have swapped a candidate
    # onto a range the greedy pass had reserved for a later slot.
    placements = place_matches(
        chosen,
        density=density,
        frame=frame,
        beats=beats,
        snap_tolerance=snap_tolerance,
        beat_mode=beat_mode,
    )
    return placements, decisions, receipts, True


def words_on_spine(items: Sequence[Item], units: dict[str, Unit]) -> list[SpokenWord]:
    """Cue tokens spread across each cue, mapped onto the spine item's clock."""
    spoken: list[SpokenWord] = []
    for item in items:
        if not item.tags.get("speech"):
            continue
        unit = units.get(str(item.tags.get("unit") or ""))
        if unit is None:
            continue
        timed = _unit_words(unit)
        for word in timed:
            if word.end <= item.start or word.start >= item.start + item.duration:
                continue
            spoken.append(
                SpokenWord(
                    text=word.text,
                    start=item.offset + (word.start - item.start),
                    end=item.offset + (word.end - item.start),
                    host_start=item.offset,
                    host_end=item.end,
                    line=unit.text,
                )
            )
    return spoken


def clips_from_units(units: Sequence[Unit]) -> list[BrollClip]:
    clips: list[BrollClip] = []
    for unit in units:
        signals = unit.footage.signals
        motion = tuple(getattr(signals, "motion", ()) or ())
        clips.append(
            BrollClip(
                id=unit.id,
                name=unit.footage.clip.stem,
                media_key=str(unit.footage.clip.path),
                source_start=unit.start,
                source_end=unit.end,
                description=unit.text,
                motion=motion,
                has_audio=bool(unit.footage.clip.has_audio),
                text_heavy=bool(getattr(signals, "text_heavy", False)),
                static=bool(getattr(signals, "static_shot", False)),
            )
        )
    return clips


def cover_speech(
    hosts: Sequence[Item],
    units: Sequence[Unit],
    pool: Sequence[Unit],
    *,
    profile: StyleProfile,
    frame: Fraction,
    beats: Sequence[Fraction],
    snap_tolerance: Fraction,
    beat_mode: str,
    router: Router | None,
    brief: str,
    media_for: Callable[[Unit], Media],
) -> CoverResult:
    """Place keyword cutaways over ``hosts``. ``applied`` is False to fall back."""
    by_id = {unit.id: unit for unit in units}
    words = words_on_spine(hosts, by_id)
    clips = clips_from_units(pool)
    placements, decisions, receipts, applied = plan_cutaways(
        words,
        clips,
        density=density_from_profile(profile),
        frame=frame,
        beats=beats,
        snap_tolerance=snap_tolerance,
        beat_mode=beat_mode,
        router=router,
        brief=brief,
    )
    if not applied:
        return CoverResult(applied=False)
    volume = float(profile.get("cuts.broll_nat_sound_db"))
    items: list[Item] = []
    pool_by_id = {unit.id: unit for unit in pool}
    for placement in placements:
        if placement.vetoed or placement.duration <= 0:
            continue
        unit = pool_by_id.get(placement.clip.id)
        if unit is None:
            continue
        mute = volume <= -60
        items.append(
            Item(
                kind="clip",
                lane=1,
                offset=placement.timeline_start,
                duration=placement.duration,
                section=hosts[0].section if hosts else "talking",
                name=unit.footage.clip.stem,
                media=media_for(unit),
                start=placement.source_start,
                role="effects",
                src_enable="video" if mute or not unit.footage.clip.has_audio else None,
                volume_db=None if mute or not unit.footage.clip.has_audio else volume,
                tags={
                    "unit": unit.id,
                    "cutaway": True,
                    "keyword": placement.slot.keyword,
                    "on_beat": placement.on_beat,
                    "beat_mode": beat_mode,
                    "reason": placement.reason,
                    "broll_slot": placement.slot.id,
                },
            )
        )
    return CoverResult(items=items, decisions=decisions, receipts=receipts, placements=placements, applied=True)


def _candidates(words: Sequence[SpokenWord], density: Density) -> list[tuple[float, SpokenWord, str]]:
    sentences = _sentences(words)
    scores = _tfidf(sentences)
    rows: list[tuple[float, SpokenWord, str]] = []
    seen: set[tuple[Fraction, str]] = set()
    for sentence in sentences:
        for word in sentence:
            key = _key(word.text)
            score = scores.get((id(sentence), key), 0.0)
            if score <= 0 or not _content(word.text):
                continue
            marker = (word.start, key)
            if marker in seen:
                continue
            seen.add(marker)
            rows.append((score, word, key))
    for word in _holds(words):
        key = _key(word.text)
        if not key or not _content(word.text):
            continue
        marker = (word.start, key)
        if marker in seen:
            continue
        seen.add(marker)
        rows.append((0.35, word, key))
    del density
    return rows


def _fill_holds(words, chosen: list[Slot], density: Density, frame: Fraction, accept) -> None:
    """If A-roll runs longer than the max hold, take the best skipped keyword in the gap."""
    if not words:
        return
    hold = Fraction(str(density.max_hold_seconds))
    starts = [slot.start for slot in chosen]
    edges = [words[0].host_start, *starts, words[-1].host_end]
    pending = _candidates(words, density)
    for left, right in zip(edges, edges[1:]):
        if right - left <= hold:
            continue
        inside = [row for row in pending if left < row[1].start < right]
        inside.sort(key=lambda row: (-row[0], row[1].start))
        for score, word, keyword in inside:
            before = len(chosen)
            accept(score, word, keyword)
            if len(chosen) > before:
                break
    del frame


def _sentences(words: Sequence[SpokenWord]) -> list[list[SpokenWord]]:
    groups: list[list[SpokenWord]] = []
    current: list[SpokenWord] = []
    for word in words:
        if current and _sentence_break(current[-1], word):
            groups.append(current)
            current = []
        current.append(word)
    if current:
        groups.append(current)
    return groups


def _sentence_break(previous: SpokenWord, word: SpokenWord) -> bool:
    if word.start - previous.end >= Fraction(str(HOLD_GAP)):
        return True
    return previous.text.endswith((".", "!", "?"))


def _holds(words: Sequence[SpokenWord]) -> list[SpokenWord]:
    found: list[SpokenWord] = []
    groups = _sentences(words)
    for index, sentence in enumerate(groups):
        if index == 0:
            continue
        previous = groups[index - 1][-1]
        if sentence[0].start - previous.end < Fraction(str(HOLD_GAP)):
            continue
        for word in sentence:
            if _content(word.text):
                found.append(word)
                break
    return found


def _tfidf(sentences: Sequence[Sequence[SpokenWord]]) -> dict[tuple[int, str], float]:
    docs: list[list[str]] = []
    for sentence in sentences:
        docs.append([_key(word.text) for word in sentence if _content(word.text)])
    df: dict[str, int] = {}
    for doc in docs:
        for token in set(doc):
            df[token] = df.get(token, 0) + 1
    total = max(1, len(docs))
    scores: dict[tuple[int, str], float] = {}
    for index, doc in enumerate(docs):
        if not doc:
            continue
        counts: dict[str, int] = {}
        for token in doc:
            counts[token] = counts.get(token, 0) + 1
        length = float(len(doc))
        for token, count in counts.items():
            idf = math.log((total + 1) / (df[token] + 1)) + 1.0
            scores[(id(sentences[index]), token)] = (count / length) * idf
    return scores


def _content(text: str) -> bool:
    raw = text.strip(".,!?;:\"'()[]")
    if not raw:
        return False
    key = raw.lower()
    if key in _STOP or key in _VERBS:
        return False
    if raw[:1].isupper() and raw[1:2].islower():
        return True
    return key.isalpha() and len(key) > 1


def _key(text: str) -> str:
    raw = text.strip(".,!?;:\"'()[]")
    return raw.lower()


def _tokens(text: str, *, keep_stops: bool) -> list[str]:
    out = []
    for match in _TOKEN.findall(text):
        key = match.lower().strip("'")
        if not key:
            continue
        if not keep_stops and key in _STOP:
            continue
        out.append(key)
    return out


def _window_text(words: Sequence[SpokenWord], start: Fraction, density: Density) -> str:
    lo = start - Fraction(str(density.query_before))
    hi = start + Fraction(str(density.query_after))
    picked = [word.text for word in words if word.end > lo and word.start < hi]
    return " ".join(picked)


def _dedupe(rows: Sequence[ScoredClip]) -> list[ScoredClip]:
    """Overlapping ranges of one source: keep the longer, unless its score is lower."""
    ordered = sorted(rows, key=lambda row: (-row.score, -float(row.source_end - row.source_start)))
    kept: list[ScoredClip] = []
    for row in ordered:
        overlap_at = None
        for index, other in enumerate(kept):
            if other.clip.media_key != row.clip.media_key:
                continue
            if other.source_start < row.source_end and row.source_start < other.source_end:
                overlap_at = index
                break
        if overlap_at is None:
            kept.append(row)
            continue
        other = kept[overlap_at]
        longer, shorter = (row, other) if (row.source_end - row.source_start) >= (other.source_end - other.source_start) else (other, row)
        kept[overlap_at] = shorter if longer.score < shorter.score else longer
    kept.sort(key=lambda row: -row.score)
    return kept


def _duration_for(
    slot: Slot,
    scored: ScoredClip,
    density: Density,
    frame: Fraction,
    beats: Sequence[Fraction],
    snap_tolerance: Fraction | None,
    beat_mode: str,
) -> tuple[Fraction, bool | None]:
    target = Fraction(str(density.clamped_duration(density.target_seconds)))
    low = Fraction(str(max(density.min_seconds, HARD_MIN)))
    high = Fraction(str(min(density.max_seconds, HARD_MAX)))
    room = slot.host_end - slot.start
    if room < low:
        return Fraction(0), None
    length = min(target, high, room, scored.clip.duration)
    end = slot.start + length
    on_beat: bool | None = None
    if beat_mode != "off" and beats and snap_tolerance is not None:
        snapped = _nearest_beat(beats, end, snap_tolerance, lo=slot.start + low, hi=min(slot.start + high, slot.host_end))
        if snapped is not None:
            end = snapped
            on_beat = True
        else:
            on_beat = False
    duration = _snap(end, frame) - slot.start
    if duration < low:
        return Fraction(0), on_beat
    if duration > high:
        duration = _snap(slot.start + high, frame) - slot.start
    return duration, on_beat


def _nearest_beat(
    beats: Sequence[Fraction], target: Fraction, tolerance: Fraction, *, lo: Fraction, hi: Fraction
) -> Fraction | None:
    best: Fraction | None = None
    for beat in beats:
        if beat < lo or beat > hi or abs(beat - target) > tolerance:
            continue
        if best is None or abs(beat - target) < abs(best - target):
            best = beat
    return best


def _unit_words(unit: Unit) -> list[Word]:
    timed = [word for word in unit.footage.signals.words if word.text.strip()]
    if timed:
        return [word for word in timed if word.end > word.start]
    out: list[Word] = []
    for cue in unit.cues:
        tokens = cue.text.split()
        if not tokens or cue.end <= cue.start:
            continue
        step = (cue.end - cue.start) / len(tokens)
        for index, token in enumerate(tokens):
            out.append(Word(cue.start + step * index, cue.start + step * (index + 1), token))
    return out


def _name_words(name: str) -> str:
    stem = re.sub(r"^\d+[_\-\s]*", "", name)
    return " ".join(part for part in re.split(r"[_\-\s.]+", stem) if part)


def _snap(value: Fraction, frame: Fraction) -> Fraction:
    if frame <= 0:
        return value
    return Fraction(round(value / frame)) * frame
