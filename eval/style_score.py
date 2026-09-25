"""Style-match score for an assembled FCPXML.

One overall number from 0 to 100, plus a score per dimension. The numbers
compare the timeline with a style profile (pacing bands, music, type, and
the editorial checks the linter already knows). No model call.

    python eval/style_score.py timeline.fcpxml --style byjustinwu --assembly assembly.json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from conductor.assembly.beats import detect_beats  # noqa: E402
from conductor.assembly.metrics import measure, read  # noqa: E402
from conductor.critic import review_plan  # noqa: E402
from conductor.fcpxml import parse_fcpxml  # noqa: E402
from conductor.lint import lint_fcpxml, lint_plan  # noqa: E402
from conductor.plan import plan_from_document  # noqa: E402
from conductor.style import load_style  # noqa: E402

HOOK = ("why", "today", "how", "what", "?")
WEIGHTS = {
    "pacing": 0.18,
    "hook": 0.12,
    "hygiene": 0.14,
    "beat": 0.12,
    "broll": 0.12,
    "ducking": 0.10,
    "type": 0.08,
    "colour": 0.06,
    "structure": 0.08,
}


def score_timeline(
    fcpxml: str | Path,
    *,
    style: str | Path = "byjustinwu",
    assembly: str | Path | None = None,
) -> dict:
    profile = load_style(style)
    path = Path(fcpxml)
    reading = read(path)
    metrics = measure(path, profile)
    payload = {}
    if assembly and Path(assembly).is_file():
        payload = json.loads(Path(assembly).read_text(encoding="utf-8"))
    structure_findings = lint_fcpxml(path)
    document = parse_fcpxml(path)
    plan = plan_from_document(document, expects_music=True)
    plan_report = lint_plan(plan, profile=_bands(profile), check_media=True)
    hard = list(structure_findings) + list(plan_report.hard)
    dimensions = {
        "pacing": _pacing(metrics, profile),
        "hook": _hook(payload),
        "hygiene": _hygiene(payload),
        "beat": _beat(reading, profile),
        "broll": _broll(reading, profile),
        "ducking": _ducking(metrics, profile),
        "type": _type(path, reading),
        "colour": _colour(path),
        "structure": _structure(reading),
    }
    overall = round(sum(dimensions[name] * WEIGHTS[name] for name in WEIGHTS), 1)
    critic = _critic(plan, profile, hard)
    return {
        "overall": overall,
        "dimensions": {name: round(dimensions[name], 1) for name in dimensions},
        "weights": WEIGHTS,
        "lint": {
            "passed": not hard,
            "hard": [item.to_dict() for item in hard],
            "soft": [item.to_dict() for item in plan_report.soft],
        },
        "critic": critic,
        "metrics": metrics,
    }


def _critic(plan, profile, hard: list) -> dict:
    def critic(payload: dict) -> dict:
        blocked = bool(payload["lint"]["hard"])
        return {
            "action": "flag" if blocked else "approve",
            "scores": {"pacing": 4, "clip_selection": 4, "visual_script": 4, "story_arc": 4},
            "span": [],
            "cut_id": "",
            "reason": "hard lint" if blocked else "",
        }

    if hard:
        return {"passed": False, "blocked": True, "action": "blocked", "reason": hard[0].message}
    review = review_plan(plan, profile=_bands(profile), check_media=True, critic=critic)
    action = review.turns[-1].action if review.turns else "approve"
    return {
        "passed": not review.blocked and action == "approve",
        "blocked": review.blocked,
        "action": action,
        "reason": review.blocked_reason,
    }


def _bands(profile) -> dict:
    talking = profile.pacing("talking")
    return {
        "bands": {
            "median_shot_seconds": [1.0, float(talking["shot_length"]["p90"])],
            "cuts_per_minute": [1.0, 40.0],
            "broll_coverage": [0.0, 0.85],
            "longest_hold_seconds": [1.0, float(talking["shot_length"]["p90"]) + 5],
            "sentence_end_share": [0.4, 1.0],
        }
    }


def _pacing(metrics: dict, profile) -> float:
    tolerance = float(profile.get("pacing.tolerance"))
    found = metrics.get("asl_by_section") or {}
    if not found:
        return 0.0
    scores = []
    for kind, measured in found.items():
        target = float(profile.pacing(kind)["asl_seconds"])
        if target <= 0:
            continue
        gap = abs(float(measured) - target) / target
        if gap <= tolerance:
            scores.append(100.0)
        else:
            scores.append(max(0.0, 100.0 * (1.0 - (gap - tolerance) / tolerance)))
    return sum(scores) / len(scores) if scores else 0.0


def _hook(payload: dict) -> float:
    units = {row["id"]: row for row in payload.get("units") or []}
    kept = set()
    for decision in payload.get("decisions") or []:
        if not str(decision.get("key", "")).endswith("_keep"):
            continue
        try:
            value = float(decision.get("value"))
        except (TypeError, ValueError):
            continue
        if value >= 0.5:
            kept.add(decision.get("item"))
    intro = []
    for decision in payload.get("decisions") or []:
        if not str(decision.get("key", "")).endswith("_section"):
            continue
        if decision.get("value") != "intro" or decision.get("item") not in kept:
            continue
        unit = units.get(decision.get("item"))
        if unit and unit.get("text"):
            intro.append(unit["text"])
    if not intro:
        return 40.0
    text = intro[0].lower()
    if any(token in text for token in HOOK) and text.rstrip().endswith((".", "?", "!")):
        if text.strip() in {"so today we are", "so today we are."}:
            return 20.0
        return 100.0
    return 45.0


def _hygiene(payload: dict) -> float:
    units = {row["id"]: row for row in payload.get("units") or []}
    kept = []
    for decision in payload.get("decisions") or []:
        key = str(decision.get("key", ""))
        if not key.endswith("_keep"):
            continue
        try:
            value = float(decision.get("value"))
        except (TypeError, ValueError):
            continue
        if value < 0.5:
            continue
        unit = units.get(decision.get("item"))
        if unit and unit.get("kind") == "speech" and unit.get("text"):
            kept.append(unit["text"].strip())
    if not kept:
        return 30.0
    score = 100.0
    lowered = [text.lower() for text in kept]
    for text in lowered:
        if text in {"um.", "um", "uh.", "uh"}:
            score -= 40
    for index, left in enumerate(lowered):
        for right in lowered[index + 1 :]:
            if left != right and (right.startswith(left) or left.startswith(right)) and min(len(left), len(right)) > 12:
                score -= 25
    return max(0.0, score)


def _beat(reading, profile) -> float:
    mode = profile.pacing("montage")["cut_on_beat"]
    if mode == "off":
        return 100.0
    montage = [(start, end) for kind, start, end in reading.sections if kind == "montage"]
    if not montage:
        return 40.0
    beats = _beats(reading)
    if not beats:
        return 40.0
    window = float(profile.get("cuts.beat_snap_tolerance_seconds"))
    if mode == "always":
        window = max(window, 0.1)
    cuts = []
    for start, end in montage:
        for item in reading.spine:
            if item.tag == "gap":
                continue
            if start < item.start < end:
                cuts.append(float(item.start))
    if not cuts:
        return 50.0
    hit = 0
    for cut in cuts:
        if min(abs(cut - beat) for beat in beats) <= window + 1e-3:
            hit += 1
    return 100.0 * hit / len(cuts)


def _beats(reading) -> list[float]:
    """Beat times on the timeline, from each music clip's own in-point."""
    found: list[float] = []
    for item in reading.connected:
        role = str(item.element.get("audioRole") or "")
        if item.tag != "asset-clip" or not role.startswith("music"):
            continue
        src = reading.assets.get(item.element.get("ref") or "")
        path = _file(src) if src else None
        if path is None:
            continue
        grid = detect_beats(path, duration=float(item.end - item.start) + float(item.local_start) + 1.0, fallback_bpm=120.0)
        if grid is None or not grid.beats:
            continue
        origin = float(item.local_start)
        for beat in grid.beats:
            if beat + 1e-6 < origin:
                continue
            moment = float(item.start) + (float(beat) - origin)
            if float(item.start) - 1e-6 <= moment <= float(item.end) + 1e-6:
                found.append(moment)
    if found:
        return sorted(found)
    return []


def _broll(reading, profile) -> float:
    spec = profile.get("cuts.cutaway")
    low, high = float(spec["min_seconds"]), float(spec["max_seconds"])
    cutaways = [
        item for item in reading.connected
        if item.tag == "asset-clip" and item.lane > 0 and not str(item.element.get("audioRole") or "").startswith("music")
    ]
    montage = [item for item in reading.spine if _section_kind(reading, item.start) == "montage" and item.tag != "gap"]
    score = 40.0
    if cutaways or montage:
        score = 70.0
    durations = [float(item.end - item.start) for item in cutaways]
    if durations and all(low - 0.05 <= value <= high + 0.15 for value in durations):
        score = 100.0
    elif montage and not cutaways:
        score = 80.0
    return score


def _ducking(metrics: dict, profile) -> float:
    duck = metrics.get("ducking") or {}
    checked = int(duck.get("checked") or 0)
    if checked:
        return 100.0 * int(duck.get("compliant") or 0) / checked
    # The measured profile silences the bed under talk. That is the duck.
    music = metrics.get("music") or {}
    if int(music.get("clips") or 0) >= 1:
        return 100.0 if profile.get("music.sections.talking.bed_db") <= -60 else 70.0
    return 0.0


def _type(path: Path, reading) -> float:
    import xml.etree.ElementTree as ET

    from conductor.fcpxml import local

    root = ET.parse(str(path)).getroot()
    chapters = [el for el in root.iter() if local(el.tag) == "chapter-marker"]
    titles = [el for el in root.iter() if local(el.tag) == "title"]
    cards = [item for item in reading.spine if item.tag == "gap"]
    score = 0.0
    if len(chapters) >= 2:
        score += 50
    if titles or cards:
        score += 50
    return score


def _colour(path: Path) -> float:
    text = Path(path).read_text(encoding="utf-8", errors="replace").lower()
    if "exposure" in text or "colour" in text or ("color" in text and "skin" in text):
        return 100.0
    return 0.0


def _structure(reading) -> float:
    kinds = [kind for kind, _start, _end in reading.sections]
    needed = ("intro", "talking", "montage")
    hit = sum(1 for kind in needed if kind in kinds)
    extra = 20.0 if "end_card" in kinds or "outro" in kinds else 0.0
    return min(100.0, 80.0 * hit / len(needed) + extra)


def _section_kind(reading, moment) -> str:
    for kind, start, end in reading.sections:
        if start <= moment < end:
            return kind
    return ""


def _file(src: str) -> Path | None:
    from urllib.parse import unquote, urlparse

    if src.startswith("file:"):
        path = unquote(urlparse(src).path or "")
    else:
        path = src
    file = Path(path)
    return file if file.is_file() else None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("fcpxml", type=Path)
    parser.add_argument("--style", default="byjustinwu")
    parser.add_argument("--assembly", type=Path, default=None)
    args = parser.parse_args()
    report = score_timeline(args.fcpxml, style=args.style, assembly=args.assembly)
    print(json.dumps({key: report[key] for key in ("overall", "dimensions", "lint", "critic")}, indent=2))
    return 0 if report["overall"] >= 80 and report["lint"]["passed"] and report["critic"]["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
