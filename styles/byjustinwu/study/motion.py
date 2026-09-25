"""Measure graphic-layer motion in the 90 s intro excerpts.

Usage:
    python styles/byjustinwu/study/motion.py [excerpts] [--out motion.json]

The excerpts are ``<video_id>_first90s_480p*.mp4`` and never go in git. Put
them in the gitignored ``study/data/excerpts/`` (the default) or pass another
folder. Video ids live in the gitignored ``study/data/sources.json``
(``{"newest": "<id>", "cards": "<id>", "older": "<id>"}``). Output is
``study/data/motion.json``. Needs ffmpeg on PATH. For each region below the script decodes the excerpt at its
native frame rate, crops the region, and reports:

  diffs        mean absolute grey-level change between consecutive frames
  hold_frames  how many frames each texture state is held before it changes
               (a change is a frame-to-frame diff above ``CHANGE``)
  events       frames whose diff is above ``POP`` (a layer popping on or off,
               a subtitle switching, or a picture cut)

Regions are in excerpt pixels and were placed by eye on frame grabs; each has
the observation it backs in STYLE.md.
"""

from __future__ import annotations

import argparse
import collections
import json
import subprocess
from pathlib import Path

import numpy as np

DATA_DIR = Path(__file__).resolve().parent / "data"
CHANGE = 2.0
POP = 40.0

REGIONS = [
    {"key": "newest_date_stamp_texture", "video": "newest", "start": 2.9, "dur": 3.6,
     "crop": [620, 170, 120, 100], "note": "liquid gradient behind the date stamp"},
    {"key": "newest_date_stamp_on_off", "video": "newest", "start": 2.5, "dur": 4.5,
     "crop": [600, 150, 180, 150], "note": "rectangle pops on ~2.8 s and off ~6.67 s"},
    {"key": "cards_glitch_rect", "video": "cards", "start": 9.85, "dur": 1.75,
     "crop": [520, 150, 110, 80], "note": "yellow pixel-sorted rectangle"},
    {"key": "cards_lava_rect", "video": "cards", "start": 9.85, "dur": 1.7,
     "crop": [700, 230, 120, 100], "note": "red lava rectangle"},
    {"key": "cards_rects_on_off", "video": "cards", "start": 9.5, "dur": 2.6,
     "crop": [500, 140, 354, 200], "note": "both rectangles and the date stamp"},
    {"key": "older_title_card_art", "video": "older", "start": 12.85, "dur": 3.1,
     "crop": [160, 165, 70, 70], "note": "inverted manga line art behind the title card"},
]


def _fps(path: Path) -> float:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v", "-show_entries", "stream=r_frame_rate",
         "-of", "csv=p=0", str(path)], capture_output=True, text=True, check=True).stdout.strip()
    n, d = out.split("/")
    return float(n) / float(d)


def _frames(path: Path, start: float, dur: float, crop: list[int]) -> np.ndarray:
    x, y, w, h = crop
    raw = subprocess.run(
        ["ffmpeg", "-v", "error", "-ss", str(start), "-i", str(path), "-t", str(dur),
         "-vf", f"crop={w}:{h}:{x}:{y}", "-f", "rawvideo", "-pix_fmt", "gray", "-"],
        capture_output=True, check=True).stdout
    return np.frombuffer(raw, np.uint8).reshape(-1, h, w).astype(float)


def measure(path: Path, region: dict) -> dict:
    fps = _fps(path)
    frames = _frames(path, region["start"], region["dur"], region["crop"])
    diffs = np.abs(np.diff(frames, axis=0)).mean(axis=(1, 2))
    holds, run = [], 1
    for d in diffs:
        if d < CHANGE:
            run += 1
        else:
            holds.append(run)
            run = 1
    holds.append(run)
    events = [round(region["start"] + (i + 1) / fps, 3) for i, d in enumerate(diffs) if d > POP]
    counts = collections.Counter(holds[1:-1] or holds)
    typical = counts.most_common(1)[0][0]
    return {
        "note": region["note"], "fps": round(fps, 3), "frames": int(len(frames)),
        "diff_median": round(float(np.median(diffs)), 2),
        "hold_frames_counts": dict(sorted(counts.items())),
        "typical_hold_frames": typical,
        "typical_updates_per_s": round(fps / typical, 1),
        "events_s": events,
    }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("excerpts", type=Path, nargs="?", default=DATA_DIR / "excerpts")
    ap.add_argument("--out", type=Path, default=DATA_DIR / "motion.json")
    args = ap.parse_args(argv)
    sources_path = DATA_DIR / "sources.json"
    if not sources_path.is_file():
        raise SystemExit(f"missing {sources_path} (gitignored): map newest, cards, older to video ids")
    sources = json.loads(sources_path.read_text())
    result = {"change_threshold": CHANGE, "pop_threshold": POP, "regions": {}}
    for region in REGIONS:
        video_id = sources.get(region["video"], region["video"])
        matches = sorted(args.excerpts.glob(f"{video_id}_first90s*.mp4"))
        if not matches:
            raise SystemExit(f"no excerpt for {region['video']} in {args.excerpts}")
        result["regions"][region["key"]] = measure(matches[0], region)
    args.out.write_text(json.dumps(result, indent=2) + "\n")
    print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
