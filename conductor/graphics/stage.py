"""The graphics stage: subtitles, distorted titles, and the rectangle layer.

``apply_graphics`` is what ``iterate``, ``room-run``, and a future
``conductor.assemble`` call. It is off unless ``enabled`` is true or the
profile's ``graphics.enabled`` is true. Rendered movies go in
``<fcpxml stem>.assets/`` beside the output file, referenced by a relative
``media-rep`` ``src``. Missing ffmpeg, Pillow, or a font never fails the
run: that piece is skipped and the note is returned for ``room.md``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from fractions import Fraction
from pathlib import Path
from urllib.parse import quote
from xml.etree import ElementTree as ET

from ..fcpxml import Document, local, parse_fcpxml, write_document
from ..router import Router
from ..timeutil import format_time, parse_time
from ..timing import _conform_scale, has_time_map
from .diffusion import sample_keyframes
from .encode import Encoded, encode
from .profile import BLEND_MODES, GraphicsProfile, load_graphics_profile, parse_colour
from .render import FontChoice, TitleSpec, rect_schedule, rect_seed, resolve_font, title_frames
from .render import RenderUnavailable
from .subtitles import Cue, Word, load_words, plan_subtitles
from .titles import TitleCard, _case, plan_titles

BASIC_TITLE_UID = (
    ".../Titles.localized/Bumper:Opener.localized/Basic Title.localized/Basic Title.moti"
)
# Generator and filter ids follow Final Cut's localized effect paths. The
# rectangle motion itself is Diffusion Studio's RectangleClip (position,
# scale, opacity over time) plus its blur and hue-rotate effects, written
# as native FCPXML instead of a pre-rendered alpha movie.
SHAPES_UID = ".../Generators.localized/Elements.localized/Shapes.localized/Shapes.motn"
GAUSSIAN_UID = ".../Filters.localized/Blur.localized/Gaussian.localized/Gaussian.motn"
HUE_UID = ".../Filters.localized/Color.localized/Hue:Saturation.localized/Hue:Saturation.motn"
# Treatments Final Cut can do with a title and keyframes. The rest (glitch
# slice, RGB split, wave, blur) stay rendered alpha clips.
_NATIVE_TREATMENTS = frozenset({"none", "scale_warp"})
# Unspoken tail, from Diffusion Studio's WHISPER caption preset.
_DIM = "#C4C4C4"
_ANCHOR_BEFORE = frozenset(
    {
        "marker",
        "chapter-marker",
        "rating",
        "keyword",
        "analysis-marker",
        "audio-channel-source",
        "filter-video",
        "filter-video-mask",
        "filter-audio",
        "metadata",
    }
)
_MARKER = "byjwu title"


@dataclass
class GraphicsResult:
    enabled: bool
    fcpxml: Path | None = None
    assets_dir: Path | None = None
    subtitles: list[dict] = field(default_factory=list)
    titles: list[dict] = field(default_factory=list)
    rectangles: list[dict] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    decisions: list = field(default_factory=list)
    placeholder_fields: list[str] = field(default_factory=list)
    profile_source: str = ""

    def to_dict(self) -> dict:
        return {
            "enabled": self.enabled,
            "fcpxml": str(self.fcpxml) if self.fcpxml else None,
            "assets_dir": str(self.assets_dir) if self.assets_dir else None,
            "subtitles": len(self.subtitles),
            "titles": len(self.titles),
            "rectangles": len(self.rectangles),
            "notes": list(self.notes),
            "placeholder_fields": list(self.placeholder_fields),
            "profile_source": self.profile_source,
        }


def apply_graphics(
    fcpxml: str | Path,
    *,
    out_path: str | Path | None = None,
    words: str | Path | list | None = None,
    beats: str | Path | list | None = None,
    profile: GraphicsProfile | str | Path | dict | None = None,
    style: str | None = None,
    router: Router | None = None,
    brief: str = "",
    enabled: bool | None = None,
    ledger=None,
) -> GraphicsResult:
    """Write type and graphics onto a copy of ``fcpxml``. The input is not modified.

    ``enabled`` None follows the profile. False returns without writing.
    """
    loaded = profile if isinstance(profile, GraphicsProfile) else load_graphics_profile(profile or style)
    turn_on = loaded.enabled if enabled is None else bool(enabled)
    result = GraphicsResult(
        enabled=turn_on,
        placeholder_fields=loaded.placeholder_fields,
        profile_source=loaded.source,
    )
    if not turn_on:
        result.notes.append("Graphics stayed off. Pass --graphics, or set graphics.enabled in the style profile.")
        return result
    source = Path(fcpxml)
    document = parse_fcpxml(source)
    destination = Path(out_path) if out_path else source
    if destination.resolve() == source.resolve() and document.source is not None and out_path is None:
        destination = source.with_name(source.stem + ".graphics.fcpxml")
    if _already(document):
        result.notes.append("Graphics were already on this timeline, so this pass left them.")
        result.fcpxml = destination
        return result
    result.notes.append(
        "Type sizes, colours, and the rectangle palette are placeholder defaults, "
        "not measurements from Justin's videos, except fields the profile file set."
        if loaded.placeholder_fields
        else "Graphics values came from the style profile."
    )
    owned = router is None
    router = router or Router(live=False)
    try:
        heard = _words(words, document)
        beat_times = _beats(beats)
        assets = destination.with_name(destination.stem + ".assets")
        _build(document, destination, assets, heard, beat_times, loaded, router, brief, ledger, result)
    finally:
        if owned:
            router.close()
    write_document(document.tree, destination)
    result.fcpxml = destination
    result.assets_dir = assets if assets.is_dir() else None
    return result


def _build(document, destination, assets, words, beats, profile, router, brief, ledger, result: GraphicsResult) -> None:
    resources = _resources(document.tree.getroot())
    ids = _Ids(document.tree.getroot())
    title_effect = ids.effect(resources, "Basic Title", BASIC_TITLE_UID)
    title_font = resolve_font(profile.title_font(), profile.text_fx.face, profile.typography.fallback_fonts)
    if title_font.note() and title_font.note() not in result.notes:
        result.notes.append(title_font.note())
    for index, sequence in enumerate(document.sequences):
        if sequence.element is None:
            continue
        prefix = f"s{index}"
        seq_words = [word for word in words if word.sequence in ("", sequence.name)]
        if profile.subtitles.enabled and seq_words:
            plan = plan_subtitles(sequence, seq_words, profile, router, brief=brief, ledger=ledger, id_prefix=prefix)
            _subtitles(sequence, plan.cues, profile, title_effect, ids, result)
            result.subtitles.extend(cue.to_dict() for cue in plan.cues)
            result.decisions.extend(plan.decisions)
        cards: list[TitleCard] = []
        if profile.text_fx.enabled:
            cards, title_decisions, _receipts = plan_titles(
                sequence, profile, router, brief=brief, ledger=ledger, id_prefix=f"{prefix}t"
            )
            result.decisions.extend(title_decisions)
            _titles(sequence, cards, profile, title_font, destination, assets, ids, resources, result)
            result.titles.extend(card.to_dict() for card in cards)
        if profile.rect_layer.enabled and profile.rect_layer.coverage != "none":
            _rectangles(sequence, cards, profile, beats, destination, assets, ids, resources, result, frame_scale=profile.rect_layer.render_scale)


def _subtitles(sequence, cues: list[Cue], profile, effect_id, ids, result: GraphicsResult) -> None:
    by_clip: dict[str, list[Cue]] = {}
    for cue in cues:
        clip = sequence.spine[cue.span]
        by_clip.setdefault(clip.id, []).append(cue)
    lane = _free_lane(sequence, positive=True)
    for clip_id, group in by_clip.items():
        clip = next(item for item in sequence.spine if item.id == clip_id)
        colour = profile.subtitles.color or profile.typography.color
        size = profile.subtitles.size * (sequence.height or 1080) / 1080
        for cue in group:
            for start, end, active in _highlight_slices(cue):
                _title_element(
                    clip,
                    sequence,
                    effect_id,
                    lane,
                    start,
                    end - start,
                    _case("\n".join(cue.lines) or cue.text, profile.subtitles.casing),
                    profile.subtitle_font(),
                    profile.subtitles.face,
                    size,
                    colour,
                    profile.subtitles.position_y,
                    profile,
                    ids,
                    name="Subtitle",
                    runs=_highlight_runs(cue, active, profile.subtitles.face, colour, profile.subtitles.casing),
                )


def _titles(sequence, cards, profile, font: FontChoice, destination, assets, ids, resources, result) -> None:
    lane = _free_lane(sequence, positive=True)
    frame = sequence.frame_duration
    width, height = sequence.width or 1920, sequence.height or 1080
    scale = profile.rect_layer.render_scale
    for card in cards:
        if card.needs_review:
            _review_marker(sequence, card, profile)
        if card.treatment in _NATIVE_TREATMENTS:
            clip = document_clip(sequence, card.clip_id)
            duration = _fit(sequence, clip, card.timeline_start, _seconds(profile.text_fx.duration_seconds), frame)
            if duration <= 0:
                continue
            _title_element(
                clip, sequence, ids.effect(resources, "Basic Title", BASIC_TITLE_UID), lane,
                card.timeline_start, duration, card.text, profile.title_font(), profile.text_fx.face,
                profile.text_fx.size * height / 1080, profile.text_fx.color or profile.typography.color,
                profile.text_fx.position_y, profile, ids, name=card.text,
                keyframes=_title_motion(card.treatment, profile, float(duration)),
            )
            continue
        duration = _fit(
            sequence, document_clip(sequence, card.clip_id), card.timeline_start,
            _seconds(profile.text_fx.duration_seconds), frame,
        )
        if duration <= 0:
            continue
        frames = max(1, int(round(duration / frame)))
        spec = TitleSpec(
            card.text, card.treatment, max(16, int(width * scale)), max(16, int(height * scale)),
            float(1 / frame), frames, profile.text_fx.seed + int(card.timeline_start * 1000),
        )
        written = _render_mov(
            title_frames(spec, profile, font),
            assets / f"{card.id}.mov",
            spec.width, spec.height, frame, profile.codec, result, f"title {card.text!r}",
        )
        if written is None:
            continue
        _connected_asset(
            document_clip(sequence, card.clip_id), sequence, written, destination, ids, resources,
            lane, card.timeline_start, duration, f"byjwu title {card.text}", 1.0, "normal",
            width=spec.width, height=spec.height, asset_duration=frames * frame,
        )


def _rectangles(sequence, cards, profile, beats, destination, assets, ids, resources, result, frame_scale: float) -> None:
    layer = profile.rect_layer
    width, height = sequence.width or 1920, sequence.height or 1080
    windows = _windows(sequence, cards, profile)
    if not windows:
        result.notes.append("The rectangle layer had no window to cover, so none was rendered.")
        return
    lane = _free_lane(sequence, positive=layer.placement == "over")
    if beats is None and layer.beat_sync:
        result.notes.append(
            "No music beat grid was available, so the rectangle layer did not cut to a beat. "
            "Pass beats, or a beats.json of seconds beside the timeline."
        )
    del destination, assets, frame_scale
    shapes = ids.effect(resources, "Shapes", SHAPES_UID)
    blur_id = ids.effect(resources, "Gaussian", GAUSSIAN_UID)
    hue_id = ids.effect(resources, "Hue/Saturation", HUE_UID)
    for number, (start, end) in enumerate(windows):
        duration = end - start
        local_beats = None
        if beats is not None and layer.beat_sync:
            local_beats = [float(beat - start) for beat in beats if start <= beat < end]
        seed = rect_seed(layer, (sequence.name, number, format_time(start)))
        rects = rect_schedule(layer, float(duration), seed, local_beats)
        placed = 0
        for rect in rects:
            born = start + _seconds(max(0.0, rect.born))
            dies = start + _seconds(min(float(duration), rect.dies))
            if dies <= born:
                continue
            for clip in sequence.spine:
                overlap_start = max(born, clip.timeline_start)
                overlap_end = min(dies, clip.timeline_end)
                if overlap_end <= overlap_start or clip.element is None:
                    continue
                _shape_clip(
                    clip, sequence, shapes, blur_id, hue_id, lane,
                    overlap_start, overlap_end - overlap_start, rect, born, layer, width, height,
                )
                placed += 1
        result.rectangles.append(
            {"sequence": sequence.name, "start_seconds": float(start), "shapes": placed, "seed": seed}
        )


def _windows(sequence, cards, profile: GraphicsProfile) -> list[tuple[Fraction, Fraction]]:
    if not sequence.spine:
        return []
    origin, ending = sequence.spine[0].timeline_start, sequence.spine[-1].timeline_end
    if profile.rect_layer.coverage == "full":
        return [(origin, ending)] if ending > origin else []
    pad = _seconds(profile.rect_layer.pad_seconds)
    hold = _seconds(profile.text_fx.duration_seconds)
    windows = []
    for card in cards:
        start, end = max(origin, card.timeline_start - pad), min(ending, card.timeline_start + hold + pad)
        if end > start:
            windows.append((start, end))
    return _merge(windows)


def _render_mov(frames, path: Path, width, height, frame, codec, result: GraphicsResult, label: str) -> Encoded | None:
    try:
        return encode(frames, path, width=width, height=height, frame_duration=frame, codec=codec)
    except RenderUnavailable as exc:
        note = f"{label}: {exc}"
        if note not in result.notes:
            result.notes.append(note)
        return None


class _Ids:
    def __init__(self, root: ET.Element) -> None:
        self.used = {elem.get("id") for elem in root.iter() if elem.get("id")}
        self.n = 1
        self.styles = 0

    def fresh(self, prefix: str) -> str:
        while True:
            ident = f"{prefix}{self.n}"
            self.n += 1
            if ident not in self.used:
                self.used.add(ident)
                return ident

    def effect(self, resources: ET.Element, name: str, uid: str) -> str:
        for child in resources:
            if local(child.tag) == "effect" and child.get("uid") == uid:
                return child.get("id") or ""
        ident = self.fresh("r")
        effect = ET.Element("effect", {"id": ident, "name": name, "uid": uid})
        resources.append(effect)
        return ident

    def style(self) -> str:
        self.styles += 1
        ident = f"ts{self.styles}"
        while ident in self.used:
            self.styles += 1
            ident = f"ts{self.styles}"
        self.used.add(ident)
        return ident


def _title_element(
    clip, sequence, effect_id, lane, start, duration, text, font, face, size, colour, position_y, profile, ids, name,
    runs=None, keyframes=None,
) -> None:
    if clip.element is None or duration <= 0:
        return
    title = ET.Element("title")
    title.set("ref", effect_id)
    title.set("lane", str(lane))
    title.set("offset", format_time(_offset(clip, sequence, start)))
    title.set("name", name)
    title.set("start", "0s")
    title.set("duration", format_time(duration))
    title.set("role", "Titles")
    body = ET.SubElement(title, "text")
    pieces = runs or [(text, face, colour)]
    defs = []
    shadow = parse_colour(profile.typography.shadow_color)
    for chunk, chunk_face, chunk_colour in pieces:
        style_id = ids.style()
        node = ET.SubElement(body, "text-style", {"ref": style_id})
        node.text = chunk
        red, green, blue = parse_colour(chunk_colour)
        style = ET.Element("text-style")
        style.set("font", font)
        style.set("fontSize", f"{size:g}")
        style.set("fontFace", chunk_face)
        style.set("fontColor", f"{red / 255:.4f} {green / 255:.4f} {blue / 255:.4f} 1")
        style.set("alignment", "center")
        style.set("shadowColor", f"{shadow[0] / 255:.4f} {shadow[1] / 255:.4f} {shadow[2] / 255:.4f} {profile.typography.shadow_opacity:g}")
        style.set("shadowOffset", f"{profile.typography.shadow_offset:g} {profile.typography.shadow_angle:g}")
        style.set("shadowBlurRadius", f"{profile.typography.shadow_blur:g}")
        defs.append((style_id, style))
    for style_id, style in defs:
        ET.SubElement(title, "text-style-def", {"id": style_id}).append(style)
    height = sequence.height or 1080
    y = (0.5 - position_y) * height
    transform = ET.SubElement(title, "adjust-transform", {"position": f"0 {y:g}"})
    if keyframes:
        _keyframe_param(transform, "scale", keyframes)
    _insert_anchor(clip.element, title)


def _highlight_slices(cue: Cue) -> list[tuple[Fraction, Fraction, int]]:
    """One title per spoken word, tiling the cue. The last slice holds to cue.end."""
    if not cue.words:
        return [(cue.start, cue.end, 0)] if cue.end > cue.start else []
    slices = []
    cursor = cue.start
    for index, word in enumerate(cue.words):
        start = max(cursor, min(word.start, cue.end))
        if index + 1 < len(cue.words):
            end = min(cue.end, max(start, cue.words[index + 1].start))
        else:
            end = cue.end
        if end > start:
            slices.append((start, end, index))
            cursor = end
    return slices


def _highlight_runs(cue: Cue, active: int, face: str, colour: str, casing: str = "as-is") -> list[tuple[str, str, str]]:
    """WHISPER-style active word: the spoken word stays the subtitle colour; the rest is dim."""
    runs = []
    for index, word in enumerate(cue.words):
        shown = _case(word.text, casing)
        chunk = shown if index == 0 else " " + shown
        if index == active:
            runs.append((chunk, face, colour))
        else:
            runs.append((chunk, face, _DIM))
    return runs or [(cue.text, face, colour)]


def _title_motion(treatment: str, profile: GraphicsProfile, duration: float) -> list[tuple[str, str]] | None:
    """Scale keyframes for ``scale_warp``. Values are Diffusion Studio factors, written as FCP percent."""
    if treatment != "scale_warp" or duration <= 0:
        return None
    fx = profile.text_fx
    start_scale = (fx.scale_from[0] + fx.scale_from[1]) / 2
    end_scale = (fx.scale_to[0] + fx.scale_to[1]) / 2
    span = min(float(fx.scale_seconds), duration)
    frames = [(0.0, start_scale), (span, end_scale)]
    points = [0.0, span] if span > 0 else [0.0]
    return [
        (format_time(_seconds(time)), f"{sample_keyframes(frames, time, 'smooth') * 100:g} {sample_keyframes(frames, time, 'smooth') * 100:g}")
        for time in points
    ]


def _keyframe_param(parent: ET.Element, name: str, points: list[tuple[str, str]]) -> None:
    param = ET.SubElement(parent, "param", {"name": name})
    animation = ET.SubElement(param, "keyframeAnimation")
    for index, (time, value) in enumerate(points):
        attrs = {"time": time, "value": value}
        if index + 1 < len(points):
            attrs["interp"] = "ease"
        ET.SubElement(animation, "keyframe", attrs)


def _shape_clip(clip, sequence, shapes, blur_id, hue_id, lane, start, duration, rect, born, layer, width, height) -> None:
    """One RectangleClip as a Shapes generator, with blur and hue-rotate filters."""
    if clip.element is None or duration <= 0:
        return
    local_start = float(start - born)
    local_end = local_start + float(duration)
    age0 = max(0.0, local_start - max(0.0, rect.born))
    age1 = max(age0, local_end - max(0.0, rect.born))
    aspect = width / height if height else 1
    def position(age: float) -> str:
        cx = rect.x + rect.vx * age
        cy = rect.y + rect.vy * age
        x = (cx - 0.5) * aspect * 100
        y = (0.5 - cy) * 100
        return f"{x:g} {y:g}"
    scale0 = max(0.05, (1 + rect.grow * age0) * rect.w) * 100
    scale1 = max(0.05, (1 + rect.grow * age1) * rect.w) * 100
    node = ET.Element("video")
    node.set("ref", shapes)
    node.set("lane", str(lane))
    node.set("offset", format_time(_offset(clip, sequence, start)))
    node.set("name", "byjwu rectangles")
    node.set("start", "3600s")
    node.set("duration", format_time(duration))
    transform = ET.SubElement(node, "adjust-transform", {"position": position(age0)})
    _keyframe_param(
        transform,
        "position",
        [("0s", position(age0)), (format_time(duration), position(age1))],
    )
    _keyframe_param(
        transform,
        "scale",
        [("0s", f"{scale0:g} {scale0:g}"), (format_time(duration), f"{scale1:g} {scale1:g}")],
    )
    ET.SubElement(node, "adjust-blend", {"amount": f"{layer.opacity:g}", "mode": str(BLEND_MODES.get(layer.blend_mode, 0))})
    blur = ET.SubElement(node, "filter-video", {"ref": blur_id, "name": "Gaussian"})
    amount = max(0.0, layer.stroke * height)
    ET.SubElement(blur, "param", {"name": "Amount", "value": f"{amount:g}"})
    hue = ET.SubElement(node, "filter-video", {"ref": hue_id, "name": "Hue/Saturation"})
    ET.SubElement(hue, "param", {"name": "Hue", "value": f"{(rect.colour % 12) * 30:g}"})
    _insert_anchor(clip.element, node)


def _connected_asset(
    clip, sequence, encoded: Encoded, fcpxml: Path, ids, resources, lane, start, duration, name,
    opacity, blend, movie_start: Fraction = Fraction(0), *, width: int, height: int, asset_duration: Fraction,
) -> None:
    if clip.element is None:
        return
    rel = encoded.path.relative_to(fcpxml.parent)
    src = "/".join(quote(part) for part in rel.parts)
    asset_id = ids.fresh("r")
    format_id = ids.fresh("r")
    ET.SubElement(
        resources, "format",
        {"id": format_id, "frameDuration": format_time(sequence.frame_duration), "width": str(width), "height": str(height)},
    )
    asset = ET.SubElement(
        resources, "asset",
        {
            "id": asset_id, "name": name, "start": "0s", "duration": format_time(asset_duration),
            "hasVideo": "1", "hasAudio": "0", "format": format_id, "videoSources": "1",
        },
    )
    ET.SubElement(asset, "media-rep", {"kind": "original-media", "src": src})
    node = ET.Element("asset-clip")
    node.set("ref", asset_id)
    node.set("lane", str(lane))
    node.set("offset", format_time(_offset(clip, sequence, start)))
    node.set("name", name)
    node.set("start", format_time(movie_start))
    node.set("duration", format_time(duration))
    ET.SubElement(node, "adjust-blend", {"amount": f"{opacity:g}", "mode": str(BLEND_MODES.get(blend, 0))})
    _insert_anchor(clip.element, node)


def _offset(clip, sequence, timeline_pos: Fraction) -> Fraction:
    local_pos = timeline_pos - clip.timeline_start
    if local_pos < 0:
        local_pos = Fraction(0)
    origin = parse_time(clip.element.get("start"), Fraction(0)) if clip.element is not None else clip.start
    if clip.element is not None and has_time_map(clip.element):
        return origin + local_pos
    scale = _conform_scale(clip.element, sequence.frame_duration) if clip.element is not None else Fraction(1)
    if scale == 0:
        scale = Fraction(1)
    return origin + local_pos / scale


def _insert_anchor(parent: ET.Element, node: ET.Element) -> None:
    for index, child in enumerate(list(parent)):
        if local(child.tag) in _ANCHOR_BEFORE:
            parent.insert(index, node)
            return
    parent.append(node)


def _review_marker(sequence, card: TitleCard, profile: GraphicsProfile) -> None:
    clip = document_clip(sequence, card.clip_id)
    if clip.element is None:
        return
    marker = ET.Element("marker")
    marker.set("start", format_time(_offset(clip, sequence, card.timeline_start)))
    marker.set("duration", format_time(sequence.frame_duration))
    marker.set("value", f"{_MARKER}: {card.text}")
    marker.set("note", "byjwu: Opus was unavailable, so this title used the default placement and treatment. Review it.")
    marker.set("completed", "0")
    clip.element.append(marker)


def _free_lane(sequence, *, positive: bool) -> int:
    used = set()
    for clip in sequence.spine:
        if clip.element is None:
            continue
        for child in clip.element:
            raw = child.get("lane")
            if raw and raw.lstrip("-").isdigit():
                used.add(int(raw))
    lane = 1 if positive else -1
    while lane in used:
        lane += 1 if positive else -1
    used.add(lane)
    return lane


def document_clip(sequence, clip_id):
    return next(clip for clip in sequence.spine if clip.id == clip_id)


def _fit(sequence, clip, start: Fraction, want: Fraction, frame: Fraction) -> Fraction:
    room = clip.timeline_end - start
    duration = min(want, room)
    frames = int(duration / frame)
    return frames * frame


def _seconds(value: float) -> Fraction:
    return Fraction(value).limit_denominator(1000)


def _resources(root: ET.Element) -> ET.Element:
    found = next((child for child in root if local(child.tag) == "resources"), None)
    if found is None:
        found = ET.Element("resources")
        root.insert(0, found)
    return found


def _already(document: Document) -> bool:
    return any(asset.name.startswith("byjwu ") for asset in document.assets.values())


def _merge(windows: list[tuple[Fraction, Fraction]]) -> list[tuple[Fraction, Fraction]]:
    merged: list[tuple[Fraction, Fraction]] = []
    for start, end in sorted(windows):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def _words(words, document: Document) -> list[Word]:
    if words is None and document.source is not None:
        sidecar = document.source.with_suffix(".words.json")
        if sidecar.is_file():
            words = sidecar
    return load_words(words)


def _beats(beats) -> list[Fraction] | None:
    if beats is None:
        return None
    if isinstance(beats, (str, Path)):
        path = Path(beats)
        if not path.is_file():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        beats = payload.get("beats", payload) if isinstance(payload, dict) else payload
    return [Fraction(str(item)).limit_denominator(10000) for item in beats]


