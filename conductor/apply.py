"""Ripple accepted deletions into a new timeline.

The input document is mutated in memory. The caller writes that tree to a
new path. Spine items that are not clips (a transition, for example) are
refused rather than left at a stale offset.

A deletion is a half-open range on the sequence. Whole-clip removes, lifted
filler, closed holes, and the tail of a tightened hold are all the same
operation here. Splitting a clip keeps effects and role sources on every
piece, and keeps markers and keywords whose time falls inside that piece.
A connected clip that would be sliced in half is dropped and reported.

Connected offsets stay in the parent's timebase. Shifting a spine item along
the sequence does not rewrite them. Trimming the parent's in point only
rewrites an offset that was stored as seconds from that in point; a Final Cut
timebase offset stays put and the parent's ``start`` moves instead.
"""

from __future__ import annotations

import copy
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from fractions import Fraction

from .errors import ConductorError
from .fcpxml import CLIP_TAGS, Document, anchored_local, is_primary_story, local
from .timeutil import format_time, parse_time

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
    planned: list[tuple] = []
    adjusted: list[Deletion] = []
    for sequence in document.sequences:
        group = by_sequence.get(sequence.name)
        if not group:
            continue
        kept, notes = _shield_connected(sequence, group)
        warnings.extend(notes)
        planned.append((sequence, kept))
        adjusted.extend(kept)
    if not adjusted:
        raise ConductorError(
            "refusing to remove a clip that still has connected items on it"
        )
    for sequence, kept in planned:
        if kept:
            warnings.extend(_ripple(sequence, kept))
    return ApplyResult(cuts=[_cut_row(item) for item in adjusted], warnings=warnings)


def _shield_connected(sequence, deletions: list[Deletion]) -> tuple[list[Deletion], list[str]]:
    """Punch connected coverage out of a deletion.

    A spine gap or clip with anchored children is never removed wholesale.
    Only the uncovered stretches are cut. The covered picture stays, and the
    caller ripples what remains.
    """
    protected: list[tuple[Fraction, Fraction]] = []
    frame = sequence.frame_duration
    for clip in sequence.spine:
        protected.extend(_anchored_spans(clip, frame))
    if not protected:
        return list(deletions), []
    blockers = [
        Deletion(
            candidate_id="connected",
            sequence=sequence.name,
            start=start,
            end=end,
            action="keep",
            pass_name="mechanical",
        )
        for start, end in _merge_spans(protected)
    ]
    warnings: list[str] = []
    adjusted: list[Deletion] = []
    for deletion in deletions:
        pieces = _subtract(deletion.start, deletion.end, blockers)
        kept = sum((end - start for start, end in pieces), Fraction(0))
        covered = deletion.duration - kept
        if covered > 0:
            warnings.append(
                f"kept {format_time(covered)} of {deletion.candidate_id} "
                "because a connected clip covers it"
            )
        for start, end in pieces:
            adjusted.append(
                Deletion(
                    candidate_id=deletion.candidate_id,
                    sequence=deletion.sequence,
                    start=start,
                    end=end,
                    action=deletion.action,
                    pass_name=deletion.pass_name,
                )
            )
    return adjusted, warnings


def _anchored_spans(clip, frame: Fraction) -> list[tuple[Fraction, Fraction]]:
    """Timeline ranges of laned children. Lane-less compound media is not one."""
    element = clip.element
    spans: list[tuple[Fraction, Fraction]] = []
    if element is None:
        children = [
            (child.offset, child.duration)
            for child in clip.connected_clips
            if child.lane is not None
        ]
    else:
        children = []
        for child in element:
            if local(child.tag) not in CLIP_TAGS or not child.get("lane"):
                continue
            children.append(
                (
                    parse_time(child.get("offset"), Fraction(0)),
                    parse_time(child.get("duration"), Fraction(0)),
                )
            )
    for offset, duration in children:
        local_pos, _mode = anchored_local(
            clip.start, clip.duration, offset, duration, frame=frame
        )
        start = clip.timeline_start + local_pos
        end = start + duration
        left = max(start, clip.timeline_start)
        right = min(end, clip.timeline_end)
        if right > left:
            spans.append((left, right))
    return spans


def _merge_spans(spans: list[tuple[Fraction, Fraction]]) -> list[tuple[Fraction, Fraction]]:
    ordered = sorted(spans)
    merged: list[list[Fraction]] = []
    for start, end in ordered:
        if not merged or start > merged[-1][1]:
            merged.append([start, end])
        else:
            merged[-1][1] = max(merged[-1][1], end)
    return [(start, end) for start, end in merged]


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
            # Leave the attribute string alone when the clip did not move.
            if new_offset != clip.offset:
                clip.element.set("offset", format_time(new_offset))
            pieces.append(clip.element)
            continue
        for start, end in kept:
            piece, dropped = _piece(
                clip.element, clip, start, end, deletions, sequence.frame_duration
            )
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


def _piece(element, clip, start: Fraction, end: Fraction, deletions: list[Deletion], frame: Fraction):
    local_start = start - clip.timeline_start
    source_start = clip.start + local_start
    source_end = source_start + (end - start)
    piece = copy.deepcopy(element)
    for child in list(piece):
        piece.remove(child)
    warnings: list[str] = []
    for child in list(element):
        tag = local(child.tag)
        if is_primary_story(local(element.tag), child):
            # The compound's own media. Its offset stays in container time;
            # the parent's start and duration are the trim.
            piece.append(copy.deepcopy(child))
            continue
        if tag in CLIP_TAGS:
            placed = _place_connected(
                child,
                parent_start=clip.start,
                parent_duration=clip.duration,
                local_start=local_start,
                piece_duration=end - start,
                clip_name=clip.name,
                warnings=warnings,
                frame=frame,
            )
            if placed is not None:
                piece.append(placed)
            continue
        if tag in _TIMED:
            placed = _place_timed(child, source_start, source_end)
            if placed is not None:
                piece.append(placed)
            continue
        piece.append(copy.deepcopy(child))
    offset = start - _deleted_before(start, deletions)
    piece.set("offset", format_time(offset))
    piece.set("start", format_time(source_start))
    piece.set("duration", format_time(end - start))
    return piece, warnings


def _place_connected(
    child,
    *,
    parent_start: Fraction,
    parent_duration: Fraction,
    local_start: Fraction,
    piece_duration: Fraction,
    clip_name: str,
    warnings: list[str],
    frame: Fraction,
):
    offset = parse_time(child.get("offset"), Fraction(0))
    duration = parse_time(child.get("duration"), Fraction(0))
    local_pos, mode = anchored_local(
        parent_start, parent_duration, offset, duration, frame=frame
    )
    local_end = local_start + piece_duration
    if local_pos + duration <= local_start or local_pos >= local_end:
        return None
    if local_pos < local_start or local_pos + duration > local_end:
        warnings.append(
            f"dropped connected clip {child.get('name') or local(child.tag)!r} "
            f"on {clip_name!r}; it crossed a cut"
        )
        return None
    placed = copy.deepcopy(child)
    if mode == "edit":
        placed.set("offset", format_time(offset - local_start))
    return placed


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
