"""Headless frame rendering: distorted title text and the rectangle layer.

Frames are straight-alpha RGBA ``uint8`` arrays, one per sequence frame.
Everything is a pure function of its inputs and a seed, so the same profile
and seed give the same pixels on every machine that has the same font file.

Text needs Pillow. The rectangle layer is numpy only. Fonts: SF Pro when the
machine has it (Justin's Mac), otherwise the profile's fallback list, then
Pillow's built-in face. The title XML names SF Pro either way.
"""

from __future__ import annotations

import hashlib
import json
import math
import shutil
import subprocess
from collections.abc import Iterator, Sequence
from dataclasses import asdict, dataclass
from functools import lru_cache
from pathlib import Path

import numpy as np

from .profile import GraphicsProfile, RectLayer, TextFX, parse_colour

MAC_FONT_DIRS = (
    Path("/Library/Fonts"),
    Path("/System/Library/Fonts"),
    Path("/System/Library/Fonts/Supplemental"),
    Path("~/Library/Fonts").expanduser(),
)
LINUX_FONT_DIRS = (Path("/usr/share/fonts"), Path("/usr/local/share/fonts"), Path("~/.fonts").expanduser())


class RenderUnavailable(Exception):
    """A dependency for rendering is missing. The stage records the note and moves on."""


@dataclass(frozen=True)
class FontChoice:
    intended: str
    face: str
    path: str | None
    family: str
    fallback: bool

    def note(self) -> str | None:
        if not self.fallback:
            return None
        used = f"{self.family} {self.face}" if self.path else "Pillow's built-in face"
        return (
            f"{self.intended} is not installed here, so rendered titles use {used}. "
            f"The title XML still names {self.intended}; Final Cut uses it on the Mac."
        )


def seed_int(*parts: object) -> int:
    blob = json.dumps(parts, sort_keys=True, default=str).encode("utf-8")
    return int.from_bytes(hashlib.blake2b(blob, digest_size=8).digest(), "big")


# ----------------------------------------------------------------------
# fonts
# ----------------------------------------------------------------------


@lru_cache(maxsize=1)
def font_index() -> dict[str, dict[str, str]]:
    """family -> style -> file, from fc-list when present, plus a scan of the usual folders."""
    found: dict[str, dict[str, str]] = {}
    tool = shutil.which("fc-list")
    if tool:
        try:
            out = subprocess.run(
                [tool, "--format", "%{family}\t%{style}\t%{file}\n"],
                capture_output=True, text=True, timeout=20, check=False,
            ).stdout
        except (OSError, subprocess.SubprocessError):
            out = ""
        for line in out.splitlines():
            parts = line.split("\t")
            if len(parts) != 3:
                continue
            families, styles, file = parts
            style = styles.split(",")[0].strip() or "Regular"
            for family in families.split(","):
                found.setdefault(family.strip(), {}).setdefault(style, file)
    for folder in (*MAC_FONT_DIRS, *LINUX_FONT_DIRS):
        if not folder.is_dir():
            continue
        for file in sorted(folder.rglob("*")):
            if file.suffix.lower() not in {".ttf", ".otf", ".ttc"}:
                continue
            family, _, style = file.stem.replace("_", "-").rpartition("-")
            if not family:
                family, style = file.stem, "Regular"
            name = family.replace("-", " ")
            found.setdefault(name, {}).setdefault(style or "Regular", str(file))
    return found


#: macOS ships SF as a variable system font; the SF Pro family files are a separate install.
SYSTEM_ALIASES = {"SF Pro Display": ("SFNS", "SFNSDisplay"), "SF Pro Text": ("SFNS", "SFNSText"), "SF Pro": ("SFNS",)}


def resolve_font(intended: str, face: str, fallbacks: Sequence[str]) -> FontChoice:
    index = font_index()
    for position, family in enumerate([intended, *fallbacks]):
        styles = index.get(family)
        if not styles and position == 0:
            alias = next((name for name in SYSTEM_ALIASES.get(intended, ()) if index.get(name)), None)
            if alias:
                path = next(iter(sorted(index[alias].values())))
                return FontChoice(intended, face, path, intended, False)
        if not styles:
            continue
        path = styles.get(face) or styles.get(face.replace("Semibold", "SemiBold")) or styles.get("Regular")
        path = path or next(iter(sorted(styles.values())))
        return FontChoice(intended, face, path, family, position > 0)
    return FontChoice(intended, face, None, "", True)


def _pil():
    try:
        from PIL import Image, ImageDraw, ImageFilter, ImageFont
    except ImportError as exc:
        raise RenderUnavailable("Pillow is not installed (pip install Pillow), so no title was rendered") from exc
    return Image, ImageDraw, ImageFilter, ImageFont


def load_font(choice: FontChoice, size: int):
    _image, _draw, _filter, font_module = _pil()
    if choice.path:
        try:
            font = font_module.truetype(choice.path, size)
        except OSError:
            font = None
        if font is not None:
            try:
                font.set_variation_by_name(choice.face)
            except (OSError, ValueError, AttributeError):
                pass
            return font
    try:
        return font_module.load_default(size)
    except TypeError:
        return font_module.load_default()


# ----------------------------------------------------------------------
# text treatments
# ----------------------------------------------------------------------


@dataclass(frozen=True)
class TitleSpec:
    text: str
    treatment: str
    width: int
    height: int
    fps: float
    frames: int
    seed: int


def _ease(p: float) -> float:
    p = min(1.0, max(0.0, p))
    return 1 - (1 - p) ** 3


def text_layer(text: str, profile: GraphicsProfile, height: int, font: FontChoice) -> tuple[np.ndarray, int]:
    """The static title (with its shadow) on a padded transparent canvas, and the pad."""
    image_mod, draw_mod, filter_mod, _fonts = _pil()
    fx, typo = profile.text_fx, profile.typography
    size = max(8, int(round(fx.size * height / 1080)))
    face = load_font(font, size)
    probe = draw_mod.Draw(image_mod.new("L", (1, 1)))
    left, top, right, bottom = probe.multiline_textbbox((0, 0), text, font=face, align="center")
    pad = int(size * 0.6)
    width, tall = right - left + 2 * pad, bottom - top + 2 * pad
    colour = parse_colour(fx.color or typo.color)
    layer = image_mod.new("RGBA", (width, tall), (0, 0, 0, 0))
    if typo.shadow_opacity > 0:
        shadow = image_mod.new("RGBA", (width, tall), (0, 0, 0, 0))
        angle = math.radians(typo.shadow_angle)
        dx = typo.shadow_offset * math.cos(angle) * height / 1080
        dy = -typo.shadow_offset * math.sin(angle) * height / 1080
        draw_mod.Draw(shadow).multiline_text(
            (pad - left + dx, pad - top + dy), text, font=face, align="center",
            fill=(*parse_colour(typo.shadow_color), int(255 * typo.shadow_opacity)),
        )
        if typo.shadow_blur > 0:
            shadow = shadow.filter(filter_mod.GaussianBlur(typo.shadow_blur * height / 1080))
        layer = image_mod.alpha_composite(layer, shadow)
    draw_mod.Draw(layer).multiline_text((pad - left, pad - top), text, font=face, align="center", fill=(*colour, 255))
    return np.asarray(layer, dtype=np.uint8).copy(), pad


def title_frames(spec: TitleSpec, profile: GraphicsProfile, font: FontChoice) -> Iterator[np.ndarray]:
    """One full-frame RGBA array per frame of the title, with its treatment applied."""
    base, _pad = text_layer(spec.text, profile, spec.height, font)
    fx = profile.text_fx
    fade = max(1, int(round(fx.fade_out_seconds * spec.fps)))
    for index in range(spec.frames):
        t = index / spec.fps
        layer = apply_treatment(base, spec.treatment, t, index, fx, spec.height, spec.seed)
        remaining = spec.frames - index
        if remaining <= fade:
            layer = _scale_alpha(layer, remaining / (fade + 1))
        yield _place(layer, spec.width, spec.height, fx.position_y)


def apply_treatment(base: np.ndarray, treatment: str, t: float, index: int, fx: TextFX, height: int, seed: int) -> np.ndarray:
    if treatment == "blur_in":
        return _blur_in(base, _ease(t / max(fx.blur_seconds, 1e-6)), fx.blur_radius * height)
    if treatment == "glitch_slice":
        return _glitch(base, 1 - _ease(t / max(fx.glitch_seconds, 1e-6)), fx, index, seed)
    if treatment == "rgb_split":
        amount = (1 - _ease(t / max(fx.rgb_seconds, 1e-6))) * fx.rgb_offset * base.shape[1]
        return _rgb_split(base, amount)
    if treatment == "wave":
        amount = (1 - _ease(t / max(fx.wave_seconds, 1e-6))) * fx.wave_amplitude * height
        return _wave(base, amount, fx.wave_wavelength * height, t)
    if treatment == "scale_warp":
        p = _ease(t / max(fx.scale_seconds, 1e-6))
        sx = fx.scale_from[0] + (fx.scale_to[0] - fx.scale_from[0]) * p
        sy = fx.scale_from[1] + (fx.scale_to[1] - fx.scale_from[1]) * p
        return _scale(base, sx, sy)
    return base


def _blur_in(base: np.ndarray, progress: float, radius: float) -> np.ndarray:
    image_mod, _draw, filter_mod, _fonts = _pil()
    layer = base
    blur = radius * (1 - progress)
    if blur >= 0.5:
        premult = image_mod.fromarray(base, "RGBA").convert("RGBa")
        layer = np.asarray(premult.filter(filter_mod.GaussianBlur(blur)).convert("RGBA"), dtype=np.uint8)
    return _scale_alpha(layer, progress)


def _glitch(base: np.ndarray, intensity: float, fx: TextFX, index: int, seed: int) -> np.ndarray:
    if intensity <= 0.01:
        return base
    rng = np.random.default_rng([seed, index, 1])
    height, width = base.shape[:2]
    out = np.zeros_like(base)
    cuts = np.sort(rng.choice(np.arange(1, height), size=min(max(1, fx.glitch_slices - 1), height - 1), replace=False))
    edges = [0, *cuts.tolist(), height]
    for top, bottom in zip(edges, edges[1:]):
        shift = 0
        if rng.random() < 0.6:
            shift = int(round(rng.uniform(-1, 1) * fx.glitch_max_offset * width * intensity))
        out[top:bottom] = _shift_x(base[top:bottom], shift)
    return out


def _rgb_split(base: np.ndarray, amount: float) -> np.ndarray:
    shift = int(round(amount))
    if shift == 0:
        return base
    alpha = base[..., 3:4].astype(np.float32) / 255
    premult = base[..., :3].astype(np.float32) * alpha
    left, right = _shift_x(np.dstack([premult, alpha * 255]), -shift), _shift_x(np.dstack([premult, alpha * 255]), shift)
    out_alpha = np.maximum.reduce([alpha[..., 0] * 255, left[..., 3], right[..., 3]])
    rgb = np.dstack([left[..., 0], premult[..., 1], right[..., 2]])
    safe = np.where(out_alpha > 0, out_alpha / 255, 1)[..., None]
    rgb = np.clip(rgb / safe, 0, 255)
    return np.dstack([rgb, out_alpha]).astype(np.uint8)


def _wave(base: np.ndarray, amplitude: float, wavelength: float, t: float) -> np.ndarray:
    if amplitude < 0.5:
        return base
    height, width = base.shape[:2]
    rows = np.arange(height)
    shifts = np.round(amplitude * np.sin(2 * math.pi * rows / max(wavelength, 1.0) + 2 * math.pi * t)).astype(int)
    columns = (np.arange(width)[None, :] - shifts[:, None])
    valid = (columns >= 0) & (columns < width)
    out = base[rows[:, None], np.clip(columns, 0, width - 1)]
    out[~valid] = 0
    return out


def _scale(base: np.ndarray, sx: float, sy: float) -> np.ndarray:
    if abs(sx - 1) < 1e-3 and abs(sy - 1) < 1e-3:
        return base
    image_mod, _draw, _filter, _fonts = _pil()
    height, width = base.shape[:2]
    new_w, new_h = max(1, int(round(width * sx))), max(1, int(round(height * sy)))
    premult = image_mod.fromarray(base, "RGBA").convert("RGBa").resize((new_w, new_h), image_mod.BICUBIC)
    return np.asarray(premult.convert("RGBA"), dtype=np.uint8)


def _shift_x(block: np.ndarray, shift: int) -> np.ndarray:
    if shift == 0:
        return block.copy()
    out = np.zeros_like(block)
    if shift > 0:
        out[:, shift:] = block[:, :-shift]
    else:
        out[:, :shift] = block[:, -shift:]
    return out


def _scale_alpha(layer: np.ndarray, amount: float) -> np.ndarray:
    if amount >= 1:
        return layer
    out = layer.copy()
    out[..., 3] = (out[..., 3].astype(np.float32) * max(0.0, amount)).astype(np.uint8)
    return out


def _place(layer: np.ndarray, width: int, height: int, position_y: float) -> np.ndarray:
    frame = np.zeros((height, width, 4), dtype=np.uint8)
    tall, wide = layer.shape[:2]
    top = int(round(position_y * height - tall / 2))
    left = int(round((width - wide) / 2))
    y0, x0 = max(0, top), max(0, left)
    y1, x1 = min(height, top + tall), min(width, left + wide)
    if y1 > y0 and x1 > x0:
        frame[y0:y1, x0:x1] = layer[y0 - top:y1 - top, x0 - left:x1 - left]
    return frame


# ----------------------------------------------------------------------
# rectangle layer
# ----------------------------------------------------------------------


@dataclass(frozen=True)
class Rect:
    born: float
    dies: float
    x: float
    y: float
    w: float
    h: float
    vx: float
    vy: float
    grow: float
    colour: int
    filled: bool


def rect_seed(params: RectLayer, key: object) -> int:
    return seed_int("rect", params.seed, asdict(params), key)


def rect_schedule(params: RectLayer, duration: float, seed: int, beats: Sequence[float] | None = None) -> list[Rect]:
    """Every rectangle in a window, from ``seed`` alone. ``beats`` are seconds into the window."""
    rng = np.random.default_rng(seed)
    low, high = params.lifetime_seconds
    mean_life = max((low + high) / 2, 1e-3)

    def make(born: float, life: float) -> Rect:
        return Rect(
            born=born,
            dies=born + life,
            x=float(rng.uniform(0, 1)),
            y=float(rng.uniform(0, 1)),
            w=float(rng.uniform(params.min_size, params.max_size)),
            h=float(rng.uniform(params.min_size, params.max_size)),
            vx=float(rng.uniform(-1, 1) * params.motion_speed),
            vy=float(rng.uniform(-1, 1) * params.motion_speed),
            grow=float(rng.uniform(-1, 1) * params.scale_speed),
            colour=int(rng.integers(0, len(params.palette))),
            filled=bool(rng.random() < params.fill_share),
        )

    rects: list[Rect] = []
    for _ in range(int(round(params.density))):
        life = float(rng.uniform(low, high))
        rects.append(make(-float(rng.uniform(0, life)), life))
    points = sorted(beat for beat in (beats or []) if 0 <= beat < duration) if params.beat_sync else []
    if points:
        per_beat = max(1, int(math.ceil(params.density / 2)))
        for position, beat in enumerate(points):
            until = points[position + 1] if position + 1 < len(points) else beat + mean_life
            for _ in range(per_beat):
                rects.append(make(beat, max(until - beat, 1e-3) * float(rng.uniform(1.0, 2.0))))
    else:
        rate = max(params.density, 0.0) / mean_life
        t = 0.0
        while rate > 0:
            t += float(rng.exponential(1 / rate))
            if t >= duration:
                break
            rects.append(make(t, float(rng.uniform(low, high))))
    return rects


def rect_frame(
    rects: Sequence[Rect],
    params: RectLayer,
    t: float,
    index: int,
    width: int,
    height: int,
    seed: int,
    *,
    duration: float,
    beats: Sequence[float] | None = None,
) -> np.ndarray:
    canvas = np.zeros((height, width, 4), dtype=np.float32)
    palette = [np.array(parse_colour(item), dtype=np.float32) / 255 for item in params.palette]
    hidden = np.random.default_rng([seed, index, 2]).random(max(len(rects), 1))
    stroke = max(1, int(round(params.stroke * height)))
    envelope = min(1.0, t / 0.2, max(0.0, duration - t) / 0.2) if duration > 0 else 1.0
    pulse = 1.0
    if beats and params.beat_sync and any(0 <= t - beat < 2.5 / 24 for beat in beats):
        pulse = 1.4
    for position, rect in enumerate(rects):
        if not rect.born <= t < rect.dies or hidden[position] < params.flicker:
            continue
        age = t - rect.born
        scale = max(0.05, 1 + rect.grow * age)
        cx, cy = rect.x + rect.vx * age, rect.y + rect.vy * age
        half_w, half_h = rect.w * scale * width / 2, rect.h * scale * height / 2
        x0, x1 = int(round(cx * width - half_w)), int(round(cx * width + half_w))
        y0, y1 = int(round(cy * height - half_h)), int(round(cy * height + half_h))
        colour = palette[rect.colour]
        if rect.filled:
            _over(canvas, x0, y0, x1, y1, colour, params.fill_opacity * envelope * pulse)
        else:
            alpha = min(1.0, envelope * pulse)
            _over(canvas, x0, y0, x1, y0 + stroke, colour, alpha)
            _over(canvas, x0, y1 - stroke, x1, y1, colour, alpha)
            _over(canvas, x0, y0, x0 + stroke, y1, colour, alpha)
            _over(canvas, x1 - stroke, y0, x1, y1, colour, alpha)
    return (np.clip(canvas, 0, 1) * 255 + 0.5).astype(np.uint8)


def rect_frames(
    params: RectLayer, width: int, height: int, fps: float, frames: int, seed: int, beats: Sequence[float] | None = None
) -> Iterator[np.ndarray]:
    duration = frames / fps
    rects = rect_schedule(params, duration, seed, beats)
    for index in range(frames):
        yield rect_frame(rects, params, index / fps, index, width, height, seed, duration=duration, beats=beats)


def _over(canvas: np.ndarray, x0: int, y0: int, x1: int, y1: int, colour: np.ndarray, alpha: float) -> None:
    height, width = canvas.shape[:2]
    x0, x1 = max(0, x0), min(width, x1)
    y0, y1 = max(0, y0), min(height, y1)
    alpha = min(1.0, max(0.0, alpha))
    if x1 <= x0 or y1 <= y0 or alpha <= 0:
        return
    region = canvas[y0:y1, x0:x1]
    below = region[..., 3:4]
    out_alpha = alpha + below * (1 - alpha)
    safe = np.where(out_alpha > 0, out_alpha, 1)
    region[..., :3] = (colour * alpha + region[..., :3] * below * (1 - alpha)) / safe
    region[..., 3:4] = out_alpha
