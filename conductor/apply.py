"""Ripple accepted deletions into a new timeline.

The input document is mutated in memory. The caller writes that tree to a
new path. Spine items that are not clips (a transition, for example) are
refused rather than left at a stale offset.

A deletion is a half-open range on the sequence. Whole-clip removes, lifted
filler, closed holes, and the tail of a tightened hold are all the same
operation here. Splitting a clip keeps effects and role sources on every
piece, and keeps markers and keywords whose time falls inside that piece.
A connected clip that would be sliced in half is dropped and reported.
"""

from __future__ import annotations

import copy
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from fractions import Fraction

from .errors import ConductorError
from .fcpxml import CLIP_TAGS, Document, local
from .timeutil import format_time, parse_time
from .timing import has_time_map, kept_media, local_window, sequence_fps

_TIMED = frozenset({"marker", "keyword", "chapter-marker"})


@dataclass(frozen=True)
class Deletion:
    candidate_id: str
    sequence: str
    start: Fraction
    end: Fraction
    action: str
    pass_name: str

    @property
    def duration(self) -> Fraction:
        return self.end - self.start


@dataclass
class ApplyResult:
    cuts: list[dict]
    warnings: list[str]


def apply_edits(document: Document, deletions: list[Deletion]) -> ApplyResult:
    if not deletions:
        raise ConductorError("apply had nothing to cut")
    _reject_overlaps(deletions)
    warnings: list[str] = []
    by_sequence: dict[str, list[Deletion]] = {}
    for deletion in deletions:
        by_sequence.setdefault(deletion.sequence, []).append(deletion)
    for sequence in document.sequences:
        group = by_sequence.get(sequence.name)
        if not group:
            continue
        warnings.extend(_ripple(sequence, group))
    return ApplyResult(cuts=[_cut_row(item) for item in deletions], warnings=warnings)


def _ripple(sequence, deletions: list[Deletion]) -> list[str]:
    warnings: list[str] = []
    spine = _spine(sequence)
    for child in list(spine):
        if local(child.tag) not in CLIP_TAGS:
            raise ConductorError(
                "apply cannot ripple a spine that contains transitions or other "
                "non-clip items yet"
            )
    clips = [clip for clip in sequence.spine if clip.element is not None]
    pieces: list[ET.Element] = []
    for clip in clips:
        kept = _subtract(clip.timeline_start, clip.timeline_end, deletions)
        if not kept:
            continue
        if kept == [(clip.timeline_start, clip.timeline_end)]:
            new_offset = clip.timeline_start - _deleted_before(clip.timeline_start, deletions)
            clip.element.set("offset", format_time(new_offset))
            pieces.append(clip.element)
            continue
        for start, end in kept:
            piece, dropped = _piece(clip.element, clip, start, end, deletions, sequence)
            warnings.extend(dropped)
            pieces.append(piece)
    for child in list(spine):
        spine.remove(child)
    for piece in pieces:
        spine.append(piece)
    if sequence.element is not None and sequence.duration is not None:
        removed = sum((item.duration for item in deletions), Fraction(0))
        sequence.element.set("duration", format_time(sequence.duration - removed))
    return warnings


def _piece(element, clip, start: Fraction, end: Fraction, deletions: list[Deletion], sequence):
    local_start = start - clip.timeline_start
    local_end = end - clip.timeline_start
    if element is None:
        source_start = clip.start + local_start
        source_end = source_start + (end - start)
    else:
        fps = sequence_fps(sequence.frame_duration)
        source_start, source_end = local_window(element, local_start, local_end, fps)
    source_lo, source_hi = (
        (source_start, source_end)
        if source_start <= source_end
        else (source_end, source_start)
    )
    piece = copy.deepcopy(element)
    for child in list(piece):
        piece.remove(child)
    warnings: list[str] = []
    for child in list(element):
        tag = local(child.tag)
        if tag in CLIP_TAGS:
            if _component(local(element.tag), child):
                piece.append(copy.deepcopy(child))
                continue
            placed = _place_connected(child, source_lo, source_hi, clip.name, warnings)
            if placed is not None:
                piece.append(placed)
            continue
        if tag in _TIMED:
            placed = _place_timed(child, source_lo, source_hi)
            if placed is not None:
                piece.append(placed)
            continue
        piece.append(copy.deepcopy(child))
    offset = start - _deleted_before(start, deletions)
    piece.set("offset", format_time(offset))
    piece.set("start", format_time(source_start))
    piece.set("duration", format_time(end - start))
    if element is not None and element.get("audioStart") is not None and not has_time_map(element):
        audio_start, audio_end = kept_media(
            element, local_start, local_end, sequence_fps(sequence.frame_duration), audio=True
        )
        piece.set("audioStart", format_time(audio_start))
        if element.get("audioDuration") is not None:
            piece.set("audioDuration", format_time(abs(audio_end - audio_start)))
    return piece, warnings


def _component(parent_kind: str, child: ET.Element) -> bool:
    """A lane-less ``<audio>`` / ``<video>`` inside a ``clip`` is its media, not a connected item."""
    return child.get("lane") is None and parent_kind in {"clip", "sync-clip"}


def _place_connected(child, window_start: Fraction, window_end: Fraction, clip_name: str, warnings: list[str]):
    """``offset`` is on the parent's own clock, so a kept piece leaves it where it is."""
    offset = parse_time(child.get("offset"), Fraction(0))
    duration = parse_time(child.get("duration"), Fraction(0))
    if offset + duration <= window_start or offset >= window_end:
        return None
    if offset < window_start or offset + duration > window_end:
        warnings.append(
            f"dropped connected clip {child.get('name') or local(child.tag)!r} "
            f"on {clip_name!r}; it crossed a cut"
        )
        return None
    return copy.deepcopy(child)


def _place_timed(child, source_start: Fraction, source_end: Fraction):
    start = parse_time(child.get("start"), source_start)
    if local(child.tag) == "marker":
        if source_start <= start < source_end:
            return copy.deepcopy(child)
        return None
    duration = parse_time(child.get("duration"), Fraction(0))
    end = start + duration
    if end <= source_start or start >= source_end:
        return None
    placed = copy.deepcopy(child)
    new_start = max(start, source_start)
    new_end = min(end, source_end)
    placed.set("start", format_time(new_start))
    if child.get("duration") is not None:
        placed.set("duration", format_time(new_end - new_start))
    return placed


def _subtract(start: Fraction, end: Fraction, deletions: list[Deletion]) -> list[tuple[Fraction, Fraction]]:
    pieces = [(start, end)]
    for deletion in deletions:
        nxt: list[tuple[Fraction, Fraction]] = []
        for left, right in pieces:
            if deletion.end <= left or deletion.start >= right:
                nxt.append((left, right))
                continue
            if deletion.start > left:
                nxt.append((left, deletion.start))
            if deletion.end < right:
                nxt.append((deletion.end, right))
        pieces = [(left, right) for left, right in nxt if right > left]
    return pieces


def _deleted_before(moment: Fraction, deletions: list[Deletion]) -> Fraction:
    total = Fraction(0)
    for deletion in deletions:
        if deletion.end <= moment:
            total += deletion.duration
        elif deletion.start < moment:
            total += moment - deletion.start
    return total


def _reject_overlaps(deletions: list[Deletion]) -> None:
    ordered = sorted(deletions, key=lambda item: (item.sequence, item.start, item.end))
    for left, right in zip(ordered, ordered[1:]):
        if left.sequence != right.sequence:
            continue
        if left.end > right.start:
            raise ConductorError(
                f"accepted cuts overlap ({left.candidate_id} and {right.candidate_id}); "
                "accept one of them"
            )


def _spine(sequence) -> ET.Element:
    if sequence.element is None:
        raise ConductorError(f"sequence {sequence.name!r} has no element")
    for child in sequence.element:
        if local(child.tag) == "spine":
            return child
    raise ConductorError(f"sequence {sequence.name!r} has no spine")


def _cut_row(deletion: Deletion) -> dict:
    return {
        "candidate_id": deletion.candidate_id,
        "sequence": deletion.sequence,
        "action": deletion.action,
        "pass": deletion.pass_name,
        "start_seconds": round(float(deletion.start), 6),
        "end_seconds": round(float(deletion.end), 6),
    }
