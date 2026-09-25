"""Candidate regions an editor might want a second look at.

These are heuristics, not judgments. Jev decides what to do with each one.
Nothing here calls a model. Thresholds are the spec; the README quotes them.

Silence gaps
    A spine ``<gap>`` of at least 1.25s, a hole of that length between two
    spine items when the XML has no gap element, or a quiet range of that
    length inside a spine clip when the media-signal stage measured one.
    Final Cut usually writes an explicit gap. The hole path covers hand-built
    XML. Audio silence is optional and only appears when a referenced file
    could be read.

Short clips
    A spine clip (not a gap) shorter than 0.45s. Under 0.20s is a flash frame.
    The mock treats those two bands differently; the generator just flags both.

Long static segments
    A spine clip of at least 20s whose overlapping transcript runs under 0.40
    words per second. With no transcript, a clip is flagged only when its name
    looks like a hold/slate/b-roll, or when it is at least 45s (we cannot see
    the picture, so a merely long talking head is not accused of being static).

Filler-ish pauses
    A whole cue that :func:`cutmcp.extract.is_trivial_filler` would catch
    (``um``, ``you know``, … — never ``like`` / ``yeah`` / ``okay``), lasting
    0.25–3s. Also a gap of at least 0.80s between two cues inside the same
    clip. Partial filler inside a real sentence is left alone.

Connected clips (lower thirds, audio on a lane) are parsed and ignored here.
v1 candidates come from the primary spine only.
"""

from __future__ import annotations

import re
from collections.abc import Sequence as SequenceOf
from dataclasses import dataclass, field
from fractions import Fraction

from cutmcp.extract import is_trivial_filler

from .fcpxml import Clip, Sequence
from .timeutil import seconds
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
_WORD_RE = re.compile(r"[^\w\s\-']+")


@dataclass(frozen=True)
class AudioSilence:
    """A quiet range already mapped onto the sequence timeline.

    Built by the media-signal stage from a reachable file. ``generate`` turns
    ranges of at least ``SILENCE_GAP`` on a spine clip into ``silence_gap``
    candidates. Connected audio stays in the report and does not become a cut.
    """

    clip_id: str
    timeline_start: Fraction
    timeline_end: Fraction
    media_start: Fraction | None = None
    media_end: Fraction | None = None
    integrated_lufs: float | None = None
    true_peak_db: float | None = None
    clipping: bool | None = None

KIND_LABEL = {
    "silence_gap": "silence gap",
    "short_clip": "short clip",
    "long_static": "long static",
    "filler_pause": "filler",
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
    sequence: Sequence,
    cues: list[Cue],
    *,
    transcript_present: bool,
    audio_silences: SequenceOf[AudioSilence] | None = None,
) -> list[Candidate]:
    """Spine candidates for one sequence, in timeline order, ids unassigned."""
    by_clip: dict[str, list[AudioSilence]] = {}
    for item in audio_silences or []:
        by_clip.setdefault(item.clip_id, []).append(item)
    found: list[Candidate] = []
    spine = sequence.spine
    for clip in spine:
        if clip.kind == "gap":
            if clip.duration >= SILENCE_GAP:
                found.append(_silence(sequence, clip, cues, explicit=True))
            continue
        if clip.duration < SHORT_CLIP and clip.duration > 0:
            found.append(_short(sequence, clip, cues))
        elif clip.duration >= LONG_STATIC:
            static = _long_static(sequence, clip, cues, transcript_present)
            if static is not None:
                found.append(static)
        found.extend(_fillers(sequence, clip, cues))
        found.extend(_audio_silences(sequence, clip, by_clip.get(clip.id, []), cues))
    found.extend(_holes(sequence, spine, cues))
    found.sort(key=lambda item: (item.timeline_start, item.timeline_end, item.kind))
    return found


def assign_ids(candidates: list[Candidate]) -> list[Candidate]:
    for index, candidate in enumerate(candidates, start=1):
        candidate.id = f"c{index:04d}"
    return candidates


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
    if transcript_present:
        if wps >= STATIC_WPS:
            return None
    elif not hinted and clip.duration < LONG_STATIC_UNVERIFIED:
        return None
    hint_note = "; the clip name looks like a hold or b-roll" if hinted else ""
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
    found.extend(_restarts(sequence, clip, owned, cues))
    return found


def _restarts(sequence: Sequence, clip: Clip, owned: list[Cue], cues: list[Cue]) -> list[Candidate]:
    """An abandoned attempt immediately retaken. Review, same kind as filler."""
    found: list[Candidate] = []
    for left, right in zip(owned, owned[1:]):
        if not _is_restart(left, right):
            continue
        found.append(
            _make(
                kind="filler_pause",
                label="restart",
                sequence=sequence,
                clip=clip,
                start=left.start,
                end=left.end,
                transcript=left.text,
                reason=(
                    f'restart: "{left.text}" is picked up again by "{_trim(right.text, 80)}"'
                ),
                signals={
                    "pure_filler": False,
                    "adjacent_filler": False,
                    "restart": True,
                    "pause_seconds": seconds(left.duration),
                    "text": left.text,
                    "retake": right.text,
                },
                cues=cues,
                span="subrange",
            )
        )
    return found


def _is_restart(left: Cue, right: Cue) -> bool:
    if is_trivial_filler(left.text) or left.duration > FILLER_MAX:
        return False
    gap = right.start - left.end
    if gap < 0 or gap > 2:
        return False
    left_words = _words(left.text)
    right_words = _words(right.text)
    if not left_words or not right_words or len(left_words) > 6:
        return False
    shared = 0
    for a, b in zip(left_words, right_words):
        if a != b:
            break
        shared += 1
    prefix = shared == len(left_words) and len(right_words) > len(left_words)
    stumbled = shared >= 2 and len(left_words) <= shared + 1 and len(right_words) > shared
    return prefix or stumbled


def _words(text: str) -> list[str]:
    return [part for part in _WORD_RE.sub(" ", text.lower()).split() if part]


def _audio_silences(
    sequence: Sequence, clip: Clip, silences: list[AudioSilence], cues: list[Cue]
) -> list[Candidate]:
    found: list[Candidate] = []
    frame = sequence.frame_duration if sequence.frame_duration > 0 else Fraction(1, 24)
    for item in silences:
        start = item.timeline_start
        end = item.timeline_end
        if end - start < SILENCE_GAP:
            continue
        whole = start <= clip.timeline_start + frame and end >= clip.timeline_end - frame
        signals = {
            "gap_seconds": seconds(end - start),
            "explicit_gap": False,
            "audio": True,
        }
        if item.media_start is not None and item.media_end is not None:
            signals["media_start_seconds"] = seconds(item.media_start)
            signals["media_end_seconds"] = seconds(item.media_end)
        if item.integrated_lufs is not None:
            signals["integrated_lufs"] = item.integrated_lufs
        if item.true_peak_db is not None:
            signals["true_peak_db"] = item.true_peak_db
        if item.clipping is not None:
            signals["clipping"] = item.clipping
        found.append(
            _make(
                kind="silence_gap",
                label=KIND_LABEL["silence_gap"],
                sequence=sequence,
                clip=clip,
                start=start,
                end=end,
                transcript="",
                reason=(
                    f"audio silence of {_num(end - start)}s inside the clip "
                    f"(threshold {_num(SILENCE_GAP)}s)"
                ),
                signals=signals,
                cues=cues,
                span="clip" if whole else "subrange",
            )
        )
    return found


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
