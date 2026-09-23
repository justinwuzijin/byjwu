"""Insert proposal markers. Clip edits are snapshotted and must not change.

FCPXML markers have no color attribute (the DTD allows start, duration, value,
note, completed). Color is the ``color=`` field of the note. ``completed="0"``
makes a to-do marker, which is how review and uncertain calls show up in
Final Cut's to-do index.

Marker ``start`` is in the parent clip's source time, the same coordinate as
the clip's ``start`` attribute — not the sequence offset.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from fractions import Fraction

from .candidates import Candidate
from .decide import Proposal
from .errors import ConductorError
from .fcpxml import Document, local
from .timeutil import format_time

_SHADOW = "Cut Conductor shadow proposal"


def apply_markers(
    document: Document,
    proposals: list[Proposal],
    candidates: list[Candidate],
) -> int:
    """Append markers for non-keep proposals. Returns how many were inserted."""
    by_id = {item.id: item for item in candidates}
    frames = {sequence.name: sequence.frame_duration for sequence in document.sequences}
    added = 0
    for proposal in proposals:
        if not proposal.marker_name or not proposal.marker_note:
            continue
        candidate = by_id[proposal.candidate_id]
        clip = document.clips.get(candidate.clip_id)
        if clip is None or clip.element is None:
            raise ConductorError(
                f"candidate {candidate.id} has no clip element to hang a marker on"
            )
        if _already_present(clip.element, proposal.marker_name):
            continue
        frame = frames.get(candidate.sequence, Fraction(1, 24))
        before_clips = _clip_snapshot(document)
        before_markers = _marker_snapshot(clip.element)
        start = _source_time(clip, candidate.timeline_start, frame)
        _append_marker(
            clip.element,
            start=start,
            duration=frame,
            value=proposal.marker_name,
            note=proposal.marker_note,
            completed="0" if proposal.needs_human else None,
        )
        if _clip_snapshot(document) != before_clips:
            raise ConductorError("refusing to write: a clip edit changed while adding markers")
        after = _marker_snapshot(clip.element)
        if after[: len(before_markers)] != before_markers:
            raise ConductorError("refusing to write: an existing marker was modified")
        added += 1
    return added


def _already_present(element: ET.Element, value: str) -> bool:
    for child in element:
        if local(child.tag) == "marker" and child.get("value") == value:
            return True
    return False


def _source_time(clip, timeline_pos: Fraction, frame: Fraction) -> Fraction:
    local_pos = timeline_pos - clip.timeline_start
    if local_pos < 0:
        local_pos = Fraction(0)
    if clip.duration > frame:
        last = clip.duration - frame
        if local_pos > last:
            local_pos = last
    elif clip.duration > 0:
        local_pos = Fraction(0)
    start = clip.start + local_pos
    used = {
        _marker_start(child)
        for child in clip.element
        if local(child.tag) == "marker" and child.get("start")
    }
    while start in used:
        start += frame
    return start


def _marker_start(element: ET.Element) -> Fraction:
    from .timeutil import parse_time

    return parse_time(element.get("start"), Fraction(0))


def _append_marker(
    element: ET.Element,
    *,
    start: Fraction,
    duration: Fraction,
    value: str,
    note: str,
    completed: str | None,
) -> None:
    marker = ET.Element("marker")
    marker.set("start", format_time(start))
    marker.set("duration", format_time(duration))
    marker.set("value", value)
    marker.set("note", note)
    if completed is not None:
        marker.set("completed", completed)
    element.append(marker)


def _clip_snapshot(document: Document) -> tuple:
    rows = []
    for sequence in document.sequences:
        for clip in sequence.spine:
            if clip.element is None:
                continue
            elem = clip.element
            rows.append(
                (
                    local(elem.tag),
                    elem.get("name"),
                    elem.get("ref"),
                    elem.get("offset"),
                    elem.get("start"),
                    elem.get("duration"),
                    elem.get("audioRole"),
                    elem.get("videoRole"),
                )
            )
    return tuple(rows)


def _marker_snapshot(element: ET.Element) -> tuple:
    return tuple(
        (child.get("start"), child.get("value"), child.get("note"), child.get("completed"))
        for child in element
        if local(child.tag) == "marker"
    )


def conductor_marker_count(element: ET.Element) -> int:
    return sum(
        1
        for child in element.iter()
        if local(child.tag) == "marker" and (child.get("note") or "").startswith(_SHADOW)
    )
