"""Write an assembled :class:`Timeline` as FCPXML 1.11.

Connected items hang off the spine item that is playing at their start.
FCPXML gives an anchored item's ``offset`` in its parent's local time, which
starts at the parent's ``start``, so the offset written is
``parent.start + (item.offset - parent.offset)``. Keyframe and marker times
are in the item's own local time the same way.

Child order follows the DTD (``asset-clip``: intrinsic video adjustments,
``adjust-volume``, anchored items, then markers). Titles use Final Cut's
built-in Basic Title with the text style set per title, so SF Pro and the
profile's sizes, colours and tracking survive import. Stills use the
``FFVideoFormatRateUndefined`` format Final Cut writes for images.
"""

from __future__ import annotations

import bisect
import hashlib
import xml.etree.ElementTree as ET
from fractions import Fraction

from ..timeutil import format_time
from .layers import LayerMode, layers_from_timeline, mark_dissolves
from .timeline import Item, Keyframes, Media, TextStyle, Timeline, Transform

VERSION = "1.11"
BASIC_TITLE_UID = ".../Titles.localized/Bumper:Opener.localized/Basic Title.localized/Basic Title.moti"
CROSS_DISSOLVE_UID = (
    ".../Transitions.localized/Dissolves.localized/Cross Dissolve.localized/Cross Dissolve.motr"
)
POSITION_KEY = "9999/999166631/999166633/1/100/101"
_RATES = {
    Fraction(1001, 24000): "2398",
    Fraction(1, 24): "24",
    Fraction(1, 25): "25",
    Fraction(1001, 30000): "2997",
    Fraction(1, 30): "30",
    Fraction(1, 50): "50",
    Fraction(1001, 60000): "5994",
    Fraction(1, 60): "60",
}


class _Resources:
    def __init__(self, root: ET.Element):
        self.el = ET.SubElement(root, "resources")
        self.next = 1
        self.formats: dict[tuple, str] = {}
        self.assets: dict[str, str] = {}
        self.effect: str | None = None
        self.dissolve: str | None = None
        self.style_count = 0

    def ident(self) -> str:
        value = f"r{self.next}"
        self.next += 1
        return value

    def format(self, frame: Fraction | None, width: int, height: int) -> str:
        key = (frame, width, height)
        if key not in self.formats:
            ident = self.ident()
            attrs = {"id": ident}
            name = format_name(frame, width, height)
            if name:
                attrs["name"] = name
            if frame is not None:
                attrs["frameDuration"] = format_time(frame)
            attrs["width"] = str(width)
            attrs["height"] = str(height)
            if frame is not None:
                attrs["colorSpace"] = "1-1-1 (Rec. 709)"
            ET.SubElement(self.el, "format", attrs)
            self.formats[key] = ident
        return self.formats[key]

    def asset(self, media: Media, sequence_frame: Fraction) -> str:
        if media.key in self.assets:
            return self.assets[media.key]
        if media.kind == "still":
            fmt = self.format(None, media.width or 1920, media.height or 1080)
        elif media.kind == "video":
            fmt = self.format(media.frame_duration or sequence_frame, media.width or 1920, media.height or 1080)
        else:
            fmt = None
        ident = self.ident()
        attrs = {
            "id": ident,
            "name": media.name,
            "uid": media.uid,
            "start": "0s",
            "duration": format_time(media.duration),
            "hasVideo": "1" if media.has_video else "0",
            "hasAudio": "1" if media.has_audio else "0",
        }
        if fmt:
            attrs["format"] = fmt
        if media.has_video:
            attrs["videoSources"] = "1"
        if media.has_audio:
            attrs["audioSources"] = "1"
            attrs["audioChannels"] = str(media.audio_channels)
            attrs["audioRate"] = str(media.audio_rate)
        asset = ET.SubElement(self.el, "asset", attrs)
        ET.SubElement(asset, "media-rep", {"kind": "original-media", "src": media.src})
        self.assets[media.key] = ident
        return ident

    def title_effect(self) -> str:
        if self.effect is None:
            self.effect = self.ident()
            ET.SubElement(self.el, "effect", {"id": self.effect, "name": "Basic Title", "uid": BASIC_TITLE_UID})
        return self.effect

    def dissolve_effect(self) -> str:
        if self.dissolve is None:
            self.dissolve = self.ident()
            ET.SubElement(
                self.el,
                "effect",
                {"id": self.dissolve, "name": "Cross Dissolve", "uid": CROSS_DISSOLVE_UID},
            )
        return self.dissolve

    def style_id(self) -> str:
        self.style_count += 1
        return f"ts{self.style_count}"


def render(
    timeline: Timeline,
    *,
    event: str | None = None,
    dissolve_sections: set[str] | None = None,
    dissolve_seconds: float = 0.5,
) -> ET.ElementTree:
    if not timeline.spine:
        raise ValueError("refusing to render an empty timeline")
    root = ET.Element("fcpxml", {"version": VERSION})
    resources = _Resources(root)
    sequence_format = resources.format(timeline.frame, timeline.width, timeline.height)
    for item in timeline.spine + timeline.connected:
        if item.media is not None:
            resources.asset(item.media, timeline.frame)
        if item.kind == "title":
            resources.title_effect()
    name = timeline.name
    library = ET.SubElement(root, "library")
    event_el = ET.SubElement(library, "event", {"name": event or name, "uid": _uid("event", event or name)})
    project = ET.SubElement(event_el, "project", {"name": name, "uid": _uid("project", name)})
    sequence = ET.SubElement(
        project,
        "sequence",
        {
            "format": sequence_format,
            "duration": format_time(timeline.duration),
            "tcStart": "0s",
            "tcFormat": "NDF",
            "audioLayout": "stereo",
            "audioRate": "48k",
        },
    )
    layers = layers_from_timeline(timeline)
    spine_layer = layers[0]
    if spine_layer.mode is not LayerMode.SEQUENTIAL:
        raise ValueError("the storyline layer must be sequential")
    duration = Fraction(str(dissolve_seconds)) if dissolve_sections else Fraction(0)
    if dissolve_sections and duration > 0:
        mark_dissolves(spine_layer, dissolve_sections, duration, timeline.frame)
    overlaps = sum((clip.transition_duration or Fraction(0) for clip in spine_layer.clips), Fraction(0))
    if overlaps:
        sequence.set("duration", format_time(timeline.duration - overlaps))
        resources.dissolve_effect()
    spine_el = ET.SubElement(sequence, "spine")
    children: dict[int, list[Item]] = {i: [] for i in range(len(spine_layer.clips))}
    offsets = [clip.item.offset for clip in spine_layer.clips]
    for item in timeline.connected:
        index = max(0, bisect.bisect_right(offsets, item.offset) - 1)
        children[index].append(item)
    pulled = Fraction(0)
    for index, clip in enumerate(spine_layer.clips):
        anchored = sorted(children[index], key=lambda c: (c.offset, c.lane, c.name))
        _element(spine_el, clip.item, None, anchored, resources, timeline, shift=pulled)
        if clip.transition and clip.transition_duration:
            _transition(spine_el, clip.item, clip.transition_duration, pulled, resources)
            pulled += clip.transition_duration
    return ET.ElementTree(root)


def _element(
    parent_el: ET.Element,
    item: Item,
    parent: Item | None,
    anchored: list[Item],
    resources: _Resources,
    timeline: Timeline,
    shift: Fraction = Fraction(0),
) -> ET.Element:
    local_start = _local_start(item)
    if parent is None:
        offset = item.offset - shift
    else:
        offset = _local_start(parent) + (item.offset - parent.offset)
    attrs: dict[str, str] = {}
    if item.kind == "gap":
        tag = "gap"
        attrs = {"name": item.name, "offset": format_time(offset), "start": "0s", "duration": format_time(item.duration)}
    elif item.kind == "title":
        tag = "title"
        attrs = {"ref": resources.title_effect()}
    elif item.kind == "still":
        tag = "video"
        attrs = {"ref": resources.asset(item.media, timeline.frame)}
    else:
        tag = "asset-clip"
        attrs = {"ref": resources.asset(item.media, timeline.frame)}
    if tag != "gap":
        if item.lane:
            attrs["lane"] = str(item.lane)
        attrs["offset"] = format_time(offset)
        attrs["name"] = item.name
        attrs["start"] = format_time(local_start)
        attrs["duration"] = format_time(item.duration)
    if tag == "asset-clip":
        if item.media is not None and item.media.has_audio and item.src_enable != "video":
            attrs["audioRole"] = item.role or "dialogue"
        if item.src_enable:
            attrs["srcEnable"] = item.src_enable
        if item.audio_start is not None:
            attrs["audioStart"] = format_time(item.audio_start)
        if item.audio_duration is not None:
            attrs["audioDuration"] = format_time(item.audio_duration)
        attrs["tcFormat"] = "NDF"
    element = ET.SubElement(parent_el, tag, attrs)
    if tag == "title" and item.title is not None:
        _title_body(element, item, resources)
    if tag != "gap":
        _video_adjustments(element, item, local_start)
    if tag == "asset-clip":
        _volume(element, item, local_start)
    for child in anchored:
        _element(element, child, item, [], resources, timeline)
    _markers(element, item, local_start, timeline.frame)
    return element


def _title_body(element: ET.Element, item: Item, resources: _Resources) -> None:
    spec = item.title
    x, y = spec.position
    ET.SubElement(element, "param", {"name": "Position", "key": POSITION_KEY, "value": f"{_num(x)} {_num(y)}"})
    style_id = resources.style_id()
    text = ET.SubElement(element, "text")
    span = ET.SubElement(text, "text-style", {"ref": style_id})
    span.text = spec.text
    definition = ET.SubElement(element, "text-style-def", {"id": style_id})
    ET.SubElement(definition, "text-style", _style_attrs(spec.style))


def _style_attrs(style: TextStyle) -> dict[str, str]:
    attrs = {
        "font": style.font,
        "fontSize": _num(style.size),
        "fontFace": style.face,
        "fontColor": _rgba(style.color),
        "alignment": style.alignment,
    }
    if style.kerning:
        attrs["kerning"] = _num(style.kerning)
    if style.line_spacing:
        attrs["lineSpacing"] = _num(style.line_spacing)
    if style.stroke_color is not None and style.stroke_width:
        attrs["strokeColor"] = _rgba(style.stroke_color)
        attrs["strokeWidth"] = _num(style.stroke_width)
    if style.shadow_color is not None:
        attrs["shadowColor"] = _rgba(style.shadow_color)
        attrs["shadowOffset"] = f"{_num(style.shadow_distance or 0)} {_num(style.shadow_angle or 0)}"
        attrs["shadowBlurRadius"] = _num(style.shadow_blur or 0)
    return attrs


def _video_adjustments(element: ET.Element, item: Item, local_start: Fraction) -> None:
    transform = item.transform
    if transform is not None and transform.corners:
        attrs = {key: f"{_num(v[0])} {_num(v[1])}" for key, v in transform.corners.items()}
        ET.SubElement(element, "adjust-corners", attrs)
    if item.conform:
        ET.SubElement(element, "adjust-conform", {"type": item.conform})
    if transform is not None and not transform.is_identity():
        _transform(element, transform, local_start)
    if item.blend is not None:
        ET.SubElement(element, "adjust-blend", {"amount": _num(item.blend)})


def _transform(element: ET.Element, transform: Transform, local_start: Fraction) -> None:
    keys = transform.keyframes or Keyframes()
    attrs: dict[str, str] = {}
    if transform.position is not None and not keys.position:
        attrs["position"] = f"{_num(transform.position[0])} {_num(transform.position[1])}"
    if transform.scale is not None and not keys.scale:
        attrs["scale"] = f"{_num(transform.scale[0])} {_num(transform.scale[1])}"
    if transform.rotation and not keys.rotation:
        attrs["rotation"] = _num(transform.rotation)
    adjust = ET.SubElement(element, "adjust-transform", attrs)
    for name, frames in (("position", keys.position), ("scale", keys.scale), ("rotation", keys.rotation)):
        if not frames:
            continue
        param = ET.SubElement(adjust, "param", {"name": name})
        animation = ET.SubElement(param, "keyframeAnimation")
        for t, value in frames:
            text = _num(value) if isinstance(value, (int, float)) else f"{_num(value[0])} {_num(value[1])}"
            ET.SubElement(animation, "keyframe", {"time": format_time(local_start + t), "value": text, "curve": "smooth"})


def _transition(
    parent_el: ET.Element,
    left: Item,
    duration: Fraction,
    shift: Fraction,
    resources: _Resources,
) -> None:
    """Cross dissolve overlapping the cut, centred the way their window is.

    The transition starts ``duration`` before the left clip ends (after earlier
    overlaps have been pulled forward). FCPXML overlaps that tail with the
    head of the next clip. ``filter-video`` references the Cross Dissolve effect.
    """
    start = left.offset + left.duration - duration - shift
    effect = resources.dissolve_effect()
    element = ET.SubElement(
        parent_el,
        "transition",
        {"name": "Cross Dissolve", "offset": format_time(start), "duration": format_time(duration)},
    )
    ET.SubElement(element, "filter-video", {"ref": effect, "name": "Cross Dissolve"})


def _volume(element: ET.Element, item: Item, local_start: Fraction) -> None:
    """``adjust-volume`` keyframes. Music fades are their log-linear ramp in dB."""
    if item.volume_keys:
        adjust = ET.SubElement(element, "adjust-volume")
        param = ET.SubElement(adjust, "param", {"name": "amount"})
        animation = ET.SubElement(param, "keyframeAnimation")
        for key in item.volume_keys:
            attrs = {
                "time": format_time(local_start + (key.at - item.offset)),
                "value": f"{_num(key.db)}dB",
            }
            if key.interp != "linear":
                attrs["interp"] = key.interp
                attrs["curve"] = "smooth"
            else:
                attrs["curve"] = "linear"
            ET.SubElement(animation, "keyframe", attrs)
    elif item.volume_db is not None:
        ET.SubElement(element, "adjust-volume", {"amount": f"{_num(item.volume_db)}dB"})


def _markers(element: ET.Element, item: Item, local_start: Fraction, frame: Fraction) -> None:
    used: set[Fraction] = set()
    last = local_start + max(Fraction(0), item.duration - frame)
    for note in sorted(item.notes, key=lambda n: (not n.chapter, n.at)):
        start = local_start + (note.at - item.offset)
        start = min(max(start, local_start), last)
        while start in used and start < last:
            start += frame
        used.add(start)
        attrs = {"start": format_time(start), "duration": format_time(frame), "value": note.value}
        if note.chapter:
            attrs["note"] = note.note
            attrs["posterOffset"] = "0s"
            ET.SubElement(element, "chapter-marker", attrs)
            continue
        if note.todo:
            attrs["completed"] = "0"
        attrs["note"] = note.note
        ET.SubElement(element, "marker", attrs)


def _local_start(item: Item) -> Fraction:
    return item.start if item.kind == "clip" else Fraction(0)


def format_name(frame: Fraction | None, width: int, height: int) -> str | None:
    if frame is None:
        return "FFVideoFormatRateUndefined"
    rate = _RATES.get(Fraction(frame))
    if rate is None:
        return None
    if (width, height) in {(1280, 720), (1920, 1080), (3840, 2160)}:
        return f"FFVideoFormat{height}p{rate}"
    return f"FFVideoFormat{width}x{height}p{rate}"


def _rgba(colour: tuple[float, float, float, float]) -> str:
    return " ".join(_num(round(c, 4)) for c in colour)


def _num(value: float) -> str:
    if isinstance(value, Fraction):
        value = float(value)
    text = f"{float(value):.4f}".rstrip("0").rstrip(".")
    return text if text not in {"-0", ""} else "0"


def _uid(kind: str, text: str) -> str:
    return hashlib.blake2b(f"{kind}\0{text}".encode(), digest_size=8).hexdigest().upper()

