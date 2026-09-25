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

from .apply import Deletion, _deleted_before
from .candidates import Candidate
from .decide import Proposal
from .errors import ConductorError
from .fcpxml import CLIP_TAGS, Document, local
from .timeutil import clock, format_time, parse_time, seconds

_SHADOW = "Cut Conductor shadow proposal"
_APPLIED = "Cut Conductor applied cut."

#: FCPXML 1.14 places these after ``(%marker_item;)*`` on clip-like elements.
#: Inserting a marker before the first of them keeps the content model.
_AFTER_MARKERS = frozenset(
    {
        "audio-channel-source",
        "audio-role-source",
        "sync-source",
        "filter-video",
        "filter-video-mask",
        "filter-audio",
        "metadata",
    }
)


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


def mark_applied_cuts(
    document: Document,
    deletions: list[Deletion],
    proposals: list[Proposal],
    candidates: list[Candidate],
) -> int:
    """One-frame marker on the clip that now starts where a cut was made.

    The name is ``CC cut`` plus what was removed, its duration, and the
    timeline timecode it came from. The note carries the rule and confidence.
    """
    by_proposal = {item.candidate_id: item for item in proposals}
    by_candidate = {item.id: item for item in candidates}
    frames = {sequence.name: sequence.frame_duration for sequence in document.sequences}
    added = 0
    for deletion in deletions:
        proposal = by_proposal.get(deletion.candidate_id)
        candidate = by_candidate.get(deletion.candidate_id)
        if proposal is None or candidate is None:
            continue
        sequence = next((item for item in document.sequences if item.name == deletion.sequence), None)
        if sequence is None or sequence.element is None:
            continue
        frame = frames.get(deletion.sequence) or Fraction(1, 24)
        cut_at = deletion.start - _deleted_before(deletion.start, deletions)
        host, at_in_point = _clip_at_cut(sequence, cut_at)
        if host is None:
            continue
        source = parse_time(host.get("start"), Fraction(0))
        duration = parse_time(host.get("duration"), Fraction(0))
        if at_in_point:
            marker_start = source
        else:
            marker_start = source + max(duration - frame, Fraction(0))
        name = _cut_name(candidate, deletion)
        note = _cut_note(candidate, deletion, proposal)
        if _already_present(host, name):
            continue
        _append_marker(
            host,
            start=marker_start,
            duration=frame if duration <= 0 or duration >= frame else duration,
            value=name,
            note=note,
            completed=None,
        )
        added += 1
    return added


def _clip_at_cut(sequence, cut_at: Fraction) -> tuple[ET.Element | None, bool]:
    """The spine item that starts at the cut, or the one that ends there."""
    spine = next(child for child in sequence.element if local(child.tag) == "spine")
    ending = None
    for child in spine:
        if local(child.tag) not in CLIP_TAGS:
            continue
        offset = parse_time(child.get("offset"), Fraction(0))
        duration = parse_time(child.get("duration"), Fraction(0))
        if offset == cut_at:
            return child, True
        if offset + duration == cut_at:
            ending = child
    return ending, False


def _cut_name(candidate: Candidate, deletion: Deletion) -> str:
    return (
        f"CC cut · {candidate.label} {seconds(deletion.duration)}s @ {clock(deletion.start)}"
    )


def _cut_note(candidate: Candidate, deletion: Deletion, proposal: Proposal) -> str:
    rule = (proposal.rule or {}).get("name") or proposal.decision_type or candidate.kind
    parts = [
        _APPLIED,
        f"rule={rule}",
        f"confidence={proposal.confidence:.2f}",
        f"removed={candidate.label}",
        f"duration={seconds(deletion.duration)}s",
        f"source={clock(deletion.start)}-{clock(deletion.end)}",
        f"action={deletion.action}",
        f"id={candidate.id}",
    ]
    return " | ".join(parts)


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


def add_marker(
    element: ET.Element,
    *,
    start: Fraction,
    duration: Fraction,
    value: str,
    note: str,
    completed: str | None = "0",
) -> None:
    """Insert one review marker before audio-channel sources and filters."""
    if _already_present(element, value):
        return
    _append_marker(
        element,
        start=start,
        duration=duration,
        value=value,
        note=note,
        completed=completed,
    )


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
    element.insert(_marker_insert_at(element), marker)


def _marker_insert_at(element: ET.Element) -> int:
    for index, child in enumerate(element):
        if local(child.tag) in _AFTER_MARKERS:
            return index
    return len(element)


def marker_order_violations(element: ET.Element) -> list[str]:
    """Parents whose ``<marker>`` sits after a post-marker element.

    The 1.11 and 1.14 content models put ``(%marker_item;)*`` before
    ``audio-channel-source*``, video filters, ``filter-audio*``, and ``metadata?``.
    The DTDs live in ``conductor/dtd``.
    """
    found: list[str] = []
    for parent in element.iter():
        later = None
        for child in parent:
            tag = local(child.tag)
            if tag in _AFTER_MARKERS and later is None:
                later = tag
            elif tag == "marker" and later is not None:
                name = parent.get("name") or parent.get("ref") or local(parent.tag)
                found.append(f"{name}: marker follows {later}")
                break
    return found


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
