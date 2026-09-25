"""Style profiles: the editorial taste of one creator, as data.

A profile is ``styles/<name>/profile.json`` (or ``profile.yaml`` when PyYAML
is installed). ``extends`` names a parent profile; dicts merge key by key,
lists and scalars replace. Every profile ultimately extends ``base``, which
lists every key the assembly engine reads, so a creator profile only states
what differs. The format is documented in ``docs/style-profile.md``.

``schema`` is ``jevid.style`` and ``schema_version`` is 1. A newer major
version is refused rather than half-read. Unknown keys are kept and reported
as warnings so a study can add fields before the engine reads them.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .errors import ConductorError

SCHEMA = "jevid.style"
SCHEMA_VERSION = 1
DEFAULT_STYLE = "byjustinwu"
STYLES_DIR = Path(__file__).resolve().parents[1] / "styles"
SECTIONS = ("intro", "title_card", "talking", "montage", "outro", "end_card")
CUT_SECTIONS = ("intro", "talking", "montage", "outro")
CARD_SECTIONS = ("title_card", "end_card")
CASES = ("upper", "lower", "sentence", "title", "as_is")
FADE_CURVES = ("linear", "easeIn", "easeOut", "easeInOut")
KEYFRAME_CURVES = ("linear", "ease", "easeIn", "easeOut")
BEAT_MODES = ("off", "prefer", "always")
TRANSITIONS = ("crossfade", "cut", "fade_through")
LAYOUTS = ("grid", "random", "mixed")
PROCEDURAL = ("interference", "noise", "stripes")
CORNERS = ("topLeft", "topRight", "botLeft", "botRight")
_TOP_LEVEL = frozenset(
    {
        "schema",
        "schema_version",
        "name",
        "extends",
        "version",
        "provisional",
        "description",
        "provenance",
        "assumptions",
        "format",
        "structure",
        "pacing",
        "cuts",
        "music",
        "typography",
        "text_treatments",
        "background",
        "metrics",
    }
)
_HEX = re.compile(r"^#(?:[0-9a-fA-F]{6}|[0-9a-fA-F]{8})$")
_MISSING = object()


@dataclass(frozen=True)
class StyleProfile:
    name: str
    version: str
    provisional: bool
    data: dict
    source: Path | None = None
    chain: tuple[str, ...] = ()
    warnings: tuple[str, ...] = field(default_factory=tuple)

    def get(self, dotted: str, default: Any = _MISSING) -> Any:
        node: Any = self.data
        for part in dotted.split("."):
            if isinstance(node, Mapping) and part in node:
                node = node[part]
            elif default is _MISSING:
                raise ConductorError(f"style {self.name!r} has no {dotted!r}")
            else:
                return default
        return node

    def pacing(self, section: str) -> dict:
        return dict(self.get(f"pacing.{section}", None) or self.get("pacing.talking"))

    def music_section(self, section: str) -> dict:
        base = {"bed_db": float(self.get("music.bed_db")), "duck": bool(self.get("music.duck.enabled"))}
        base.update(self.get(f"music.sections.{section}", None) or {})
        return base

    def background_on(self, section: str) -> bool:
        return bool(self.get("background.enabled")) and bool(
            self.get(f"background.sections.{section}", False)
        )

    def inset(self, section: str) -> float:
        return float(self.get(f"background.aroll_inset.{section}", 1.0) or 1.0)

    @property
    def digest(self) -> str:
        text = json.dumps(self.data, sort_keys=True, separators=(",", ":"))
        return hashlib.blake2b(text.encode("utf-8"), digest_size=8).hexdigest()

    def summary(self) -> dict:
        """The slice a decision batch sees. Numbers, not prose."""
        return {
            "name": self.name,
            "version": self.version,
            "provisional": self.provisional,
            "target_seconds": self.get("structure.target_seconds"),
            "order": list(self.get("structure.order")),
            "asl_seconds": {name: self.pacing(name)["asl_seconds"] for name in CUT_SECTIONS},
            "cut_on_beat": {name: self.pacing(name)["cut_on_beat"] for name in CUT_SECTIONS},
            "broll_cover": {name: self.pacing(name).get("broll_cover", 0.0) for name in CUT_SECTIONS},
            "subtitle_emphasis": dict(self.get("typography.subtitle.emphasis")),
            "title_treatments": list(self.get("typography.title.treatments")),
        }

    def to_state(self) -> dict:
        return {
            "name": self.name,
            "version": self.version,
            "provisional": self.provisional,
            "chain": list(self.chain),
            "source": str(self.source) if self.source else None,
            "digest": self.digest,
            "warnings": list(self.warnings),
        }


def load_style(ref: str | Path | None = None, *, search: list[Path] | None = None) -> StyleProfile:
    """Load a profile by name, directory, or file, resolving ``extends``."""
    ref = DEFAULT_STYLE if ref is None or str(ref).strip() == "" else ref
    dirs = search if search is not None else search_dirs()
    file = _locate(ref, dirs)
    chain: list[str] = []
    merged = _resolve(file, dirs, chain, seen=set())
    warnings = validate(merged)
    return StyleProfile(
        name=str(merged["name"]),
        version=str(merged["version"]),
        provisional=bool(merged.get("provisional", False)),
        data=merged,
        source=file,
        chain=tuple(chain),
        warnings=tuple(warnings),
    )


def style_from_dict(data: Mapping[str, Any], *, base: str | None = "base") -> StyleProfile:
    """Build a profile from an in-memory dict (tests, a room bot's overrides)."""
    raw = copy.deepcopy(dict(data))
    parent = raw.pop("extends", base)
    merged = raw
    chain = [str(raw.get("name", "inline"))]
    if parent:
        dirs = search_dirs()
        parent_file = _locate(parent, dirs)
        parent_chain: list[str] = []
        merged = deep_merge(_resolve(parent_file, dirs, parent_chain, seen=set()), raw)
        chain.extend(parent_chain)
    warnings = validate(merged)
    return StyleProfile(
        name=str(merged["name"]),
        version=str(merged["version"]),
        provisional=bool(merged.get("provisional", False)),
        data=merged,
        chain=tuple(chain),
        warnings=tuple(warnings),
    )


def search_dirs() -> list[Path]:
    dirs: list[Path] = []
    extra = os.environ.get("JEVID_STYLES_DIR", "").strip()
    if extra:
        dirs.extend(Path(part) for part in extra.split(os.pathsep) if part)
    dirs.append(STYLES_DIR)
    return dirs


def available_styles(dirs: list[Path] | None = None) -> list[str]:
    names: set[str] = set()
    for root in dirs if dirs is not None else search_dirs():
        if not root.is_dir():
            continue
        for child in root.iterdir():
            if child.is_dir() and _profile_file(child) is not None:
                names.add(child.name)
    return sorted(names)


def deep_merge(base: Mapping[str, Any], over: Mapping[str, Any]) -> dict:
    out = copy.deepcopy(dict(base))
    for key, value in over.items():
        if isinstance(value, Mapping) and isinstance(out.get(key), Mapping):
            out[key] = deep_merge(out[key], value)
        else:
            out[key] = copy.deepcopy(value)
    return out


def _locate(ref: str | Path, dirs: list[Path]) -> Path:
    path = Path(ref)
    if path.is_file():
        return path.resolve()
    if path.is_dir():
        found = _profile_file(path)
        if found is None:
            raise ConductorError(f"no profile.json or profile.yaml in {path}")
        return found.resolve()
    name = str(ref)
    if "/" in name or "\\" in name or name.endswith((".json", ".yaml", ".yml")):
        raise ConductorError(f"no such style profile: {ref}")
    for root in dirs:
        found = _profile_file(root / name)
        if found is not None:
            return found.resolve()
    known = ", ".join(available_styles(dirs)) or "(none)"
    raise ConductorError(f"no style named {name!r}. Known: {known}")


def _profile_file(folder: Path) -> Path | None:
    for name in ("profile.json", "profile.yaml", "profile.yml"):
        candidate = folder / name
        if candidate.is_file():
            return candidate
    return None


def _resolve(file: Path, dirs: list[Path], chain: list[str], seen: set[Path]) -> dict:
    if file in seen:
        raise ConductorError(f"style profiles extend each other in a loop at {file}")
    seen.add(file)
    raw = _read(file)
    chain.append(str(raw.get("name") or file.parent.name))
    parent = raw.pop("extends", None)
    if not parent:
        return raw
    parent_file = _locate(parent, [file.parent.parent, *dirs])
    return deep_merge(_resolve(parent_file, dirs, chain, seen), raw)


def _read(file: Path) -> dict:
    text = file.read_text(encoding="utf-8")
    if file.suffix in {".yaml", ".yml"}:
        try:
            import yaml
        except ImportError as exc:
            raise ConductorError(
                f"{file} is YAML but PyYAML is not installed. Use profile.json or pip install pyyaml."
            ) from exc
        try:
            data = yaml.safe_load(text)
        except yaml.YAMLError as exc:
            raise ConductorError(f"style profile is not YAML: {file}: {exc}") from exc
    else:
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ConductorError(f"style profile is not JSON: {file}: {exc}") from exc
    if not isinstance(data, dict):
        raise ConductorError(f"style profile must be an object: {file}")
    return data


def validate(data: Mapping[str, Any]) -> list[str]:
    """Raise on a value the engine cannot use. Return warnings for the rest."""
    errors: list[str] = []
    warnings: list[str] = []
    v = _Validator(data, errors)
    if data.get("schema") != SCHEMA:
        errors.append(f"schema must be {SCHEMA!r}, got {data.get('schema')!r}")
    version = data.get("schema_version")
    if not isinstance(version, int) or isinstance(version, bool):
        errors.append("schema_version must be an integer")
    elif version > SCHEMA_VERSION:
        errors.append(
            f"schema_version {version} is newer than this engine ({SCHEMA_VERSION}); update jevid"
        )
    v.string("name")
    v.string("version")
    v.boolean("provisional")
    for key in sorted(set(data) - _TOP_LEVEL):
        warnings.append(f"unknown top-level key {key!r} kept but not read")

    v.number("format.width", low=16, integer=True)
    v.number("format.height", low=16, integer=True)
    v.number("format.frame_rate", low=1, high=240)
    v.boolean("format.follow_footage")

    v.number("structure.target_seconds", low=1)
    v.number("structure.tolerance_seconds", low=0)
    order = v.get("structure.order")
    if not isinstance(order, list) or not order:
        errors.append("structure.order must be a non-empty list")
    else:
        for name in order:
            if name not in SECTIONS:
                errors.append(f"structure.order has unknown section {name!r}; known: {', '.join(SECTIONS)}")
        if "talking" not in order:
            errors.append("structure.order needs at least one 'talking' section")
    for name in CUT_SECTIONS:
        v.number(f"structure.sections.{name}.share", low=0, high=1)
        v.number(f"structure.sections.{name}.min_seconds", low=0)
        v.number(f"structure.sections.{name}.max_seconds", low=0, nullable=True)
    for name in CARD_SECTIONS:
        v.number(f"structure.{name}.seconds", low=0)
        v.number(f"structure.{name}.bars", low=0, integer=True)
    v.string("structure.end_card.text")
    for key in (
        "broll_chunk_seconds",
        "speech_merge_gap_seconds",
        "speech_handle_seconds",
        "min_speech_seconds",
    ):
        v.number(f"structure.{key}", low=0)
    v.number("structure.keep_threshold", low=0, high=1)
    v.number("structure.intro_flash_share", low=0, high=1)

    v.number("pacing.tolerance", low=0, high=1)
    for name in CUT_SECTIONS:
        v.number(f"pacing.{name}.asl_seconds", low=0.05)
        v.choice(f"pacing.{name}.cut_on_beat", BEAT_MODES)
        v.number(f"pacing.{name}.broll_cover", low=0, high=1)
        shot = v.get(f"pacing.{name}.shot_length")
        if isinstance(shot, Mapping):
            for key in ("p10", "median", "p90"):
                v.number(f"pacing.{name}.shot_length.{key}", low=0.04)
            if all(isinstance(shot.get(k), (int, float)) for k in ("p10", "median", "p90")):
                if not shot["p10"] <= shot["median"] <= shot["p90"]:
                    errors.append(f"pacing.{name}.shot_length needs p10 <= median <= p90")
        else:
            errors.append(f"pacing.{name}.shot_length must be an object")

    v.number("cuts.beat_snap_tolerance_seconds", low=0)
    v.number("cuts.min_shot_seconds", low=0.04)
    v.number("cuts.j_cut.probability", low=0, high=1)
    v.number("cuts.j_cut.lead_seconds", low=0)
    v.number("cuts.l_cut.probability", low=0, high=1)
    v.number("cuts.l_cut.tail_seconds", low=0)
    v.boolean("cuts.punch_in.enabled")
    v.number("cuts.punch_in.scale", low=1, high=3)
    v.number("cuts.punch_in.every", low=1, integer=True)
    v.number("cuts.cutaway.min_seconds", low=0.1)
    v.number("cuts.cutaway.max_seconds", low=0.1)
    v.number("cuts.broll_nat_sound_db", low=-96, high=12)

    v.string("music.role")
    v.number("music.bed_db", low=-96, high=12)
    v.number("music.floor_db", low=-96, high=0)
    for name in ("fade_in", "fade_out"):
        v.number(f"music.{name}.seconds", low=0)
        v.choice(f"music.{name}.curve", FADE_CURVES)
    v.boolean("music.duck.enabled")
    v.number("music.duck.depth_db", low=-96, high=0)
    v.number("music.duck.attack_seconds", low=0)
    v.number("music.duck.release_seconds", low=0)
    v.number("music.duck.merge_gap_seconds", low=0)
    v.choice("music.duck.curve", KEYFRAME_CURVES)
    v.choice("music.song_change.transition", TRANSITIONS)
    v.number("music.song_change.crossfade_seconds", low=0)
    sections = v.get("music.sections")
    if isinstance(sections, Mapping):
        for name, spec in sections.items():
            if name not in SECTIONS:
                errors.append(f"music.sections has unknown section {name!r}")
            elif isinstance(spec, Mapping):
                v.number(f"music.sections.{name}.bed_db", low=-96, high=12, optional=True)
                v.boolean(f"music.sections.{name}.duck", optional=True)
    v.boolean("music.beats.detect")
    v.number("music.beats.min_bpm", low=20, high=400)
    v.number("music.beats.max_bpm", low=20, high=400)
    v.number("music.beats.fallback_bpm", low=20, high=400, nullable=True)
    v.number("music.beats.beats_per_bar", low=1, high=16, integer=True)
    if isinstance(v.get("music.beats.min_bpm"), (int, float)) and isinstance(
        v.get("music.beats.max_bpm"), (int, float)
    ):
        if v.get("music.beats.min_bpm") >= v.get("music.beats.max_bpm"):
            errors.append("music.beats.min_bpm must be below max_bpm")
    v.boolean("music.start_on_downbeat")

    treatments = v.get("text_treatments")
    if not isinstance(treatments, Mapping) or "none" not in treatments:
        errors.append("text_treatments must be an object that includes 'none'")
        treatments = {}
    for name, spec in treatments.items():
        _treatment(name, spec, errors)

    v.string("typography.family")
    for which in ("title", "subtitle"):
        prefix = f"typography.{which}"
        v.string(f"{prefix}.font")
        v.string(f"{prefix}.face")
        v.number(f"{prefix}.size", low=0.005, high=1)
        v.vector(f"{prefix}.position", 2, low=-1, high=1)
        v.choice(f"{prefix}.case", CASES)
        v.number(f"{prefix}.tracking", low=-1, high=2)
        v.color(f"{prefix}.color")
        v.choice(f"{prefix}.alignment", ("left", "center", "right"))
        v.number(f"{prefix}.lane", low=1, high=20, integer=True)
        stroke = v.get(f"{prefix}.stroke", None)
        if stroke is not None:
            v.color(f"{prefix}.stroke.color")
            v.number(f"{prefix}.stroke.width", low=0)
        shadow = v.get(f"{prefix}.shadow", None)
        if shadow is not None:
            v.color(f"{prefix}.shadow.color")
            v.number(f"{prefix}.shadow.distance", low=0)
            v.number(f"{prefix}.shadow.angle", low=-360, high=360)
            v.number(f"{prefix}.shadow.blur", low=0)
    for name in v.get("typography.title.treatments", []) or []:
        if name not in treatments:
            errors.append(f"typography.title.treatments names unknown treatment {name!r}")
    v.boolean("typography.subtitle.enabled")
    v.number("typography.subtitle.max_chars_per_line", low=4, integer=True)
    v.number("typography.subtitle.max_lines", low=1, high=4, integer=True)
    v.number("typography.subtitle.min_seconds", low=0.1)
    v.number("typography.subtitle.max_seconds", low=0.2)
    v.number("typography.subtitle.emphasis.rate", low=0, high=1)
    for name in v.get("typography.subtitle.emphasis.treatments", []) or []:
        if name not in treatments:
            errors.append(f"typography.subtitle.emphasis names unknown treatment {name!r}")

    v.boolean("background.enabled")
    v.choice("background.placement", ("under", "over"))
    v.number("background.lane", low=-20, high=20, integer=True)
    if v.get("background.lane", 0) == 0:
        errors.append("background.lane must not be 0 (that is the primary storyline)")
    bg_sections = v.get("background.sections")
    if isinstance(bg_sections, Mapping):
        for name in bg_sections:
            if name not in SECTIONS:
                errors.append(f"background.sections has unknown section {name!r}")
    insets = v.get("background.aroll_inset")
    if isinstance(insets, Mapping):
        for name in insets:
            v.number(f"background.aroll_inset.{name}", low=0.1, high=1.0)
    v.number("background.change.max_seconds", low=0.5)
    v.number("background.render_scale", low=0.05, high=1.0)
    v.choice("background.layout.mode", LAYOUTS)
    v.number("background.layout.grid.columns", low=1, high=64, integer=True)
    v.number("background.layout.grid.rows", low=1, high=64, integer=True)
    v.number("background.layout.grid.merge_probability", low=0, high=1)
    v.number("background.layout.grid.fill", low=0, high=1)
    v.span("background.layout.random.count", low=0, integer=True)
    ratios = v.get("background.layout.size_ratios")
    if not isinstance(ratios, list) or not ratios or not all(
        isinstance(r, list) and len(r) == 2 and all(isinstance(x, (int, float)) and x > 0 for x in r)
        for r in ratios
    ):
        errors.append("background.layout.size_ratios must be a list of [w, h] pairs")
    v.span("background.layout.size_range", low=0.005, high=1)
    v.number("background.layout.gutter", low=0, high=0.5)
    v.number("background.layout.margin", low=0, high=0.5)
    v.span("background.contortion.stretch", low=0.05, high=10)
    v.span("background.contortion.shear_degrees", low=-60, high=60)
    v.span("background.contortion.slices", low=0, high=64, integer=True)
    v.number("background.contortion.slice_shift", low=0, high=1)
    v.number("background.contortion.smear", low=0, high=1)
    v.number("background.contortion.mirror", low=0, high=1)
    v.number("background.motion.drift", low=0, high=1)
    v.number("background.motion.pulse", low=0, high=1)
    v.boolean("background.motion.pulse_on_beat")
    v.number("background.motion.floating", low=0, high=12, integer=True)
    v.span("background.motion.floating_size", low=0.01, high=1)
    v.span("background.motion.rotation_degrees", low=-180, high=180)
    v.number("background.opacity", low=0, high=1)
    v.color("background.base_color")
    palette = v.get("background.palette")
    if not isinstance(palette, list) or not palette:
        errors.append("background.palette must be a non-empty list of colours")
    else:
        for index, colour in enumerate(palette):
            if not isinstance(colour, str) or not _HEX.match(colour):
                errors.append(f"background.palette[{index}] must be #RRGGBB or #RRGGBBAA")
    v.string("background.art.folder", nullable=True)
    v.choice("background.art.procedural", PROCEDURAL)
    v.number("background.art.seed", integer=True)

    for key in (
        "subtitle_coverage_min",
        "on_beat_min",
        "background_coverage_min",
        "font_compliance_min",
    ):
        v.number(f"metrics.{key}", low=0, high=1)
    v.number("metrics.fade_tolerance_seconds", low=0)
    v.number("metrics.duck_tolerance_db", low=0)

    if errors:
        name = data.get("name", "?")
        raise ConductorError(f"style profile {name!r} is invalid: " + "; ".join(errors))
    return warnings


def _treatment(name: str, spec: Any, errors: list[str]) -> None:
    where = f"text_treatments.{name}"
    if not isinstance(spec, Mapping):
        errors.append(f"{where} must be an object")
        return
    for key in ("scale", "position"):
        if key in spec:
            _keyframes(f"{where}.{key}", spec[key], 2, errors)
    if "rotation" in spec:
        _keyframes(f"{where}.rotation", spec["rotation"], 1, errors)
    if "tracking" in spec and not _is_number(spec["tracking"]):
        errors.append(f"{where}.tracking must be a number (em)")
    if "corners" in spec:
        corners = spec["corners"]
        if not isinstance(corners, Mapping):
            errors.append(f"{where}.corners must be an object")
        else:
            for key, value in corners.items():
                if key not in CORNERS:
                    errors.append(f"{where}.corners has unknown corner {key!r}")
                elif not (isinstance(value, list) and len(value) == 2 and all(_is_number(x) for x in value)):
                    errors.append(f"{where}.corners.{key} must be [dx, dy] as frame fractions")


def _keyframes(where: str, frames: Any, size: int, errors: list[str]) -> None:
    if not isinstance(frames, list) or not frames:
        errors.append(f"{where} must be a list of [seconds, value] keyframes")
        return
    for frame in frames:
        ok = isinstance(frame, list) and len(frame) == 2 and _is_number(frame[0])
        if ok and size == 1:
            ok = _is_number(frame[1])
        elif ok:
            ok = isinstance(frame[1], list) and len(frame[1]) == size and all(_is_number(x) for x in frame[1])
        if not ok:
            errors.append(f"{where} keyframe {frame!r} must be [seconds, value]")
            return


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


class _Validator:
    def __init__(self, data: Mapping[str, Any], errors: list[str]):
        self.data = data
        self.errors = errors

    def get(self, dotted: str, default: Any = _MISSING) -> Any:
        node: Any = self.data
        for part in dotted.split("."):
            if isinstance(node, Mapping) and part in node:
                node = node[part]
            else:
                return None if default is _MISSING else default
        return node

    def number(
        self,
        dotted: str,
        *,
        low: float | None = None,
        high: float | None = None,
        integer: bool = False,
        nullable: bool = False,
        optional: bool = False,
    ) -> Any:
        value = self.get(dotted, _MISSING)
        if value is _MISSING:
            if not optional:
                self.errors.append(f"{dotted} is required")
            return None
        if value is None:
            if not nullable and not optional:
                self.errors.append(f"{dotted} must be a number")
            return None
        if not _is_number(value) or (integer and not isinstance(value, int)):
            self.errors.append(f"{dotted} must be {'an integer' if integer else 'a number'}")
            return None
        if low is not None and value < low:
            self.errors.append(f"{dotted} must be >= {low}")
        if high is not None and value > high:
            self.errors.append(f"{dotted} must be <= {high}")
        return value

    def boolean(self, dotted: str, *, optional: bool = False) -> None:
        value = self.get(dotted, _MISSING)
        if value is _MISSING:
            if not optional:
                self.errors.append(f"{dotted} is required")
            return
        if not isinstance(value, bool):
            self.errors.append(f"{dotted} must be true or false")

    def string(self, dotted: str, *, nullable: bool = False) -> None:
        value = self.get(dotted, _MISSING)
        if value is None and nullable:
            return
        if not isinstance(value, str) or not value.strip():
            self.errors.append(f"{dotted} must be a non-empty string")

    def choice(self, dotted: str, options: tuple[str, ...]) -> None:
        value = self.get(dotted, _MISSING)
        if value not in options:
            self.errors.append(f"{dotted} must be one of {', '.join(options)}, got {value!r}")

    def color(self, dotted: str) -> None:
        value = self.get(dotted, _MISSING)
        if not isinstance(value, str) or not _HEX.match(value):
            self.errors.append(f"{dotted} must be #RRGGBB or #RRGGBBAA")

    def vector(self, dotted: str, size: int, *, low: float, high: float) -> None:
        value = self.get(dotted, _MISSING)
        if not (isinstance(value, list) and len(value) == size and all(_is_number(x) for x in value)):
            self.errors.append(f"{dotted} must be a list of {size} numbers")
            return
        if any(x < low or x > high for x in value):
            self.errors.append(f"{dotted} values must be within [{low}, {high}]")

    def span(self, dotted: str, *, low: float, high: float | None = None, integer: bool = False) -> None:
        value = self.get(dotted, _MISSING)
        if not (isinstance(value, list) and len(value) == 2 and all(_is_number(x) for x in value)):
            self.errors.append(f"{dotted} must be [low, high]")
            return
        if integer and not all(isinstance(x, int) for x in value):
            self.errors.append(f"{dotted} must be integers")
        if value[0] > value[1]:
            self.errors.append(f"{dotted} must have low <= high")
        if value[0] < low or (high is not None and value[1] > high):
            bound = f"[{low}, {high}]" if high is not None else f">= {low}"
            self.errors.append(f"{dotted} must be within {bound}")


def hex_rgba(colour: str) -> tuple[float, float, float, float]:
    """``#RRGGBB[AA]`` → floats in [0, 1]."""
    text = colour.lstrip("#")
    parts = [int(text[i : i + 2], 16) / 255.0 for i in range(0, len(text), 2)]
    if len(parts) == 3:
        parts.append(1.0)
    return parts[0], parts[1], parts[2], parts[3]
