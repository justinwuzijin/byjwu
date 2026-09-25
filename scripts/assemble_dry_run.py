#!/usr/bin/env python3
"""End-to-end dry run: a folder of clips + a music file → an assembled FCPXML.

    python scripts/assemble_dry_run.py [--out out/assembly-dry-run] [--no-ffmpeg]

Writes a synthetic shoot (three talking clips with SRT sidecars, two b-roll
clips, a 120 bpm song) into ``<out>/shoot``, then runs the assembly loop in
dry-run mode (no API key) with the provisional ``byjustinwu`` profile:
``<out>/v0 … vN``. Exits non-zero unless the final FCPXML is DTD-valid and
has music fade and duck keyframes, SF Pro subtitle titles, the rectangle
background layer, and markers explaining the choices.
"""

from __future__ import annotations

import argparse
import json
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from conductor.assembly.synth import make_fixture  # noqa: E402
from conductor.iterate import format_report, iterate  # noqa: E402
from conductor.validate import validate_fcpxml  # noqa: E402

BRIEF = "A 40-second byjustinwu-style test edit about cutting on the beat"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", default=str(ROOT / "out" / "assembly-dry-run"))
    parser.add_argument("--no-ffmpeg", action="store_true", help="placeholder clips + durations.json")
    parser.add_argument("--rounds", type=int, default=3)
    args = parser.parse_args()
    out = Path(args.out)
    shoot = out / "shoot"
    info = make_fixture(shoot, use_ffmpeg=not args.no_ffmpeg)
    durations = None if info["real_clips"] else shoot / "durations.json"
    print(f"shoot  {shoot} ({'ffmpeg clips' if info['real_clips'] else 'placeholder clips + durations.json'})")
    result = iterate(
        media=shoot,
        brief=BRIEF,
        out_dir=out,
        style="byjustinwu",
        durations_path=durations,
        max_rounds=args.rounds,
    )
    print(format_report(result))
    final = result.final
    report = json.loads((final.parent / "assembly.json").read_text())
    checks = _checks(final, report)
    print()
    print(structure(final, report))
    print()
    failed = [name for name, ok in checks.items() if not ok]
    for name, ok in checks.items():
        print(f"{'ok  ' if ok else 'FAIL'}  {name}")
    return 1 if failed else 0


def _checks(final: Path, report: dict) -> dict[str, bool]:
    root = ET.parse(final).getroot()
    errors = validate_fcpxml(final)
    music = [c for c in root.iter("asset-clip") if c.get("audioRole") == "music"]
    keys = [float(k.get("value")[:-2]) for c in music for k in c.iter("keyframe")]
    subtitles = [t for t in root.iter("title") if (t.get("name") or "").startswith("Subtitle")]
    fonts = {t.find("text-style-def/text-style").get("font") for t in root.iter("title")}
    stills = list(root.iter("video"))
    markers = [m.get("value") for m in root.iter("marker")]
    return {
        "DTD-valid FCPXML 1.11" if errors is not None else "DTD check (install lxml)": errors == [],
        "music fades in from and out to silence": bool(keys) and keys[0] == -96 and keys[-1] == -96,
        "music ducks under dialogue": report["metrics"]["ducking"]["checked"] > 0
        and report["metrics"]["ducking"]["compliant"] == report["metrics"]["ducking"]["checked"],
        "SF Pro subtitle titles": bool(subtitles) and all(f.startswith("SF Pro") for f in fonts),
        "rectangle background layer": bool(stills) and report["metrics"]["background"]["coverage"] == 1.0,
        "markers explain choices": any(m.startswith("jevid · music") for m in markers)
        and any(m.startswith("jevid · background") for m in markers)
        and any("speech" in m for m in markers),
    }


def structure(final: Path, report: dict) -> str:
    """A text tree of the final timeline: sections, then what hangs off them."""
    root = ET.parse(final).getroot()
    spine = root.find(".//spine")
    lines = [f"{report['timeline']['name']}  {report['timeline']['duration_seconds']:.2f}s  ({final.name})"]
    for row in report["structure"]:
        lines.append(f"├─ {row['label']:<10} {row['start']}  {row['duration_seconds']:>5.2f}s")
        items = [c for c in spine if _starts_in(c, row)]
        for clip in items:
            anchored = [a for a in clip if a.get("lane")]
            kinds: dict[str, int] = {}
            for a in anchored:
                label = _label(a)
                kinds[label] = kinds.get(label, 0) + 1
            extra = ", ".join(f"{n}× {k}" for k, n in kinds.items())
            name = clip.get("name")
            split = " J/L" if clip.get("audioStart") or clip.get("audioDuration") else ""
            transform = clip.find("adjust-transform")
            scale = f" scale {transform.get('scale')}" if transform is not None and transform.get("scale") else ""
            lines.append(f"│   ├─ {clip.tag:<10} {name}{split}{scale}" + (f"  [{extra}]" if extra else ""))
    music = report["music"]
    for row in music:
        lines.append(
            f"└─ music {row['song']} {row['start']} {row['duration_seconds']:.2f}s, fade in {row['fade_in_seconds']}s, "
            f"out {row['fade_out_seconds']}s, {row['volume_keyframes']} volume keyframes"
        )
    return "\n".join(lines)


def _starts_in(clip: ET.Element, row: dict) -> bool:
    from conductor.timeutil import parse_time

    start = float(parse_time(clip.get("offset"), 0))
    return row["start_seconds"] - 2e-3 <= start < row["start_seconds"] + row["duration_seconds"] - 2e-3


def _label(element: ET.Element) -> str:
    lane = int(element.get("lane"))
    if element.tag == "title":
        return "subtitle" if (element.get("name") or "").startswith("Subtitle") else "title card"
    if element.tag == "video":
        return "background plate" if (element.get("name") or "").startswith("Background") else "floating rect"
    if element.get("audioRole") == "music":
        return "music"
    if lane > 0:
        return "cutaway"
    return element.tag


if __name__ == "__main__":
    raise SystemExit(main())
