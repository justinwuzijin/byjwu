"""Candidate regions an editor might want a second look at.

These are heuristics, not judgments. Jev decides what to do with each one.
Nothing here calls a model. Thresholds are the spec; the README quotes them.

Silence gaps
    A bare stretch of at least 1.25s on the primary storyline: a spine
    ``<gap>`` with nothing on a lane, the uncovered part of a gap whose other
    stretches sit under connected clips, or a hole of that length between two
    spine items when the XML has no gap element. A gap that is covered for its
    whole duration is not silence. Nested gaps inside a compound clip are
    timing containers, not empty picture, and are ignored.

Covered gaps
    The part of a spine gap that has a connected clip on a lane. That is the
    shot the viewer sees. It is a note, not a lift.

Short clips
    A spine clip (not a gap) shorter than 0.45s. Under 0.20s is a flash frame.
    The mock treats those two bands differently; the generator just flags both.

Long static segments
    A spine clip of at least 20s whose overlapping transcript runs under 0.40
    words per second. With no transcript, a clip is flagged when its name
    looks like a hold/slate/b-roll, when it runs at least 4× the shots around
    it, or when it is at least 45s and the timeline is too short to compare.
    A 45s shot in a stretch of 40s shots is the pace, not a hold.

Reused source
    The same asset, later in the timeline, overlaps an earlier use by at least
    1s and at least half of the shorter use. Generators are not assets.
    A note, not a lift.

Rhythm
    On a timeline of at least 12 shots, a jump of 2.2× in the local average
    shot length. A note, not a lift.

Frame rate
    At least 8 spine items conformed from a rate other than the sequence.
    One note per sequence, not a per-clip nag and not a lift.

Untrimmed section
    Eight or more contiguous spine clips, each used from the asset's own
    start for its full duration, running at least two minutes and at least
    2.5× the average of the rest of the cut. One note for the whole run.

Silent card
    A generator (a ``<video>`` whose ref is not an asset) of at least 8s
    with no audio element and nothing on a lane. A note, not a lift.

Music tail
    An external audio bed of at least 20s that ends 8–90s before the
    sequence does. A note, not a lift.

Filler-ish pauses
    A whole cue that :func:`cutmcp.extract.is_trivial_filler` would catch
    (``um``, ``you know``, … — never ``like`` / ``yeah`` / ``okay``), lasting
    0.25–3s. Also a gap of at least 0.80s between two cues inside the same
    clip. Partial filler inside a real sentence is left alone.

Connected clips are parsed so a gap can tell coverage from empty primary.
Candidates still hang on the primary spine.
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from fractions import Fraction

from cutmcp.extract import is_trivial_filler

from .fcpxml import Clip, Sequence, local
from .timeutil import rate_label, same_rate, seconds
from .transcript import Cue

SILENCE_GAP = Fraction("1.25")
SHORT_CLIP = Fraction("0.45")
FLASH_CLIP = Fraction("0.20")
LONG_STATIC = Fraction(20)
LONG_STATIC_UNVERIFIED = Fraction(45)
STATIC_WPS = 0.40
FILLER_MIN = Fraction("0.25")
FILLER_MAX = Fraction(3)
PAUSE = Fraction("0.80")

_NAME_HINT = re.compile(r"\b(static|hold|slate|b-?roll|locked|freeze)\b", re.I)

#: A hold has to be this many times the surrounding shots, and those shots
#: have to exist. Four neighbors is enough to know the local pace.
ASL_RATIO = 4
ASL_NEIGHBORS = 4
REUSE_MIN = Fraction(1)
REUSE_SHARE = Fraction(1, 2)
RHYTHM_RATIO = 2.2
RHYTHM_MIN_SHOTS = 12
RATE_MIX_MIN = 8
UNTRIMMED_MIN_CLIPS = 8
UNTRIMMED_MIN_SECONDS = Fraction(120)
UNTRIMMED_ASL_RATIO = 2.5
SILENT_CARD_MIN = Fraction(8)
MUSIC_BED_MIN = Fraction(20)
MUSIC_TAIL_MIN = Fraction(8)
MUSIC_TAIL_MAX = Fraction(90)

KIND_LABEL = {
    "silence_gap": "silence gap",
    "short_clip": "short clip",
    "long_static": "long static",
    "filler_pause": "filler",
    "covered_gap": "covered gap",
    "source_reuse": "reused source",
    "rhythm_shift": "rhythm change",
    "rate_mix": "frame rate",
    "untrimmed_run": "untrimmed section",
    "silent_card": "silent card",
    "music_tail": "music ends early",
}


@dataclass
class Candidate:
    id: str
    kind: str
    label: str
    sequence: str
    clip_id: str
    clip_name: str
    role: str | None
    timeline_start: Fraction
    timeline_end: Fraction
    reason: str
    transcript: str
    signals: dict
    span: str = "clip"
    pass_name: str = ""
    context: dict = field(default_factory=dict)

    @property
    def duration(self) -> Fraction:
        return self.timeline_end - self.timeline_start

    def to_state(self) -> dict:
        return {
            "id": self.id,
            "kind": self.kind,
            "label": self.label,
            "sequence": self.sequence,
            "clip_name": self.clip_name,
            "role": self.role,
            "timeline_start_seconds": seconds(self.timeline_start),
            "timeline_end_seconds": seconds(self.timeline_end),
            "duration_seconds": seconds(self.duration),
            "pass": self.pass_name,
            "span": self.span,
            "transcript": self.transcript,
            "reason": self.reason,
            "signals": self.signals,
            "context": self.context,
        }


def generate(
    sequence: Sequence, cues: list[Cue], *, transcript_present: bool
) -> list[Candidate]:
    """Spine candidates for one sequence, in timeline order, ids unassigned."""
    found: list[Candidate] = []
    spine = sequence.spine
    for clip in spine:
        if clip.kind == "gap":
            found.extend(_gap_ranges(sequence, clip, cues))
            continue
        if clip.duration < SHORT_CLIP and clip.duration > 0:
            found.append(_short(sequence, clip, cues))
        elif clip.duration >= LONG_STATIC:
            static = _long_static(sequence, clip, cues, transcript_present)
            if static is not None:
                found.append(static)
        found.extend(_fillers(sequence, clip, cues))
    found.extend(_holes(sequence, spine, cues))
    found.extend(_source_reuse(sequence))
    found.extend(_rhythm(sequence))
    found.extend(_rate_mix(sequence))
    found.extend(_untrimmed_runs(sequence))
    found.extend(_silent_cards(sequence))
    found.extend(_music_tail(sequence))
    found.sort(key=lambda item: (item.timeline_start, item.timeline_end, item.kind))
    return found


def assign_ids(candidates: list[Candidate]) -> list[Candidate]:
    for index, candidate in enumerate(candidates, start=1):
        candidate.id = f"c{index:04d}"
    return candidates


def _gap_ranges(sequence: Sequence, clip: Clip, cues: list[Cue]) -> list[Candidate]:
    """Bare stretches are silence. Covered stretches are a note."""
    bare, covered, connected = _coverage(clip)
    found: list[Candidate] = []
    for start, end in bare:
        duration = end - start
        if duration < SILENCE_GAP:
            continue
        partial = not (start == Fraction(0) and end == clip.duration)
        if partial:
            reason = (
                f"uncovered primary gap of {_num(duration)}s "
                f"inside a {_num(clip.duration)}s gap whose other stretches "
                f"sit under connected clips (threshold {_num(SILENCE_GAP)}s)"
            )
        else:
            reason = (
                f"explicit gap of {_num(duration)}s (threshold {_num(SILENCE_GAP)}s)"
            )
        found.append(
            _silence_range(
                sequence,
                clip,
                cues,
                start=clip.timeline_start + start,
                end=clip.timeline_start + end,
                reason=reason,
                partial=partial,
                connected=connected,
            )
        )
    for start, end in covered:
        duration = end - start
        if duration < SILENCE_GAP:
            continue
        found.append(
            _make(
                kind="covered_gap",
                label=KIND_LABEL["covered_gap"],
                sequence=sequence,
                clip=clip,
                start=clip.timeline_start + start,
                end=clip.timeline_start + end,
                transcript="",
                reason=(
                    f"connected clips cover {_num(duration)}s of this "
                    f"{_num(clip.duration)}s primary gap ({connected} on a lane). "
                    "The picture is that coverage, so the gap stays."
                ),
                signals={
                    "covered_seconds": seconds(duration),
                    "gap_seconds": seconds(clip.duration),
                    "connected_count": connected,
                    "do_not_cut": True,
                },
                cues=cues,
                span="note",
            )
        )
    return found


def _coverage(
    clip: Clip,
) -> tuple[list[tuple[Fraction, Fraction]], list[tuple[Fraction, Fraction]], int]:
    """Local bare ranges, local covered ranges, and how many lane items contributed."""
    intervals: list[tuple[Fraction, Fraction]] = []
    for child in clip.connected_clips:
        if child.lane is None:
            continue
        local = child.local_offset if child.local_offset is not None else child.offset
        start = local
        end = local + child.duration
        if end <= 0 or start >= clip.duration:
            continue
        intervals.append((max(start, Fraction(0)), min(end, clip.duration)))
    intervals.sort()
    merged: list[list[Fraction]] = []
    for start, end in intervals:
        if not merged or start > merged[-1][1]:
            merged.append([start, end])
        else:
            merged[-1][1] = max(merged[-1][1], end)
    bare: list[tuple[Fraction, Fraction]] = []
    cursor = Fraction(0)
    for start, end in merged:
        if start > cursor:
            bare.append((cursor, start))
        cursor = max(cursor, end)
    if cursor < clip.duration:
        bare.append((cursor, clip.duration))
    covered = [(start, end) for start, end in merged]
    return bare, covered, len(intervals)


def _silence_range(
    sequence: Sequence,
    clip: Clip,
    cues: list[Cue],
    *,
    start: Fraction,
    end: Fraction,
    reason: str,
    partial: bool,
    connected: int,
) -> Candidate:
    whole = not partial and start == clip.timeline_start and end == clip.timeline_end
    return _make(
        kind="silence_gap",
        label=KIND_LABEL["silence_gap"],
        sequence=sequence,
        clip=clip,
        start=start,
        end=end,
        transcript="",
        reason=reason,
        signals={
            "gap_seconds": seconds(end - start),
            "explicit_gap": True,
            "partial": partial,
            "bare": True,
            "connected_on_gap": connected,
        },
        cues=cues,
        span="clip" if whole else "subrange",
    )


def _silence(sequence: Sequence, clip: Clip, cues: list[Cue], *, explicit: bool) -> Candidate:
    kind = "silence_gap"
    how = "explicit gap" if explicit else "timeline hole"
    return _make(
        kind=kind,
        label=KIND_LABEL[kind],
        sequence=sequence,
        clip=clip,
        start=clip.timeline_start,
        end=clip.timeline_end,
        transcript="",
        reason=f"{how} of {_num(clip.duration)}s (threshold {_num(SILENCE_GAP)}s)",
        signals={
            "gap_seconds": seconds(clip.duration),
            "explicit_gap": explicit,
            "partial": False,
            "bare": True,
        },
        cues=cues,
    )


def _short(sequence: Sequence, clip: Clip, cues: list[Cue]) -> Candidate:
    return _make(
        kind="short_clip",
        label=KIND_LABEL["short_clip"],
        sequence=sequence,
        clip=clip,
        start=clip.timeline_start,
        end=clip.timeline_end,
        transcript=_overlap_text(cues, clip),
        reason=f"clip is {_num(clip.duration)}s, under {_num(SHORT_CLIP)}s",
        signals={
            "duration_seconds": seconds(clip.duration),
            "flash": clip.duration < FLASH_CLIP,
        },
        cues=cues,
    )


def _long_static(
    sequence: Sequence, clip: Clip, cues: list[Cue], transcript_present: bool
) -> Candidate | None:
    hinted = bool(_NAME_HINT.search(clip.name))
    overlapping = _overlapping(cues, clip.timeline_start, clip.timeline_end)
    words = sum(len(cue.text.split()) for cue in overlapping)
    wps = (words / float(clip.duration)) if clip.duration else 0.0
    asl, neighbors = _local_pace(sequence, clip)
    ratio = float(clip.duration / asl) if asl else None
    outlier = ratio is not None and ratio >= ASL_RATIO
    isolated = neighbors < ASL_NEIGHBORS and clip.duration >= LONG_STATIC_UNVERIFIED
    if transcript_present:
        if wps >= STATIC_WPS:
            return None
    elif not hinted and not outlier and not isolated:
        return None
    hint_note = "; the clip name looks like a hold or b-roll" if hinted else ""
    if ratio is not None and (outlier or not transcript_present):
        hint_note += f"; about {ratio:.1f}× the shots around it (local average {_num(asl)}s)"
    speech = (
        f"{wps:.2f} words/sec"
        if transcript_present
        else "no transcript to confirm speech"
    )
    return _make(
        kind="long_static",
        label=KIND_LABEL["long_static"],
        sequence=sequence,
        clip=clip,
        start=clip.timeline_start,
        end=clip.timeline_end,
        transcript=" ".join(cue.text for cue in overlapping),
        reason=f"{_num(clip.duration)}s with {speech}{hint_note}",
        signals={
            "words_per_second": round(wps, 4) if transcript_present else None,
            "cue_count": len(overlapping),
            "transcript_present": transcript_present,
            "name_hint": hinted,
            "local_asl_seconds": seconds(asl) if asl is not None else None,
            "asl_ratio": round(ratio, 2) if ratio is not None else None,
            "neighbor_count": neighbors,
        },
        cues=cues,
    )


def _fillers(sequence: Sequence, clip: Clip, cues: list[Cue]) -> list[Candidate]:
    if clip.kind == "gap" or not cues:
        return []
    owned = _overlapping(cues, clip.timeline_start, clip.timeline_end)
    # A cue belongs to the clip only when this clip holds most of it. The
    # generator is called per clip, so drop cues whose midpoint sits outside.
    owned = [cue for cue in owned if clip.timeline_start <= _mid(cue) < clip.timeline_end]
    owned.sort(key=lambda cue: (cue.start, cue.end))
    found: list[Candidate] = []
    for cue in owned:
        if not is_trivial_filler(cue.text):
            continue
        if not (FILLER_MIN <= cue.duration <= FILLER_MAX):
            continue
        found.append(
            _make(
                kind="filler_pause",
                label=KIND_LABEL["filler_pause"],
                sequence=sequence,
                clip=clip,
                start=cue.start,
                end=cue.end,
                transcript=cue.text,
                reason=f'whole cue is filler ("{cue.text}", {_num(cue.duration)}s)',
                signals={
                    "pure_filler": True,
                    "adjacent_filler": False,
                    "pause_seconds": seconds(cue.duration),
                    "text": cue.text,
                },
                cues=cues,
                span="subrange",
            )
        )
    for left, right in zip(owned, owned[1:]):
        gap = right.start - left.end
        if gap < PAUSE:
            continue
        neighbor_filler = is_trivial_filler(left.text) or is_trivial_filler(right.text)
        note = "a neighbor is filler" if neighbor_filler else "both neighbors carry words"
        found.append(
            _make(
                kind="filler_pause",
                label="pause",
                sequence=sequence,
                clip=clip,
                start=left.end,
                end=right.start,
                transcript="",
                reason=f"{_num(gap)}s pause between cues, and {note}",
                signals={
                    "pure_filler": False,
                    "adjacent_filler": neighbor_filler,
                    "pause_seconds": seconds(gap),
                    "before": left.text,
                    "after": right.text,
                },
                cues=cues,
                span="subrange",
            )
        )
    return found


def _local_pace(sequence: Sequence, clip: Clip) -> tuple[Fraction | None, int]:
    """Average duration of up to four shots on either side, and how many there were."""
    shots = [item for item in sequence.spine if item.kind != "gap" and item.duration > 0]
    index = next((i for i, item in enumerate(shots) if item is clip), None)
    if index is None:
        return None, 0
    window = shots[max(0, index - 4) : index] + shots[index + 1 : index + 5]
    if len(window) < ASL_NEIGHBORS:
        return None, len(window)
    total = sum((item.duration for item in window), Fraction(0))
    return total / len(window), len(window)


def _source_reuse(sequence: Sequence) -> list[Candidate]:
    """Later spine uses that repeat an earlier source range. One note per later clip."""
    groups: dict[str, list[Clip]] = defaultdict(list)
    for clip in sequence.spine:
        if clip.kind == "gap" or not clip.asset_id or clip.duration <= 0:
            continue
        groups[clip.asset_id].append(clip)
    found: list[Candidate] = []
    for asset_id, clips in groups.items():
        for later in clips:
            best: tuple[Fraction, Clip] | None = None
            later_end = later.start + later.duration
            for earlier in clips:
                if earlier is later or earlier.timeline_start >= later.timeline_start:
                    continue
                overlap = min(later_end, earlier.start + earlier.duration) - max(
                    later.start, earlier.start
                )
                if overlap < REUSE_MIN:
                    continue
                shorter = min(later.duration, earlier.duration)
                if shorter <= 0 or overlap < shorter * REUSE_SHARE:
                    continue
                if best is None or overlap > best[0]:
                    best = (overlap, earlier)
            if best is None:
                continue
            overlap, earlier = best
            shorter = min(later.duration, earlier.duration)
            share = float(overlap / shorter) if shorter else 0.0
            found.append(
                _make(
                    kind="source_reuse",
                    label=KIND_LABEL["source_reuse"],
                    sequence=sequence,
                    clip=later,
                    start=later.timeline_start,
                    end=later.timeline_end,
                    transcript="",
                    reason=(
                        f"{later.name} repeats {_num(overlap)}s already used in "
                        f"{earlier.name} at {_tc(earlier.timeline_start)} "
                        f"({share:.0%} of the shorter use). Marked for a look; "
                        "a repeat can be a reprise."
                    ),
                    signals={
                        "asset_id": asset_id,
                        "overlap_seconds": seconds(overlap),
                        "fraction_of_shorter": round(share, 4),
                        "earlier_name": earlier.name,
                        "earlier_start_seconds": seconds(earlier.timeline_start),
                        "do_not_cut": True,
                    },
                    cues=[],
                    span="note",
                )
            )
    return found


def _untrimmed(clip: Clip, frame: Fraction) -> bool:
    if clip.kind == "gap" or clip.asset_duration is None or clip.asset_start is None:
        return False
    if clip.asset_duration <= 0:
        return False
    if abs(clip.start - clip.asset_start) > frame:
        return False
    return abs(clip.duration - clip.asset_duration) <= frame * 2


def _untrimmed_runs(sequence: Sequence) -> list[Candidate]:
    """One note for a string-out of whole source clips, not one cut per shot."""
    frame = sequence.frame_duration or Fraction(1, 24)
    runs: list[list[Clip]] = []
    current: list[Clip] = []
    for clip in sequence.spine:
        if _untrimmed(clip, frame):
            current.append(clip)
            continue
        if current:
            runs.append(current)
            current = []
    if current:
        runs.append(current)
    others = [
        clip
        for clip in sequence.spine
        if clip.kind != "gap" and clip.duration > 0 and not _untrimmed(clip, frame)
    ]
    other_total = sum((clip.duration for clip in others), Fraction(0))
    other_average = other_total / len(others) if others else None
    found: list[Candidate] = []
    for run in runs:
        total = sum((clip.duration for clip in run), Fraction(0))
        if len(run) < UNTRIMMED_MIN_CLIPS or total < UNTRIMMED_MIN_SECONDS:
            continue
        average = total / len(run)
        if other_average is not None and len(others) >= 4:
            if float(average) < float(other_average) * UNTRIMMED_ASL_RATIO:
                continue
        elif float(average) < 20:
            continue
        graded = _run_has(run, "filter-video") or _run_has(run, "adjust-voiceIsolation")
        against = ""
        if other_average is not None and len(others) >= 4:
            against = (
                f", against {_num(other_average)}s in the rest of the cut"
            )
        grade = (
            " Some of them carry a grade or voice isolation."
            if graded
            else " No grade or voice isolation on them."
        )
        found.append(
            _make(
                kind="untrimmed_run",
                label=KIND_LABEL["untrimmed_run"],
                sequence=sequence,
                clip=run[0],
                start=run[0].timeline_start,
                end=run[-1].timeline_end,
                transcript="",
                reason=(
                    f"{len(run)} shots from {_tc(run[0].timeline_start)} to "
                    f"{_tc(run[-1].timeline_end)} are the whole source clip "
                    f"(average {_num(average)}s{against}).{grade} "
                    "A string-out to review, not a lift."
                ),
                signals={
                    "shot_count": len(run),
                    "average_seconds": seconds(average),
                    "other_average_seconds": None
                    if other_average is None
                    else seconds(other_average),
                    "graded": graded,
                    "do_not_cut": True,
                },
                cues=[],
                span="note",
            )
        )
    return found


def _run_has(clips: list[Clip], tag: str) -> bool:
    for clip in clips:
        element = clip.element
        if element is None:
            continue
        for node in element.iter():
            if local(node.tag) == tag:
                return True
    return False


def _silent_cards(sequence: Sequence) -> list[Candidate]:
    """Generator cards with no audio. Review, never a mechanical remove."""
    found: list[Candidate] = []
    for clip in sequence.spine:
        if not _silent_card(clip):
            continue
        found.append(
            _make(
                kind="silent_card",
                label=KIND_LABEL["silent_card"],
                sequence=sequence,
                clip=clip,
                start=clip.timeline_start,
                end=clip.timeline_end,
                transcript="",
                reason=(
                    f"{clip.name} at {_tc(clip.timeline_start)} is a "
                    f"{_num(clip.duration)}s generator card with no audio on it "
                    "and nothing connected. Left for a title or a shorten. Not a cut."
                ),
                signals={
                    "generator": True,
                    "has_audio": False,
                    "card_seconds": seconds(clip.duration),
                    "do_not_cut": True,
                },
                cues=[],
                span="note",
            )
        )
    return found


def _silent_card(clip: Clip) -> bool:
    if clip.kind == "gap" or clip.asset_id is not None:
        return False
    if clip.kind not in {"video", "title"}:
        return False
    if clip.duration < SILENT_CARD_MIN:
        return False
    if any(child.lane is not None for child in clip.connected_clips):
        return False
    element = clip.element
    if element is None:
        return True
    for node in element.iter():
        if node is element:
            continue
        tag = local(node.tag)
        if tag in {"audio", "asset-clip", "audio-channel-source"}:
            return False
    return True


def _music_tail(sequence: Sequence) -> list[Candidate]:
    """A long external audio bed that finishes while the picture still runs."""
    if sequence.duration is None:
        return []
    frame = sequence.frame_duration or Fraction(1, 24)
    pieces: list[tuple[Clip, Clip]] = []
    for clip in sequence.spine:
        for child in clip.connected_clips:
            if child.lane is None or not child.asset_id or child.asset_has_audio is not True:
                continue
            if child.asset_id == clip.asset_id:
                continue
            pieces.append((clip, child))
    if not pieces:
        return []
    pieces.sort(key=lambda item: (item[1].asset_id or "", item[1].timeline_start))
    beds: list[dict] = []
    for parent, child in pieces:
        if (
            beds
            and beds[-1]["asset_id"] == child.asset_id
            and child.timeline_start - beds[-1]["end"] <= frame * 2
        ):
            if child.timeline_end > beds[-1]["end"]:
                beds[-1]["end"] = child.timeline_end
                beds[-1]["parent"] = parent
                beds[-1]["child"] = child
            continue
        beds.append(
            {
                "asset_id": child.asset_id,
                "start": child.timeline_start,
                "end": child.timeline_end,
                "parent": parent,
                "child": child,
            }
        )
    beds = [bed for bed in beds if bed["end"] - bed["start"] >= MUSIC_BED_MIN]
    if not beds:
        return []
    last = max(beds, key=lambda bed: bed["end"])
    tail = sequence.duration - last["end"]
    if tail < MUSIC_TAIL_MIN or tail > MUSIC_TAIL_MAX:
        return []
    child = last["child"]
    parent = last["parent"]
    return [
        _make(
            kind="music_tail",
            label=KIND_LABEL["music_tail"],
            sequence=sequence,
            clip=parent,
            start=last["end"],
            end=sequence.duration,
            transcript="",
            reason=(
                f"{child.name} ends at {_tc(last['end'])}, "
                f"{_num(tail)}s before the picture ends at {_tc(sequence.duration)}. "
                "The bed stays. Finish with the song, or tighten the tail."
            ),
            signals={
                "bed_name": child.name,
                "tail_seconds": seconds(tail),
                "bed_end_seconds": seconds(last["end"]),
                "do_not_cut": True,
            },
            cues=[],
            span="note",
        )
    ]


def _rhythm(sequence: Sequence) -> list[Candidate]:
    shots = [clip for clip in sequence.spine if clip.kind != "gap" and clip.duration > 0]
    if len(shots) < RHYTHM_MIN_SHOTS:
        return []
    averages: list[Fraction | None] = []
    for index, _clip in enumerate(shots):
        window = shots[max(0, index - 2) : index + 3]
        if len(window) < 5:
            averages.append(None)
            continue
        averages.append(sum((item.duration for item in window), Fraction(0)) / len(window))
    hits: list[tuple[int, float, Fraction, Fraction]] = []
    for index in range(1, len(shots)):
        previous, current = averages[index - 1], averages[index]
        if previous is None or current is None or previous < 1:
            continue
        ratio = float(current / previous)
        if ratio >= RHYTHM_RATIO or ratio <= 1 / RHYTHM_RATIO:
            hits.append((index, ratio, previous, current))
    found: list[Candidate] = []
    group: list[tuple[int, float, Fraction, Fraction]] = []
    for hit in hits:
        if group and hit[0] <= group[-1][0] + 1:
            group.append(hit)
            continue
        if group:
            found.append(_rhythm_hit(sequence, shots, group))
        group = [hit]
    if group:
        found.append(_rhythm_hit(sequence, shots, group))
    return found


def _rhythm_hit(
    sequence: Sequence,
    shots: list[Clip],
    group: list[tuple[int, float, Fraction, Fraction]],
) -> Candidate:
    index, ratio, previous, current = max(group, key=lambda item: abs(item[1] - 1))
    clip = shots[index]
    if ratio >= 1:
        direction = f"lengthens from {_num(previous)}s to {_num(current)}s ({ratio:.1f}×)"
    else:
        direction = (
            f"tightens from {_num(previous)}s to {_num(current)}s "
            f"(about {1/ratio:.1f}× faster)"
        )
    return _make(
        kind="rhythm_shift",
        label=KIND_LABEL["rhythm_shift"],
        sequence=sequence,
        clip=clip,
        start=clip.timeline_start,
        end=clip.timeline_end,
        transcript="",
        reason=(
            f"the local average shot {direction} around {clip.name}. A pace note, and it stays."
        ),
        signals={
            "previous_asl_seconds": seconds(previous),
            "local_asl_seconds": seconds(current),
            "ratio": round(ratio, 3),
            "do_not_cut": True,
        },
        cues=[],
        span="note",
    )


def _rate_mix(sequence: Sequence) -> list[Candidate]:
    sequence_rate = rate_label(sequence.frame_duration)
    counts: Counter[str] = Counter()
    first: dict[str, Clip] = {}
    spine_count = 0
    for clip in sequence.spine:
        if clip.kind == "gap":
            continue
        spine_count += 1
        source = _source_rate(clip)
        if source is None or same_rate(source, sequence_rate):
            continue
        counts[source] += 1
        first.setdefault(source, clip)
    total = sum(counts.values())
    if total < RATE_MIX_MIN or not counts:
        return []
    source, count = counts.most_common(1)[0]
    clip = first[source]
    others = total - count
    extra = f", plus {others} from another rate" if others else ""
    return [
        _make(
            kind="rate_mix",
            label=KIND_LABEL["rate_mix"],
            sequence=sequence,
            clip=clip,
            start=clip.timeline_start,
            end=clip.timeline_end,
            transcript="",
            reason=(
                f"{count} spine items are conformed from {source} into this "
                f"{sequence_rate} sequence{extra}. The export already did the "
                "conform. Noted, and left alone."
            ),
            signals={
                "sequence_rate": sequence_rate,
                "source_rate": source,
                "clip_count": count,
                "mixed_count": total,
                "spine_count": spine_count,
                "do_not_cut": True,
            },
            cues=[],
            span="note",
        )
    ]


def _source_rate(clip: Clip) -> str | None:
    if clip.conform:
        return clip.conform
    if clip.source_frame is not None:
        return rate_label(clip.source_frame)
    return None


def _tc(pos: Fraction) -> str:
    from .timeutil import short_clock

    return short_clock(pos)


def _holes(sequence: Sequence, spine: list[Clip], cues: list[Cue]) -> list[Candidate]:
    found: list[Candidate] = []
    for left, right in zip(spine, spine[1:]):
        hole = right.timeline_start - left.timeline_end
        if hole < SILENCE_GAP:
            continue
        # Hang the marker on the clip that ends where the hole begins.
        # Placement clamps it to that clip's last frame; the range is the hole.
        found.append(
            _make(
                kind="silence_gap",
                label=KIND_LABEL["silence_gap"],
                sequence=sequence,
                clip=left,
                start=left.timeline_end,
                end=right.timeline_start,
                transcript="",
                reason=(
                    f"timeline hole of {_num(hole)}s after this clip "
                    f"(threshold {_num(SILENCE_GAP)}s, no gap element)"
                ),
                signals={"gap_seconds": seconds(hole), "explicit_gap": False},
                cues=cues,
                span="hole",
            )
        )
    return found


def _make(
    *,
    kind: str,
    label: str,
    sequence: Sequence,
    clip: Clip,
    start: Fraction,
    end: Fraction,
    transcript: str,
    reason: str,
    signals: dict,
    cues: list[Cue],
    span: str = "clip",
) -> Candidate:
    noted = dict(signals)
    noted["is_cold_open"] = _is_cold_open(clip)
    return Candidate(
        id="",
        kind=kind,
        label=label,
        sequence=sequence.name,
        clip_id=clip.id,
        clip_name=clip.name,
        role=clip.role,
        timeline_start=start,
        timeline_end=end,
        reason=reason,
        transcript=transcript,
        signals=noted,
        span=span,
        context=_neighbors(cues, start, end),
    )


def _is_cold_open(clip: Clip) -> bool:
    match = re.fullmatch(r"s\d+c(\d+)", clip.id)
    return bool(match and match.group(1) == "0" and clip.kind != "gap")


def _neighbors(cues: list[Cue], start: Fraction, end: Fraction) -> dict:
    before = ""
    after = ""
    for cue in cues:
        if cue.end <= start:
            before = cue.text
        elif cue.start >= end and not after:
            after = cue.text
            break
    return {"before": _trim(before), "after": _trim(after)}


def _overlapping(cues: list[Cue], start: Fraction, end: Fraction) -> list[Cue]:
    return [cue for cue in cues if cue.start < end and cue.end > start]


def _overlap_text(cues: list[Cue], clip: Clip) -> str:
    return " ".join(cue.text for cue in _overlapping(cues, clip.timeline_start, clip.timeline_end))


def _mid(cue: Cue) -> Fraction:
    return cue.start + cue.duration / 2


def _num(value: Fraction) -> str:
    return f"{float(value):.3f}"


def _trim(text: str, limit: int = 140) -> str:
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"
