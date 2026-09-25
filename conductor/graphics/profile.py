"""The type and graphics section of a style profile, and every placeholder default.

PLACEHOLDER VALUES. Nothing in the defaults below was measured from Justin's
videos. They are neutral starting points (SF Pro Display / SF Pro Text, clean
white with a subtle shadow, a sparse white rectangle layer) so the graphics
stage runs end to end before the style study fills them in. The profile build
owns ``styles/<name>/profile.json``; this module is the one place that reads
it for graphics.

Two ways a profile fills these sections in:

1. A ``graphics`` object (or the same four keys at the top level) in the
   profile JSON, in this schema::

       {"graphics": {"enabled": true,
                     "typography": {...}, "subtitles": {...},
                     "text_fx": {...}, "rect_layer": {...}}}

   Keys are the dataclass field names below. Unknown keys are an error, so a
   typo does not silently fall back to a placeholder.

2. The measured parameter tree (``params.<group>.<param>.value``) the style
   study writes. ``PARAM_MAP`` lists which measured parameters map onto which
   field. Anything it does not list stays a placeholder.

``GraphicsProfile.provenance`` records, per field, whether the value is
``placeholder`` or came from a file, so reports can say which is which.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, fields, replace
from pathlib import Path
from typing import Any

from ..errors import ConductorError

PLACEHOLDER = "placeholder"

TREATMENTS = ("glitch_slice", "rgb_split", "wave", "scale_warp", "blur_in", "none")

#: Final Cut's numeric ``adjust-blend`` modes.
BLEND_MODES = {
    "normal": 0,
    "subtract": 2,
    "darken": 3,
    "multiply": 4,
    "color_burn": 5,
    "linear_burn": 6,
    "add": 8,
    "lighten": 9,
    "screen": 10,
    "color_dodge": 11,
    "linear_dodge": 12,
    "overlay": 14,
    "soft_light": 15,
    "hard_light": 16,
    "difference": 22,
    "exclusion": 23,
}


@dataclass(frozen=True)
class Typography:
    #: Family names only. SF Pro is not redistributable, so no font file is shipped.
    display_font: str = "SF Pro Display"
    text_font: str = "SF Pro Text"
    #: Tried in order when rendering on a machine without SF Pro. The XML still names SF Pro.
    fallback_fonts: tuple[str, ...] = ("Inter", "Arimo", "Liberation Sans", "DejaVu Sans")
    color: str = "#FFFFFF"
    shadow_color: str = "#000000"
    shadow_opacity: float = 0.5
    shadow_offset: float = 2.0
    shadow_angle: float = 315.0
    shadow_blur: float = 3.0
    casing: str = "as-is"


@dataclass(frozen=True)
class Subtitles:
    enabled: bool = True
    font: str | None = None
    face: str = "Semibold"
    #: Final Cut text size, in pixels on a 1080-line frame. Scaled for other heights.
    size: float = 44.0
    color: str | None = None
    #: Centre of the text block, as a fraction of frame height from the top.
    position_y: float = 0.88
    max_chars_per_line: int = 32
    max_lines: int = 2
    min_seconds: float = 0.8
    max_seconds: float = 4.0
    #: A cue shorter than this after timing is a flash and is dropped rather than shown.
    flash_seconds: float = 0.25
    #: Gap to the next cue that a cue holds across, so text does not blink off and on.
    bridge_seconds: float = 0.3
    #: Silence that ends a phrase.
    phrase_pause_seconds: float = 0.45
    #: A cut-off word is shown when at least this share of it is heard.
    partial_keep_share: float = 0.5
    strip_end_punctuation: bool = False


@dataclass(frozen=True)
class TextFX:
    enabled: bool = True
    font: str | None = None
    face: str = "Bold"
    size: float = 140.0
    color: str | None = None
    position_y: float = 0.5
    duration_seconds: float = 3.0
    fade_out_seconds: float = 0.4
    default_treatment: str = "blur_in"
    treatments: tuple[str, ...] = TREATMENTS
    max_titles: int = 8
    max_chars: int = 32
    #: A title candidate needs this much time since the previous one.
    min_spacing_seconds: float = 20.0
    #: Speech pause that marks a section boundary when there is no chapter marker.
    section_pause_seconds: float = 1.5
    glitch_slices: int = 10
    glitch_max_offset: float = 0.06
    glitch_seconds: float = 0.6
    rgb_offset: float = 0.012
    rgb_seconds: float = 0.8
    wave_amplitude: float = 0.02
    wave_wavelength: float = 0.25
    wave_seconds: float = 1.0
    scale_from: tuple[float, float] = (1.0, 1.0)
    scale_to: tuple[float, float] = (1.0, 1.0)
    scale_seconds: float = 0.6
    blur_radius: float = 0.02
    blur_seconds: float = 0.5
    seed: int = 0


@dataclass(frozen=True)
class RectLayer:
    enabled: bool = True
    seed: int = 0
    palette: tuple[str, ...] = ("#FFFFFF",)
    #: Average rectangles on screen at once.
    density: float = 4.0
    min_size: float = 0.08
    max_size: float = 0.35
    stroke: float = 0.003
    fill_share: float = 0.25
    fill_opacity: float = 0.5
    #: Drift, in frame widths per second.
    motion_speed: float = 0.03
    #: Relative size change per second.
    scale_speed: float = 0.1
    #: Chance a rectangle is hidden on a given frame.
    flicker: float = 0.05
    lifetime_seconds: tuple[float, float] = (0.6, 2.4)
    beat_sync: bool = True
    placement: str = "over"
    blend_mode: str = "screen"
    opacity: float = 0.6
    #: ``titles`` behind each rendered title, ``full`` the whole sequence, ``none`` off.
    coverage: str = "titles"
    pad_seconds: float = 0.5
    render_scale: float = 1.0


@dataclass(frozen=True)
class GraphicsProfile:
    name: str = "byjustinwu"
    enabled: bool = False
    #: ``prores4444`` everywhere ffmpeg is; ``hevc_alpha`` needs macOS VideoToolbox.
    codec: str = "prores4444"
    typography: Typography = field(default_factory=Typography)
    subtitles: Subtitles = field(default_factory=Subtitles)
    text_fx: TextFX = field(default_factory=TextFX)
    rect_layer: RectLayer = field(default_factory=RectLayer)
    source: str = PLACEHOLDER
    provenance: Mapping[str, str] = field(default_factory=dict)

    @property
    def placeholder_fields(self) -> list[str]:
        return [name for name in all_field_names() if self.provenance.get(name, PLACEHOLDER) == PLACEHOLDER]

    def subtitle_font(self) -> str:
        return self.subtitles.font or self.typography.text_font

    def title_font(self) -> str:
        return self.text_fx.font or self.typography.display_font

    def to_dict(self) -> dict:
        def section(item) -> dict:
            return {f.name: _jsonable(getattr(item, f.name)) for f in fields(item)}

        return {
            "name": self.name,
            "enabled": self.enabled,
            "codec": self.codec,
            "typography": section(self.typography),
            "subtitles": section(self.subtitles),
            "text_fx": section(self.text_fx),
            "rect_layer": section(self.rect_layer),
            "source": self.source,
            "placeholder_fields": self.placeholder_fields,
        }


SECTIONS: dict[str, type] = {
    "typography": Typography,
    "subtitles": Subtitles,
    "text_fx": TextFX,
    "rect_layer": RectLayer,
}
TOP_LEVEL = ("enabled", "codec")


def all_field_names() -> list[str]:
    names = list(TOP_LEVEL)
    for section, kind in SECTIONS.items():
        names.extend(f"{section}.{item.name}" for item in fields(kind))
    return names


def _face_split(value: str) -> dict[str, str]:
    for face in ("Semibold", "Regular", "Medium", "Bold", "Heavy", "Light", "Black"):
        if value.endswith(" " + face):
            return {"font": value[: -len(face) - 1].strip(), "face": face}
    return {"font": value}


def _cap_to_size(value: Any) -> float:
    return round(float(value) / 100.0 * 1080 / 0.7, 1)


def _casing(value: Any) -> str:
    text = str(value).lower()
    return {"lowercase": "lower", "uppercase": "upper"}.get(text, text)


def _size_range(value: Any) -> dict[str, float]:
    widths = list(value.get("w") or []) + list(value.get("h") or [])
    return {"min_size": float(min(widths)), "max_size": float(max(widths))}


def _mean(value: Any) -> float:
    items = [float(item) for item in value] if isinstance(value, list) else [float(value)]
    return sum(items) / len(items)


#: Measured parameter path -> (section, converter returning {field: value}).
PARAM_MAP: dict[str, tuple[str, Callable[[Any], dict]]] = {
    "typography.casing": ("typography", lambda v: {"casing": _casing(v)}),
    "typography.subtitles.enabled": ("subtitles", lambda v: {"enabled": bool(v)}),
    "typography.subtitles.font": ("subtitles", lambda v: _face_split(str(v))),
    "typography.subtitles.color": ("subtitles", lambda v: {"color": str(v)}),
    "typography.subtitles.position": ("subtitles", lambda v: {"position_y": float(v["baseline_y"])}),
    "typography.subtitles.cap_height_pct": ("subtitles", lambda v: {"size": _cap_to_size(v)}),
    "typography.subtitles.lines": ("subtitles", lambda v: {"max_lines": int(v)}),
    "typography.subtitles.max_chars_per_line": ("subtitles", lambda v: {"max_chars_per_line": int(v)}),
    "typography.chapter_title.color": ("text_fx", lambda v: {"color": str(v)}),
    "typography.chapter_title.cap_height_pct": ("text_fx", lambda v: {"size": _cap_to_size(v)}),
    "typography.chapter_title.hold_s": ("text_fx", lambda v: {"duration_seconds": float(v)}),
    "background_layer.palette": ("rect_layer", lambda v: {"palette": tuple(str(item) for item in v)}),
    "background_layer.size": ("rect_layer", _size_range),
    "background_layer.count.chapter_card": ("rect_layer", lambda v: {"density": _mean(v)}),
}


def load_graphics_profile(style: str | Path | GraphicsProfile | Mapping | None = None) -> GraphicsProfile:
    """The graphics profile for ``style``: a name, a JSON path, a mapping, or a profile.

    A name looks for ``styles/<name>/profile.json`` in the working directory
    and beside this package. When none exists, every value is a placeholder.
    """
    if isinstance(style, GraphicsProfile):
        return style
    if isinstance(style, Mapping):
        return from_mapping(style, source="mapping")
    name = "byjustinwu" if style is None else str(style)
    path = _profile_path(name)
    if path is None:
        if Path(name).suffix == ".json":
            raise ConductorError(f"no such style profile: {name}")
        stem = name[: -len("/profile")] if name.endswith("/profile") else name
        return GraphicsProfile(name=Path(stem).name)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ConductorError(f"style profile did not parse: {path}: {exc}") from exc
    if not isinstance(payload, Mapping):
        raise ConductorError(f"style profile is not a JSON object: {path}")
    return from_mapping(payload, source=str(path), name=str(payload.get("name") or path.parent.name))


def from_mapping(payload: Mapping, *, source: str, name: str | None = None) -> GraphicsProfile:
    provenance: dict[str, str] = {}
    base = GraphicsProfile(name=name or str(payload.get("name") or "byjustinwu"))
    sections = {key: getattr(base, key) for key in SECTIONS}
    top: dict[str, Any] = {}
    params = payload.get("params")
    if isinstance(params, Mapping):
        for path, (section, convert) in PARAM_MAP.items():
            found = _param(params, path)
            if found is None:
                continue
            try:
                updates = convert(found)
            except (KeyError, TypeError, ValueError):
                continue
            sections[section] = _update(sections[section], updates, f"{source}#params.{path}", provenance, section)
    graphics = payload.get("graphics", payload)
    if not isinstance(graphics, Mapping):
        raise ConductorError(f"{source}: 'graphics' must be an object")
    for key, value in graphics.items():
        if key in SECTIONS:
            if not isinstance(value, Mapping):
                raise ConductorError(f"{source}: '{key}' must be an object")
            sections[key] = _update(sections[key], value, source, provenance, key)
        elif key in TOP_LEVEL:
            top[key] = _coerce(getattr(base, key), value, f"{source}: {key}")
            provenance[key] = source
        elif graphics is not payload:
            raise ConductorError(f"{source}: unknown graphics key {key!r}")
    profile = replace(base, **sections, **top, source=source, provenance=provenance)
    _check(profile)
    return profile


def _update(section, values: Mapping, source: str, provenance: dict, prefix: str):
    known = {item.name: item for item in fields(section)}
    changes = {}
    for key, value in values.items():
        if key not in known:
            raise ConductorError(f"{source}: unknown {prefix} field {key!r}")
        changes[key] = _coerce(getattr(section, key), value, f"{source}: {prefix}.{key}")
        provenance[f"{prefix}.{key}"] = source
    return replace(section, **changes)


def _coerce(current: Any, value: Any, where: str) -> Any:
    try:
        if isinstance(current, bool):
            if not isinstance(value, bool):
                raise TypeError("expected true or false")
            return value
        if isinstance(current, int):
            return int(value)
        if isinstance(current, float):
            return float(value)
        if isinstance(current, tuple):
            items = list(value)
            if current and isinstance(current[0], float):
                return tuple(float(item) for item in items)
            return tuple(str(item) for item in items)
        if current is None or isinstance(current, str):
            return None if value is None else str(value)
    except (TypeError, ValueError) as exc:
        raise ConductorError(f"{where}: {exc}") from exc
    return value


def _check(profile: GraphicsProfile) -> None:
    subs, fx, rects = profile.subtitles, profile.text_fx, profile.rect_layer
    problems = []
    if subs.max_chars_per_line < 4 or subs.max_lines < 1:
        problems.append("subtitles need max_chars_per_line >= 4 and max_lines >= 1")
    if not 0 < subs.min_seconds <= subs.max_seconds:
        problems.append("subtitles need 0 < min_seconds <= max_seconds")
    if fx.default_treatment not in TREATMENTS or any(item not in TREATMENTS for item in fx.treatments):
        problems.append(f"text_fx treatments must be from {TREATMENTS}")
    if rects.placement not in {"over", "under"}:
        problems.append("rect_layer.placement is 'over' or 'under'")
    if rects.coverage not in {"titles", "full", "none"}:
        problems.append("rect_layer.coverage is 'titles', 'full', or 'none'")
    if rects.blend_mode not in BLEND_MODES:
        problems.append(f"rect_layer.blend_mode must be one of {sorted(BLEND_MODES)}")
    if not rects.palette:
        problems.append("rect_layer.palette needs at least one colour")
    if not 0 < rects.render_scale <= 1:
        problems.append("rect_layer.render_scale is in (0, 1]")
    if profile.codec not in {"prores4444", "hevc_alpha"}:
        problems.append("codec is 'prores4444' or 'hevc_alpha'")
    for colour in (profile.typography.color, profile.typography.shadow_color, *rects.palette):
        parse_colour(colour)
    if problems:
        raise ConductorError("graphics profile: " + "; ".join(problems))


def parse_colour(value: str) -> tuple[int, int, int]:
    text = str(value).strip().lstrip("#")
    if len(text) == 3:
        text = "".join(ch * 2 for ch in text)
    if len(text) != 6 or any(ch not in "0123456789abcdefABCDEF" for ch in text):
        raise ConductorError(f"graphics profile: {value!r} is not a #rrggbb colour")
    return int(text[0:2], 16), int(text[2:4], 16), int(text[4:6], 16)


def _param(params: Mapping, dotted: str) -> Any:
    node: Any = params
    for part in dotted.split("."):
        if not isinstance(node, Mapping) or part not in node:
            return None
        node = node[part]
    if isinstance(node, Mapping) and "value" in node:
        return node["value"]
    return None


def _profile_path(name: str) -> Path | None:
    text = name[: -len("/profile")] if name.endswith("/profile") else name
    direct = Path(text).expanduser()
    if direct.is_file():
        return direct
    if direct.is_dir() and (direct / "profile.json").is_file():
        return direct / "profile.json"
    for root in (Path.cwd(), Path(__file__).resolve().parents[2]):
        for candidate in (root / "styles" / direct.name / "profile.json", root / text / "profile.json"):
            if candidate.is_file():
                return candidate
    return None


def _jsonable(value: Any) -> Any:
    if isinstance(value, tuple):
        return list(value)
    return value
