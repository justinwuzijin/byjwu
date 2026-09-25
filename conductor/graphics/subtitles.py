"""Word timings to subtitle cues: grouped by code, broken and timed by Jev.

Code does the arithmetic: which spine item a word sits on, phrase grouping
by pause, length and duration, the break layouts that fit, the hold end a
cue could reach, and frame snapping. Jev picks, through ``Router.decide``:

- ``subtitle_partial``: show a word an edit cut part way through, or drop it.
- ``subtitle_break``: which of the layouts that fit a cue is shown.
- ``subtitle_timing``: show the cue as heard, hold it (to the minimum
  on-screen time, or across a short gap to the next cue), or drop a flash.

Every ask carries a deterministic ``rule``: it is the dry-run answer and the
fallback when Jev is down. A cue never crosses a spine edit and never
overlaps another cue; that is enforced after the answers, not asked.
"""

from __future__ import annotations

import json
import math
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from fractions import Fraction
from itertools import combinations
from pathlib import Path
from typing import Any

from cutmcp.jev import MAX_OPTIONS

from ..errors import ConductorError
from ..fcpxml import Clip, Sequence
from ..router import Ask, Decision, Router
from ..timeutil import parse_time, seconds
from .diffusion import group_by
from .profile import GraphicsProfile

_SENTENCE_END = re.compile(r"[.?!…]['\")\]]*$")
_SOFT_END = re.compile(r"[,;:]['\")\]]*$")
_END_PUNCT = re.compile(r"[.,;:!?…]+$")
_BREAK_BEFORE = frozenset({"and", "but", "or", "so", "because", "then", "when", "which", "that", "to"})


@dataclass(frozen=True)
class Word:
    text: str
    start: Fraction
    end: Fraction
    sequence: str
    partial: bool = False
    heard_share: float = 1.0
    confidence: float | None = None


@dataclass
class Cue:
    id: str
    sequence: str
    span: int
    words: list[Word]
    start: Fraction
    end: Fraction
    lines: list[str] = field(default_factory=list)
    timing: str = "as_heard"
    decided_by: dict[str, str] = field(default_factory=dict)

    @property
    def text(self) -> str:
        return " ".join(word.text for word in self.words)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "sequence": self.sequence,
            "start_seconds": seconds(self.start),
            "end_seconds": seconds(self.end),
            "lines": list(self.lines),
            "timing": self.timing,
            "decided_by": dict(self.decided_by),
        }


@dataclass(frozen=True)
class Span:
    """One spine item on the sequence clock. Subtitles never cross from one to the next."""

    index: int
    clip: Clip
    start: Fraction
    end: Fraction


@dataclass
class SubtitlePlan:
    cues: list[Cue]
    decisions: list[Decision]
    receipts: list[dict]
    dropped_words: int = 0
    dropped_cues: int = 0


def load_words(source: Any) -> list[Word]:
    """Words from a ``cut-conductor.words`` file or payload, ``TimelineWord``s, or ``Word``s."""
    if source is None:
        return []
    if isinstance(source, (str, Path)):
        path = Path(source)
        try:
            source = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ConductorError(f"words file did not parse: {path}: {exc}") from exc
    if isinstance(source, Mapping):
        if source.get("protocol") not in (None, "cut-conductor.words"):
            raise ConductorError(f"not a cut-conductor.words payload: {source.get('protocol')!r}")
        source = source.get("words") or []
    words: list[Word] = []
    for item in source:
        if isinstance(item, Word):
            words.append(item)
            continue
        if isinstance(item, Mapping):
            start = _time(item.get("start"), item.get("start_seconds"))
            end = _time(item.get("end"), item.get("end_seconds"))
            file_len = _float(item.get("file_end_seconds")) - _float(item.get("file_start_seconds"))
            text, sequence = str(item.get("text") or ""), str(item.get("sequence") or "")
            partial, confidence = bool(item.get("partial")), item.get("confidence")
        else:
            start, end = Fraction(item.start), Fraction(item.end)
            file_len = float(item.file_end - item.file_start)
            text, sequence = item.text, item.sequence
            partial, confidence = bool(item.partial), item.confidence
        text = text.strip()
        if not text or end <= start:
            continue
        share = 1.0
        if partial and file_len > 0:
            share = max(0.0, min(1.0, float(end - start) / file_len))
        words.append(Word(text, start, end, sequence, partial, share, confidence))
    words.sort(key=lambda word: (word.sequence, word.start, word.end))
    return words


def spans_for(sequence: Sequence) -> list[Span]:
    return [
        Span(index, clip, clip.timeline_start, clip.timeline_end)
        for index, clip in enumerate(sequence.spine)
        if clip.duration > 0
    ]


def plan_subtitles(
    sequence: Sequence,
    words: Iterable[Word],
    profile: GraphicsProfile,
    router: Router,
    *,
    brief: str = "",
    ledger=None,
    id_prefix: str = "s",
) -> SubtitlePlan:
    subs = profile.subtitles
    spans = spans_for(sequence)
    frame = sequence.frame_duration
    placed = _assign(spans, [word for word in words if word.sequence in ("", sequence.name)])
    decisions: list[Decision] = []
    receipts: list[dict] = []
    context = {"subtitles": _context(profile)}

    partial_asks = [
        _partial_ask(f"{id_prefix}w{number:05d}", word, subs.partial_keep_share)
        for number, (_span, word) in enumerate(placed)
        if word.partial
    ]
    keep_partial: dict[int, bool] = {}
    if partial_asks:
        answers, got = router.decide(partial_asks, brief=brief, context=context, ledger=ledger)
        decisions += answers
        receipts += got
        by_ask = {item.id: item for item in answers}
        partial_index = [index for index, (_span, word) in enumerate(placed) if word.partial]
        for index, ask in zip(partial_index, partial_asks, strict=True):
            keep_partial[index] = _value(by_ask[ask.id], ask) == "keep"
    kept = [pair for index, pair in enumerate(placed) if keep_partial.get(index, True)]
    dropped_words = len(placed) - len(kept)

    cues = _group(kept, profile, sequence.name, id_prefix)

    break_asks = [_break_ask(cue, profile) for cue in cues]
    if break_asks:
        answers, got = router.decide(break_asks, brief=brief, context=context, ledger=ledger)
        decisions += answers
        receipts += got
        for cue, ask, answer in zip(cues, break_asks, answers, strict=True):
            layout = _value(answer, ask)
            cue.lines = _lines(cue.words, _layout_breaks(layout), subs.strip_end_punctuation)
            cue.decided_by["break"] = f"{answer.engine}/{answer.source}"

    span_by_index = {span.index: span for span in spans}
    timing_asks: list[Ask] = []
    holds: dict[str, Fraction] = {}
    for position, cue in enumerate(cues):
        after = cues[position + 1] if position + 1 < len(cues) else None
        limit_next = after.start if after is not None and after.span == cue.span else None
        ask, hold = _timing_ask(cue, span_by_index[cue.span], limit_next, profile)
        holds[cue.id] = hold
        timing_asks.append(ask)
    if timing_asks:
        answers, got = router.decide(timing_asks, brief=brief, context=context, ledger=ledger)
        decisions += answers
        receipts += got
        for cue, ask, answer in zip(cues, timing_asks, answers, strict=True):
            cue.timing = _value(answer, ask)
            cue.decided_by["timing"] = f"{answer.engine}/{answer.source}"

    final = _finalize(cues, holds, span_by_index, frame, sequence.tc_start, profile)
    return SubtitlePlan(final, decisions, receipts, dropped_words, len(cues) - len(final))


def _assign(spans: list[Span], words: list[Word]) -> list[tuple[Span, Word]]:
    """Each word on the spine item under its midpoint, clipped to that item."""
    placed: list[tuple[Span, Word]] = []
    for word in words:
        middle = (word.start + word.end) / 2
        span = next((item for item in spans if item.start <= middle < item.end), None)
        if span is None:
            continue
        start, end = max(word.start, span.start), min(word.end, span.end)
        if end <= start:
            continue
        clipped = start != word.start or end != word.end
        share = word.heard_share * float((end - start) / (word.end - word.start)) if clipped else word.heard_share
        placed.append(
            (span, Word(word.text, start, end, word.sequence, word.partial or clipped, share, word.confidence))
        )
    return placed


def _group(pairs: list[tuple[Span, Word]], profile: GraphicsProfile, sequence: str, prefix: str) -> list[Cue]:
    """Phrase groups from Diffusion Studio's ``groupBy``, after a cut or a pause.

    A spine cut and a phrase pause still close a group, because a subtitle
    must not cross an edit. Inside a run, words are packed by character
    length (``max_chars_per_line * max_lines``, the GUINEA preset's limit)
    and then by spoken duration (the CLASSIC / WHISPER limit).
    """
    subs = profile.subtitles
    pause = Fraction(subs.phrase_pause_seconds).limit_denominator(1000)
    longest = float(Fraction(subs.max_seconds).limit_denominator(1000))
    capacity = subs.max_chars_per_line * subs.max_lines
    runs: list[list[tuple[Span, Word]]] = []
    current: list[tuple[Span, Word]] = []
    for span, word in pairs:
        if current and (span.index != current[-1][0].index or word.start - current[-1][1].end >= pause):
            runs.append(current)
            current = []
        current.append((span, word))
    if current:
        runs.append(current)

    cues: list[Cue] = []
    for run in runs:
        span_index = run[0][0].index
        words = [word for _span, word in run]
        packed = group_by(words, length=max(capacity, 1))
        for chunk in packed:
            spoken = sum(float(word.end - word.start) for word in chunk)
            pieces = group_by(chunk, duration=longest) if spoken > longest and len(chunk) > 1 else [chunk]
            for piece in pieces:
                cues.append(
                    Cue(
                        f"{prefix}{len(cues) + 1:05d}",
                        sequence,
                        span_index,
                        piece,
                        piece[0].start,
                        piece[-1].end,
                    )
                )
    return cues


def _line_count(words: list[Word], width: int) -> int:
    lines, used = 1, 0
    for word in words:
        size = len(word.text)
        if used and used + 1 + size > width:
            lines, used = lines + 1, size
        else:
            used = used + (1 if used else 0) + size
    return lines


def _layouts(words: list[Word], width: int, max_lines: int) -> list[tuple[int, ...]]:
    """Every break layout whose lines fit ``width`` (or hold one over-long word each)."""
    count = len(words)
    fit = max(width, max(len(word.text) for word in words))
    found: list[tuple[int, ...]] = []
    for breaks in range(0, min(max_lines, count)):
        for chosen in combinations(range(1, count), breaks):
            edges = (0, *chosen, count)
            if all(len(" ".join(w.text for w in words[a:b])) <= fit for a, b in zip(edges, edges[1:])):
                found.append(tuple(chosen))
    return found


def _layout_score(words: list[Word], breaks: tuple[int, ...]) -> tuple:
    edges = (0, *breaks, len(words))
    lengths = [len(" ".join(w.text for w in words[a:b])) for a, b in zip(edges, edges[1:])]
    clean = sum(
        1
        for index in breaks
        if _SOFT_END.search(words[index - 1].text)
        or _SENTENCE_END.search(words[index - 1].text)
        or _END_PUNCT.sub("", words[index].text).lower() in _BREAK_BEFORE
    )
    bottom_heavy = 0 if len(lengths) < 2 or lengths[-1] >= lengths[0] else 1
    return (len(breaks), -clean, max(lengths) - min(lengths), bottom_heavy, breaks)


def _layout_key(breaks: tuple[int, ...]) -> str:
    return "one_line" if not breaks else "after_" + "_".join(str(index) for index in breaks)


def _layout_breaks(key: str) -> tuple[int, ...]:
    if key == "one_line":
        return ()
    return tuple(int(part) for part in key.removeprefix("after_").split("_"))


def _lines(words: list[Word], breaks: tuple[int, ...], strip: bool) -> list[str]:
    edges = (0, *breaks, len(words))
    lines = [" ".join(w.text for w in words[a:b]) for a, b in zip(edges, edges[1:])]
    if strip:
        lines = [_END_PUNCT.sub("", line) or line for line in lines]
    return lines


def _break_ask(cue: Cue, profile: GraphicsProfile) -> Ask:
    subs = profile.subtitles
    layouts = sorted(_layouts(cue.words, subs.max_chars_per_line, subs.max_lines), key=lambda b: _layout_score(cue.words, b))
    layouts = layouts[:MAX_OPTIONS] or [()]
    options = {
        _layout_key(breaks): "Shows " + " / ".join(repr(line) for line in _lines(cue.words, breaks, False))
        for breaks in layouts
    }
    best = _layout_key(layouts[0])
    runner_up = _layout_score(cue.words, layouts[1]) if len(layouts) > 1 else None
    sure = 0.9 if runner_up is None or runner_up[:2] != _layout_score(cue.words, layouts[0])[:2] else 0.7
    return Ask(
        id=f"{cue.id}_break",
        type="subtitle_break",
        subject={
            "text": cue.text,
            "words": [word.text for word in cue.words],
            "max_chars_per_line": subs.max_chars_per_line,
            "max_lines": subs.max_lines,
        },
        options=options,
        question=(
            "Pick the layout that reads best: fewest lines that fit, lines broken at a phrase "
            "boundary (after punctuation, before a conjunction), and balanced line lengths."
        ),
        rule=lambda _ask, best=best, sure=sure: (best, sure),
    )


def _timing_ask(cue: Cue, span: Span, next_start: Fraction | None, profile: GraphicsProfile) -> tuple[Ask, Fraction]:
    subs = profile.subtitles
    heard = cue.end - cue.start
    minimum = _fraction(subs.min_seconds)
    limit = min(next_start if next_start is not None else span.end, span.end, cue.start + _fraction(subs.max_seconds))
    target = max(cue.end, cue.start + minimum)
    gap = (next_start - target) if next_start is not None else None
    if gap is not None and gap <= _fraction(subs.bridge_seconds):
        target = next_start
    hold = max(cue.end, min(target, limit))
    options = {"as_heard": "Show the cue exactly while its words are heard."}
    if hold > cue.end:
        options["hold"] = (
            f"Hold the cue {seconds(hold - cue.end)}s past its last word "
            "(to the minimum on-screen time, or across a short gap to the next cue)."
        )
    if heard < minimum:
        options["drop"] = "Drop the cue: it would flash on screen too briefly to read."
    flash = _fraction(subs.flash_seconds)
    if "drop" in options and hold - cue.start < flash:
        rule = ("drop", 0.85)
    elif "hold" in options:
        rule = ("hold", 0.85)
    else:
        rule = ("as_heard", 0.9)
    ask = Ask(
        id=f"{cue.id}_timing",
        type="subtitle_timing",
        subject={
            "text": cue.text,
            "heard_seconds": seconds(heard),
            "hold_seconds": seconds(hold - cue.start),
            "gap_to_next_seconds": seconds(next_start - cue.end) if next_start is not None else None,
            "room_before_edit_seconds": seconds(span.end - cue.end),
            "min_seconds": subs.min_seconds,
            "max_seconds": subs.max_seconds,
            "flash_seconds": subs.flash_seconds,
            "partial_words": sum(1 for word in cue.words if word.partial),
        },
        options=options,
        question=(
            "A cue must stay up long enough to read, must not blink off and on between close "
            "cues, and must not flash. Pick how this cue is timed."
        ),
        rule=lambda _ask, rule=rule: rule,
    )
    return ask, hold


def _partial_ask(ask_id: str, word: Word, keep_share: float) -> Ask:
    share = round(word.heard_share, 3)
    keep = share >= keep_share
    margin = abs(share - keep_share)
    return Ask(
        id=ask_id,
        type="subtitle_partial",
        subject={
            "text": word.text,
            "heard_share": share,
            "heard_seconds": seconds(word.end - word.start),
            "keep_share": keep_share,
        },
        options={
            "keep": "Show the word; enough of it is heard that a viewer hears it.",
            "drop": "Leave the word out; the edit cut most of it and it would read as a typo.",
        },
        question="An edit cut this word part way through. Is it shown in the subtitle?",
        rule=lambda _ask, keep=keep, sure=(0.9 if margin >= 0.2 else 0.65): ("keep" if keep else "drop", sure),
    )


def _finalize(
    cues: list[Cue],
    holds: Mapping[str, Fraction],
    spans: Mapping[int, Span],
    frame: Fraction,
    origin: Fraction,
    profile: GraphicsProfile,
) -> list[Cue]:
    final: list[Cue] = []
    flash = _fraction(profile.subtitles.flash_seconds)
    for position, cue in enumerate(cues):
        if cue.timing == "drop":
            continue
        span = spans[cue.span]
        end = holds[cue.id] if cue.timing == "hold" else cue.end
        end = min(end, cue.start + _fraction(profile.subtitles.max_seconds))
        start = _snap(cue.start, frame, origin)
        end = _snap(end, frame, origin)
        low = max(_ceil(span.start, frame, origin), final[-1].end if final and final[-1].sequence == cue.sequence else start)
        high = _floor(span.end, frame, origin)
        after = cues[position + 1] if position + 1 < len(cues) else None
        if after is not None and after.span == cue.span and after.timing != "drop":
            high = min(high, max(_snap(after.start, frame, origin), low))
        start = max(start, low)
        end = min(end, high)
        if end - start < max(frame, flash):
            continue
        cue.start, cue.end = start, end
        final.append(cue)
    return final


def _context(profile: GraphicsProfile) -> dict:
    subs = profile.subtitles
    return {
        "max_chars_per_line": subs.max_chars_per_line,
        "max_lines": subs.max_lines,
        "min_seconds": subs.min_seconds,
        "max_seconds": subs.max_seconds,
    }


def _value(decision: Decision, ask: Ask) -> str:
    if decision.value in (ask.options or {}):
        return decision.value
    value, _confidence = ask.rule(ask)
    return value


def _snap(value: Fraction, frame: Fraction, origin: Fraction) -> Fraction:
    return origin + round((value - origin) / frame) * frame


def _ceil(value: Fraction, frame: Fraction, origin: Fraction) -> Fraction:
    return origin + math.ceil((value - origin) / frame) * frame


def _floor(value: Fraction, frame: Fraction, origin: Fraction) -> Fraction:
    return origin + math.floor((value - origin) / frame) * frame


def _fraction(value: float) -> Fraction:
    return Fraction(value).limit_denominator(1000)


def _time(text: Any, fallback: Any) -> Fraction:
    if text:
        return parse_time(str(text))
    return Fraction(str(_float(fallback)))


def _float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0
