"""The abstract rectangle background layer, rendered to PNG stills.

Digital art (images from ``background.art.folder``, or procedural art when
there is none) is cut into rectangles of mixed sizes and ratios, then each
rectangle is contorted: stretched, sheared, sliced, mirrored, and smeared by
a pixel sort. A *plate* is a full-frame composite of those rectangles over the
base colour; *floaters* are single rectangles placed on their own lanes so
they can drift independently. Motion is FCPXML keyframes, added by the
planner; this module only makes pixels and records what it did.

Everything is seeded by ``background.art.seed`` and the plate index, so the
same profile renders the same bytes. File names carry a digest of the inputs,
so iterate rounds that change nothing share files.
"""

from __future__ import annotations

import hashlib
import json
import math
import random
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from ..style import StyleProfile, hex_rgba
from .png import read_image, write_png

ART_SIZE = (540, 960)
RENDER_VERSION = 1
IMAGE_EXTENSIONS = frozenset({".png", ".jpg", ".jpeg", ".webp", ".tif", ".tiff"})


@dataclass
class Rect:
    x: int
    y: int
    w: int
    h: int
    stretch: tuple[float, float]
    shear: float
    slices: int
    mirror: bool
    smear: bool

    def to_state(self) -> dict:
        return {
            "box": [self.x, self.y, self.w, self.h],
            "stretch": [round(self.stretch[0], 3), round(self.stretch[1], 3)],
            "shear_degrees": round(self.shear, 2),
            "slices": self.slices,
            "mirror": self.mirror,
            "smear": self.smear,
        }


@dataclass
class Still:
    path: Path
    width: int
    height: int
    kind: str
    layout: str
    seed: int
    art: str
    rects: list[Rect] = field(default_factory=list)

    def describe(self) -> str:
        return (
            f"{self.kind} layout={self.layout} rects={len(self.rects)} seed={self.seed} art={self.art}"
        )


class ArtSource:
    """Source pixels for contortion: the art folder in name order, else procedural."""

    def __init__(self, profile: StyleProfile, folder: Path | None = None):
        self.profile = profile
        self.warnings: list[str] = []
        configured = folder or profile.get("background.art.folder", None)
        self.images: list[Path] = []
        if configured:
            root = Path(configured)
            if root.is_dir():
                self.images = sorted(
                    p for p in root.iterdir() if p.is_file() and p.suffix.lower() in IMAGE_EXTENSIONS
                )
            if not self.images:
                self.warnings.append(f"background art folder {root} has no readable images; using procedural art")
        self._cache: dict[int, tuple[np.ndarray, str]] = {}

    def get(self, index: int) -> tuple[np.ndarray, str]:
        if index in self._cache:
            return self._cache[index]
        found: tuple[np.ndarray, str] | None = None
        if self.images:
            path = self.images[index % len(self.images)]
            pixels = read_image(path)
            if pixels is None:
                self.warnings.append(f"{path.name}: could not decode (install Pillow for JPEG); procedural used")
            else:
                found = (pixels, path.name)
        if found is None:
            mode = str(self.profile.get("background.art.procedural"))
            seed = int(self.profile.get("background.art.seed")) + index
            found = (procedural_art(mode, seed, self.palette()), f"procedural:{mode}")
        self._cache[index] = found
        return found

    def palette(self) -> np.ndarray:
        colours = [hex_rgba(c)[:3] for c in self.profile.get("background.palette")]
        return (np.asarray(colours) * 255.0).astype(np.float32)


def procedural_art(mode: str, seed: int, palette: np.ndarray) -> np.ndarray:
    rng = np.random.default_rng(seed)
    height, width = ART_SIZE
    yy, xx = np.mgrid[0:height, 0:width].astype(np.float32)
    xx /= width
    yy /= height
    if mode == "noise":
        coarse = rng.random((9, 16)).astype(np.float32)
        field_ = np.kron(coarse, np.ones((height // 9 + 1, width // 16 + 1), dtype=np.float32))[:height, :width]
        field_ = 0.7 * field_ + 0.3 * np.sin(12 * xx + 6 * yy + rng.random() * 6)
    elif mode == "stripes":
        angle = rng.uniform(0, math.pi)
        warp = 0.08 * np.sin(2 * math.pi * (3 * yy + rng.random()))
        field_ = np.sin(2 * math.pi * 9 * (np.cos(angle) * xx + np.sin(angle) * yy + warp))
    else:
        field_ = np.zeros_like(xx)
        for _ in range(5):
            fx, fy = rng.uniform(-7, 7, size=2)
            phase = rng.uniform(0, 2 * math.pi)
            warp = rng.uniform(0.2, 1.4) * np.sin(2 * math.pi * (rng.uniform(1, 4) * yy + rng.random()))
            field_ += np.sin(2 * math.pi * (fx * xx + fy * yy) + phase + warp)
    field_ = (field_ - field_.min()) / max(1e-6, float(field_.max() - field_.min()))
    stops = palette[rng.permutation(len(palette))]
    index = np.clip(np.rint(field_ * (len(stops) - 1)).astype(int), 0, len(stops) - 1)
    return stops[index].astype(np.uint8)


def contort(art: np.ndarray, rect: Rect, rng: random.Random, slice_shift: float) -> np.ndarray:
    """Sample ``art`` into a ``rect.h × rect.w`` tile with the rect's distortions."""
    art_h, art_w = art.shape[:2]
    crop_w = min(art_w, max(4, int(rect.w / rect.stretch[0])))
    crop_h = min(art_h, max(4, int(rect.h / rect.stretch[1])))
    cx = rng.randint(0, max(0, art_w - crop_w))
    cy = rng.randint(0, max(0, art_h - crop_h))
    v = (np.arange(rect.h, dtype=np.float32) + 0.5) / rect.h
    u = (np.arange(rect.w, dtype=np.float32) + 0.5) / rect.w
    uu, vv = np.meshgrid(u, v)
    uu = uu + math.tan(math.radians(rect.shear)) * (vv - 0.5) * (rect.h / max(1, rect.w))
    if rect.slices:
        bands = np.minimum((vv * rect.slices).astype(int), rect.slices - 1)
        offsets = np.asarray([rng.uniform(-slice_shift, slice_shift) for _ in range(rect.slices)], dtype=np.float32)
        uu = uu + offsets[bands]
    if rect.mirror:
        uu = 1.0 - uu
    uu = np.mod(uu, 1.0)
    src_x = np.clip((cx + uu * crop_w).astype(int), 0, art_w - 1)
    src_y = np.clip((cy + vv * crop_h).astype(int), 0, art_h - 1)
    tile = art[src_y, src_x]
    if rect.smear and rect.w >= 8:
        start = rng.randint(0, rect.w // 2)
        end = min(rect.w, start + rng.randint(max(2, rect.w // 8), max(3, rect.w // 3)))
        band = tile[:, start:end]
        luminance = band.astype(np.float32) @ np.asarray([0.299, 0.587, 0.114], dtype=np.float32)
        order = np.argsort(luminance, axis=0, kind="stable")
        tile[:, start:end] = np.take_along_axis(band, order[..., None], axis=0)
    return tile


def render_plate(
    index: int,
    profile: StyleProfile,
    art: ArtSource,
    out_dir: Path,
    frame: tuple[int, int],
) -> Still:
    width, height = _scaled(profile, frame)
    seed = _seed(profile, index)
    rng = random.Random(seed)
    mode = str(profile.get("background.layout.mode"))
    layout = mode if mode != "mixed" else ("grid" if index % 2 == 0 else "random")
    rects = grid_layout(profile, rng, width, height) if layout == "grid" else random_layout(profile, rng, width, height)
    pixels, art_name = art.get(index)
    digest = _digest(profile, index, frame, art_name, "plate")
    path = out_dir / f"plate_{index:03d}_{digest}.png"
    if not path.is_file():
        base = np.asarray(hex_rgba(profile.get("background.base_color"))[:3]) * 255.0
        canvas = np.empty((height, width, 3), dtype=np.uint8)
        canvas[:] = base.astype(np.uint8)
        shift = float(profile.get("background.contortion.slice_shift"))
        for rect in rects:
            canvas[rect.y : rect.y + rect.h, rect.x : rect.x + rect.w] = contort(pixels, rect, rng, shift)
        write_png(path, canvas)
    return Still(path, width, height, "plate", layout, seed, art_name, rects)


def render_floater(
    index: int,
    profile: StyleProfile,
    art: ArtSource,
    out_dir: Path,
    frame: tuple[int, int],
) -> Still:
    width, height = _scaled(profile, frame)
    seed = _seed(profile, 1000 + index)
    rng = random.Random(seed)
    low, high = profile.get("background.motion.floating_size")
    ratio = rng.choice(profile.get("background.layout.size_ratios"))
    tile_h = max(8, int(rng.uniform(low, high) * height))
    tile_w = max(8, int(tile_h * ratio[0] / ratio[1]))
    tile_w = min(tile_w, width)
    rect = _contortion(profile, rng, 0, 0, tile_w, tile_h)
    pixels, art_name = art.get(index + 7)
    digest = _digest(profile, 1000 + index, frame, art_name, "floater")
    path = out_dir / f"floater_{index:03d}_{digest}.png"
    if not path.is_file():
        tile = contort(pixels, rect, rng, float(profile.get("background.contortion.slice_shift")))
        write_png(path, tile)
    return Still(path, tile_w, tile_h, "floater", "single", seed, art_name, [rect])


def grid_layout(profile: StyleProfile, rng: random.Random, width: int, height: int) -> list[Rect]:
    columns = int(profile.get("background.layout.grid.columns"))
    rows = int(profile.get("background.layout.grid.rows"))
    merge = float(profile.get("background.layout.grid.merge_probability"))
    fill = float(profile.get("background.layout.grid.fill"))
    margin = float(profile.get("background.layout.margin"))
    gutter = max(1, int(float(profile.get("background.layout.gutter")) * height))
    left, top = int(margin * width), int(margin * height)
    cell_w = (width - 2 * left) / columns
    cell_h = (height - 2 * top) / rows
    ratios = profile.get("background.layout.size_ratios")
    used = np.zeros((rows, columns), dtype=bool)
    rects: list[Rect] = []
    target = int(round(fill * rows * columns))
    attempts = 0
    while used.sum() < target and attempts < rows * columns * 8:
        attempts += 1
        col, row = rng.randrange(columns), rng.randrange(rows)
        span_w = span_h = 1
        if rng.random() < merge:
            ratio = rng.choice(ratios)
            unit = rng.choice((1, 1, 2))
            span_w = max(1, min(columns - col, int(round(unit * ratio[0] / min(ratio)))))
            span_h = max(1, min(rows - row, int(round(unit * ratio[1] / min(ratio)))))
        if used[row : row + span_h, col : col + span_w].any():
            continue
        used[row : row + span_h, col : col + span_w] = True
        x = int(left + col * cell_w) + gutter // 2
        y = int(top + row * cell_h) + gutter // 2
        w = max(2, int(span_w * cell_w) - gutter)
        h = max(2, int(span_h * cell_h) - gutter)
        rects.append(_contortion(profile, rng, x, y, min(w, width - x), min(h, height - y)))
    return rects


def random_layout(profile: StyleProfile, rng: random.Random, width: int, height: int) -> list[Rect]:
    low_count, high_count = profile.get("background.layout.random.count")
    low, high = profile.get("background.layout.size_range")
    ratios = profile.get("background.layout.size_ratios")
    margin = float(profile.get("background.layout.margin"))
    count = rng.randint(int(low_count), int(high_count))
    rects: list[Rect] = []
    for _ in range(count):
        for _attempt in range(12):
            ratio = rng.choice(ratios)
            h = max(4, int(rng.uniform(low, high) * height))
            w = max(4, int(h * ratio[0] / ratio[1]))
            w = min(w, int(width * (1 - 2 * margin)))
            x = rng.randint(int(margin * width), max(int(margin * width), width - int(margin * width) - w))
            y = rng.randint(int(margin * height), max(int(margin * height), height - int(margin * height) - h))
            if all(_overlap((x, y, w, h), (r.x, r.y, r.w, r.h)) < 0.3 * w * h for r in rects):
                rects.append(_contortion(profile, rng, x, y, min(w, width - x), min(h, height - y)))
                break
    return rects


def _contortion(profile: StyleProfile, rng: random.Random, x: int, y: int, w: int, h: int) -> Rect:
    s_low, s_high = profile.get("background.contortion.stretch")
    stretch_x = math.exp(rng.uniform(math.log(s_low), math.log(s_high)))
    stretch_y = math.exp(rng.uniform(math.log(s_low), math.log(s_high)))
    shear = rng.uniform(*profile.get("background.contortion.shear_degrees"))
    slices_low, slices_high = profile.get("background.contortion.slices")
    return Rect(
        x=x,
        y=y,
        w=max(1, w),
        h=max(1, h),
        stretch=(stretch_x, stretch_y),
        shear=shear,
        slices=rng.randint(int(slices_low), int(slices_high)),
        mirror=rng.random() < float(profile.get("background.contortion.mirror")),
        smear=rng.random() < float(profile.get("background.contortion.smear")),
    )


def _overlap(a: tuple[int, int, int, int], b: tuple[int, int, int, int]) -> int:
    width = min(a[0] + a[2], b[0] + b[2]) - max(a[0], b[0])
    height = min(a[1] + a[3], b[1] + b[3]) - max(a[1], b[1])
    return max(0, width) * max(0, height)


def _scaled(profile: StyleProfile, frame: tuple[int, int]) -> tuple[int, int]:
    scale = float(profile.get("background.render_scale"))
    return max(16, int(round(frame[0] * scale))), max(16, int(round(frame[1] * scale)))


def _seed(profile: StyleProfile, index: int) -> int:
    return int(profile.get("background.art.seed")) * 7919 + index


def _digest(profile: StyleProfile, index: int, frame: tuple[int, int], art: str, kind: str) -> str:
    spec = {
        "background": profile.get("background"),
        "index": index,
        "frame": list(frame),
        "art": art,
        "kind": kind,
        "render_version": RENDER_VERSION,
    }
    text = json.dumps(spec, sort_keys=True, default=str)
    return hashlib.blake2b(text.encode("utf-8"), digest_size=5).hexdigest()
