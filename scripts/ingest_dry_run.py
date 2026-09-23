#!/usr/bin/env python3
"""Dry-run ingest of fixtures/selects. No API key, no playable media.

The clips are a few bytes of text with video extensions. Lengths come from
fixtures/selects/durations.json, so ffprobe is not required.

    python scripts/ingest_dry_run.py
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from conductor.cli import main  # noqa: E402
from conductor.fcpxml import parse_fcpxml  # noqa: E402
from conductor.ingest import file_hash  # noqa: E402

MEDIA = ROOT / "fixtures" / "selects"
BRIEF = "A tight interview. Keep the guest's story, lose dead air."


def main_script() -> int:
    watched = [
        MEDIA / "a_interview.mp4",
        MEDIA / "b_flash.mov",
        MEDIA / "c_button.m4v",
        MEDIA / "notes.txt",
        MEDIA / "nested" / "ignored.mp4",
        MEDIA / ".hidden.mov",
    ]
    before = {path: file_hash(path) for path in watched}
    out = ROOT / "out" / "selects-dry-run"
    code = main(
        [
            "ingest",
            "--media",
            str(MEDIA),
            "--durations",
            str(MEDIA / "durations.json"),
            "--brief",
            BRIEF,
            "--out-dir",
            str(out),
        ]
    )
    if code != 0:
        return code
    for path, digest in before.items():
        if file_hash(path) != digest:
            raise SystemExit(f"dry-run modified source media: {path}")
    starter = out / "selects.fcpxml"
    document = parse_fcpxml(starter)
    spine = document.sequences[0].spine
    if [clip.name for clip in spine] != ["a_interview", "b_flash", "c_button"]:
        raise SystemExit(f"unexpected spine: {[clip.name for clip in spine]}")
    if spine[1].duration.numerator != 1 or spine[1].duration.denominator != 8:
        raise SystemExit(f"flash duration changed: {spine[1].duration}")
    for clip, asset_id in zip(spine, ("r2", "r3", "r4")):
        src = document.assets[clip.ref].src if clip.ref else ""
        if not src.startswith("file:///") or " " in src:
            raise SystemExit(f"media src is not an absolute file URL: {src}")
        if clip.ref != asset_id:
            raise SystemExit(f"expected asset {asset_id} for {clip.name}, got {clip.ref}")
    shadow = out / "selects.conductor.fcpxml"
    if not shadow.is_file() or not (out / "selects.conductor.md").is_file():
        raise SystemExit("missing shadow FCPXML or report")
    if (out / "selects.conductor.applied.fcpxml").exists():
        raise SystemExit("shadow ingest wrote an applied cut")
    print("ingest dry-run ok: 3 clips, starter + shadow FCPXML, source media unchanged")
    print(f"  starter {starter}")
    print(f"  shadow  {shadow}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main_script())
