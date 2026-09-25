"""A useful subset of FCPXML: projects, clips, assets, timing, markers, roles.

The parser keeps the ElementTree. Marker write-back appends ``<marker>``
elements to those nodes and writes the same tree, so unknown effects, keywords,
roles, filters, and keyframes survive. This is not a full DTD implementation —
compound clips that live only inside a ``<media>`` resource are not walked.

Anchored items in a real Final Cut export store ``offset`` in the parent
clip's timebase (the same numbers as the parent's ``start``). A lower third
at the head of a clip whose ``start`` is ``12s`` is ``offset="12s"``, not
``offset="0s"``. Hand-built XML in this repo uses seconds from the in point.
:func:`anchored_local` picks the reading that actually lands on the parent.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from fractions import Fraction
from pathlib import Path

from .errors import ConductorError
from .timeutil import parse_time

#: Spine and connected items we know how to time. Anything else is preserved
#: on write and ignored when looking for candidates.
CLIP_TAGS = frozenset(
    {
        "asset-clip",
        "audio",
        "audition",
        "clip",
        "gap",
        "live-drawing",
        "mc-clip",
        "ref-clip",
        "sync-clip",
        "title",
        "video",
    }
)


def local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


@dataclass
class FormatInfo:
    id: str
    name: str | None
    frame_duration: Fraction
    width: int | None
    height: int | None


@dataclass
class Asset:
    id: str
    name: str
    src: str | None
    start: Fraction
    duration: Fraction
    has_video: bool
    has_audio: bool
    format_id: str | None


@dataclass
class XmlMarker:
    start: Fraction
    duration: Fraction | None
    value: str
    note: str | None
    completed: str | None


@dataclass
class Clip:
    id: str
    kind: str
    name: str
    ref: str | None
    offset: Fraction
    start: Fraction
    duration: Fraction
    parent_offset: Fraction
    audio_role: str | None
    video_role: str | None
    roles: tuple[str, ...]
    lane: int | None
    connected: bool
    markers: list[XmlMarker] = field(default_factory=list)
    connected_clips: list[Clip] = field(default_factory=list)
    element: ET.Element | None = None
    width: int | None = None
    height: int | None = None
    #: Seconds from the parent's in point. Spine items use the ``offset`` attribute.
    local_offset: Fraction | None = None
    #: ``spine`` for primary items, ``media`` for Final Cut's timebase, ``edit`` for seconds-from-in-point.
    anchor: str = "spine"
    asset_id: str | None = None
    conform: str | None = None
    source_frame: Fraction | None = None

    @property
    def timeline_start(self) -> Fraction:
        local = self.offset if self.local_offset is None else self.local_offset
        return self.parent_offset + local

    @property
    def timeline_end(self) -> Fraction:
        return self.timeline_start + self.duration

    @property
    def role(self) -> str | None:
        if self.audio_role:
            return self.audio_role
        if self.video_role:
            return self.video_role
        if self.roles:
            return self.roles[0]
        return None


@dataclass
class Sequence:
    name: str
    event: str | None
    duration: Fraction | None
    tc_start: Fraction
    tc_format: str | None
    format_id: str | None
    frame_duration: Fraction
    spine: list[Clip]
    element: ET.Element | None = None
    width: int | None = None
    height: int | None = None


@dataclass
class Document:
    version: str
    source: Path | None
    formats: dict[str, FormatInfo]
    assets: dict[str, Asset]
    sequences: list[Sequence]
    clips: dict[str, Clip]
    tree: ET.ElementTree


def parse_fcpxml(path: str | Path) -> Document:
    file = Path(path)
    if not file.is_file():
        raise ConductorError(f"no such FCPXML: {file}")
    try:
        tree = ET.parse(file)
    except ET.ParseError as exc:
        raise ConductorError(f"FCPXML did not parse: {file}: {exc}") from exc
    return _document(tree.getroot(), tree, file)


def parse_xml(text: str, source: Path | None = None) -> Document:
    try:
        root = ET.fromstring(text)
    except ET.ParseError as exc:
        raise ConductorError(f"FCPXML did not parse: {exc}") from exc
    return _document(root, ET.ElementTree(root), source)


def write_document(tree: ET.ElementTree, path: Path) -> None:
    """Write a new FCPXML file. Does not touch the source path."""
    root = tree.getroot()
    ET.indent(root, space="  ")
    body = ET.tostring(root, encoding="unicode")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        '<?xml version="1.0" encoding="UTF-8"?>\n<!DOCTYPE fcpxml>\n' + body + "\n",
        encoding="utf-8",
    )


def _document(root: ET.Element, tree: ET.ElementTree, source: Path | None) -> Document:
    if local(root.tag) != "fcpxml":
        raise ConductorError(
            f"not an FCPXML document (root is <{local(root.tag)}>, expected <fcpxml>)"
        )
    formats = _formats(root)
    assets = _assets(root)
    clips: dict[str, Clip] = {}
    sequences: list[Sequence] = []
    for event_name, project in _projects(root):
        sequence_el = next(
            (child for child in project if local(child.tag) == "sequence"), None
        )
        if sequence_el is None:
            continue
        name = project.get("name") or "Untitled"
        format_id = sequence_el.get("format")
        frame = Fraction(1, 24)
        seq_width = seq_height = None
        if format_id and format_id in formats:
            frame = formats[format_id].frame_duration
            seq_width = formats[format_id].width
            seq_height = formats[format_id].height
        spine_el = next(
            (child for child in sequence_el if local(child.tag) == "spine"), None
        )
        spine: list[Clip] = []
        if spine_el is not None:
            index = 0
            for child in spine_el:
                if local(child.tag) not in CLIP_TAGS:
                    continue
                clip = _clip(
                    child,
                    clip_id=f"s{len(sequences)}c{index}",
                    parent_offset=Fraction(0),
                    parent_start=Fraction(0),
                    parent_duration=Fraction(0),
                    connected=False,
                    assets=assets,
                    formats=formats,
                    frame=frame,
                )
                _index(clips, clip)
                spine.append(clip)
                index += 1
        duration_raw = sequence_el.get("duration")
        sequences.append(
            Sequence(
                name=name,
                event=event_name,
                duration=parse_time(duration_raw) if duration_raw else None,
                tc_start=parse_time(sequence_el.get("tcStart"), Fraction(0)),
                tc_format=sequence_el.get("tcFormat"),
                format_id=format_id,
                frame_duration=frame,
                spine=spine,
                element=sequence_el,
                width=seq_width,
                height=seq_height,
            )
        )
    if not sequences:
        raise ConductorError("FCPXML has no project sequence to read")
    return Document(
        version=root.get("version") or "",
        source=source,
        formats=formats,
        assets=assets,
        sequences=sequences,
        clips=clips,
        tree=tree,
    )


def _index(clips: dict[str, Clip], clip: Clip) -> None:
    clips[clip.id] = clip
    for child in clip.connected_clips:
        _index(clips, child)


def _projects(root: ET.Element):
    for child in root:
        tag = local(child.tag)
        if tag == "project":
            yield None, child
        elif tag == "library":
            for event in child:
                if local(event.tag) != "event":
                    continue
                for project in event:
                    if local(project.tag) == "project":
                        yield event.get("name"), project


def _formats(root: ET.Element) -> dict[str, FormatInfo]:
    found: dict[str, FormatInfo] = {}
    for elem in root.iter():
        if local(elem.tag) != "format" or not elem.get("id"):
            continue
        frame_raw = elem.get("frameDuration")
        found[elem.get("id")] = FormatInfo(
            id=elem.get("id") or "",
            name=elem.get("name"),
            frame_duration=parse_time(frame_raw, Fraction(1, 24)),
            width=_int_attr(elem.get("width")),
            height=_int_attr(elem.get("height")),
        )
    return found


def _assets(root: ET.Element) -> dict[str, Asset]:
    found: dict[str, Asset] = {}
    for elem in root.iter():
        if local(elem.tag) != "asset" or not elem.get("id"):
            continue
        src = None
        for child in elem:
            if local(child.tag) == "media-rep" and child.get("src"):
                src = child.get("src")
                break
        asset_id = elem.get("id") or ""
        found[asset_id] = Asset(
            id=asset_id,
            name=elem.get("name") or asset_id,
            src=src,
            start=parse_time(elem.get("start"), Fraction(0)),
            duration=parse_time(elem.get("duration"), Fraction(0)),
            has_video=_flag(elem.get("hasVideo")),
            has_audio=_flag(elem.get("hasAudio")),
            format_id=elem.get("format"),
        )
    return found


def anchored_local(
    parent_start: Fraction,
    parent_duration: Fraction,
    offset: Fraction,
    duration: Fraction,
    *,
    frame: Fraction = Fraction(1, 24),
) -> tuple[Fraction, str]:
    """Seconds from the parent's in point, and which offset convention that was.

    ``media`` means ``offset`` lives in the parent's timebase, so the local
    position is ``offset - parent_start``. ``edit`` means ``offset`` is already
    seconds from the in point. A few frames of overhang still count as landing
    on the parent, which is how a J-cut is stored.
    """
    if parent_duration <= 0:
        return offset, "edit"
    slop = frame * 12 if frame > 0 else Fraction(1, 2)
    media = offset - parent_start
    edit = offset
    media_hit = _window_overlap(media, duration, parent_duration, slop)
    edit_hit = _window_overlap(edit, duration, parent_duration, slop)
    if media_hit > edit_hit:
        return media, "media"
    if edit_hit > media_hit:
        return edit, "edit"
    if parent_start == 0 or media == edit:
        return edit, "edit"
    if abs(offset - parent_start) <= abs(offset):
        return media, "media"
    return edit, "edit"


def _window_overlap(
    local: Fraction, duration: Fraction, parent_duration: Fraction, slop: Fraction
) -> Fraction:
    left = max(local, -slop)
    right = min(local + duration, parent_duration + slop)
    if right <= left:
        return Fraction(0)
    return right - left


def _clip(
    elem: ET.Element,
    clip_id: str,
    parent_offset: Fraction,
    parent_start: Fraction,
    parent_duration: Fraction,
    connected: bool,
    assets: dict[str, Asset],
    formats: dict[str, FormatInfo],
    frame: Fraction,
) -> Clip:
    offset = parse_time(elem.get("offset"), Fraction(0))
    start = parse_time(elem.get("start"), Fraction(0))
    duration = parse_time(elem.get("duration"), Fraction(0))
    if connected:
        local_offset, anchor = anchored_local(
            parent_start, parent_duration, offset, duration, frame=frame
        )
    else:
        local_offset, anchor = offset, "spine"
    timeline_start = parent_offset + local_offset
    roles: list[str] = []
    markers: list[XmlMarker] = []
    connected_clips: list[Clip] = []
    conform = None
    child_index = 0
    for child in elem:
        tag = local(child.tag)
        if tag in {"audio-role-source", "video-role-source"} and child.get("role"):
            roles.append(child.get("role") or "")
        elif tag == "marker":
            markers.append(_marker(child))
        elif tag == "conform-rate" and conform is None:
            conform = child.get("srcFrameRate")
        elif tag in CLIP_TAGS:
            connected_clips.append(
                _clip(
                    child,
                    clip_id=f"{clip_id}k{child_index}",
                    parent_offset=timeline_start,
                    parent_start=start,
                    parent_duration=duration,
                    connected=True,
                    assets=assets,
                    formats=formats,
                    frame=frame,
                )
            )
            child_index += 1
    name = elem.get("name") or elem.get("ref") or local(elem.tag)
    lane_raw = elem.get("lane")
    ref = elem.get("ref")
    width, height = _frame_size(ref, assets, formats)
    asset_id = ref if ref in assets else None
    if asset_id is None:
        for child in elem:
            if child.get("lane"):
                continue
            if local(child.tag) not in {"video", "asset-clip", "audio"}:
                continue
            child_ref = child.get("ref")
            if child_ref in assets:
                asset_id = child_ref
                break
    format_id = elem.get("format")
    source_frame = None
    if format_id and format_id in formats:
        source_frame = formats[format_id].frame_duration
    return Clip(
        id=clip_id,
        kind=local(elem.tag),
        name=name,
        ref=ref,
        offset=offset,
        start=start,
        duration=duration,
        parent_offset=parent_offset,
        audio_role=elem.get("audioRole"),
        video_role=elem.get("videoRole"),
        roles=tuple(roles),
        lane=int(lane_raw) if lane_raw and lane_raw.lstrip("-").isdigit() else None,
        connected=connected,
        markers=markers,
        connected_clips=connected_clips,
        element=elem,
        width=width,
        height=height,
        local_offset=local_offset,
        anchor=anchor,
        asset_id=asset_id,
        conform=conform,
        source_frame=source_frame,
    )


def _frame_size(
    ref: str | None,
    assets: dict[str, Asset],
    formats: dict[str, FormatInfo],
) -> tuple[int | None, int | None]:
    if not ref:
        return None, None
    asset = assets.get(ref)
    if asset is None or not asset.format_id:
        return None, None
    info = formats.get(asset.format_id)
    if info is None:
        return None, None
    return info.width, info.height


def _marker(elem: ET.Element) -> XmlMarker:
    duration_raw = elem.get("duration")
    return XmlMarker(
        start=parse_time(elem.get("start"), Fraction(0)),
        duration=parse_time(duration_raw) if duration_raw else None,
        value=elem.get("value") or "",
        note=elem.get("note"),
        completed=elem.get("completed"),
    )


def _flag(value: str | None) -> bool:
    return value == "1"


def _int_attr(value: str | None) -> int | None:
    if value and value.isdigit():
        return int(value)
    return None
