"""Timeline linter. Hard findings block an export. Soft findings warn.

Hard checks are structural: overlap, flash frames, audio under a cutaway,
a cut inside a word, a music bed, media refs, frame alignment, title safe.
Soft checks compare pacing with a style profile's P10–P90 band. A missing
band is skipped, not failed.

Ideas: Cardboard "Verification: there's no linter for video", CutClaw's
non-overlap reviewer check, ButterCut's redundancy pass and media check.
Reimplemented from public descriptions. No code copied.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass, field
from fractions import Fraction
from pathlib import Path

from .plan import CUTAWAY, GAP, MUSIC, EditPlan, PlanClip, media_path

#: Two frames. Anything shorter is a flash.
FLASH_FRAMES = 2
#: Title-safe inset, as a fraction of each frame edge (SMPTE-style 80% area).
TITLE_SAFE_INSET = 0.10
#: Kept sentences closer than this (cosine of TF-IDF) are the same point twice.
REPEAT_COSINE = 0.80
_SENTENCE_END = re.compile(r"[.!?][\"')\]]*$")
_TOKEN = re.compile(r"[a-z0-9']+")

SOFT_BANDS = (
    "median_shot_seconds",
    "cuts_per_minute",
    "broll_coverage",
    "longest_hold_seconds",
    "sentence_end_share",
)


@dataclass(frozen=True)
class Finding:
    severity: str
    code: str
    message: str
    clip_id: str = ""
    start: float | None = None
    end: float | None = None

    def to_dict(self) -> dict:
        row = {"severity": self.severity, "code": self.code, "message": self.message}
        if self.clip_id:
            row["clip_id"] = self.clip_id
        if self.start is not None:
            row["start"] = self.start
        if self.end is not None:
            row["end"] = self.end
        return row


@dataclass
class LintReport:
    hard: list[Finding] = field(default_factory=list)
    soft: list[Finding] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    stats: dict = field(default_factory=dict)

    @property
    def blocked(self) -> bool:
        return bool(self.hard)

    def to_dict(self) -> dict:
        return {
            "blocked": self.blocked,
            "hard": [item.to_dict() for item in self.hard],
            "soft": [item.to_dict() for item in self.soft],
            "skipped": list(self.skipped),
            "stats": self.stats,
        }


def lint_plan(
    plan: EditPlan,
    *,
    profile: dict | None = None,
    check_media: bool = True,
) -> LintReport:
    """Run every check. ``profile`` bands are ``{name: [p10, p90]}``."""
    report = LintReport()
    _hard(plan, report, check_media=check_media)
    _soft(plan, report, _bands(profile))
    return report


def _hard(plan: EditPlan, report: LintReport, *, check_media: bool) -> None:
    frame = plan.frame_duration
    if frame <= 0:
        report.hard.append(Finding("hard", "time_invalid", "frame duration must be positive"))
        return
    _overlaps(plan, report)
    flash_limit = frame * FLASH_FRAMES
    for clip in plan.clips:
        if clip.role == GAP:
            continue
        if clip.duration <= 0:
            report.hard.append(
                Finding("hard", "time_invalid", f"{clip.name or clip.id} has no positive duration", clip.id)
            )
            continue
        if clip.role != MUSIC and clip.duration < flash_limit:
            report.hard.append(
                Finding(
                    "hard",
                    "flash_frame",
                    f"{clip.name or clip.id} is {float(clip.duration):.4f}s, under {FLASH_FRAMES} frames",
                    clip.id,
                    _f(clip.timeline_start),
                    _f(clip.timeline_end),
                )
            )
        _aligned(plan, clip, report)
        if check_media:
            _media(clip, report)
    _audio_gaps(plan, report)
    _words(plan, report)
    _music(plan, report)
    _titles(plan, report)


def _overlaps(plan: EditPlan, report: LintReport) -> None:
    grouped: dict[str, list[PlanClip]] = {}
    for clip in plan.clips:
        if (
            clip.role in {GAP, MUSIC}
            or clip.asset_id is None
            or clip.source_start is None
            or clip.source_end is None
        ):
            continue
        grouped.setdefault(clip.asset_id, []).append(clip)
    for asset_id, clips in grouped.items():
        ordered = sorted(clips, key=lambda item: (item.source_start, item.source_end, item.id))
        for index, left in enumerate(ordered):
            for right in ordered[index + 1 :]:
                if right.source_start >= left.source_end:
                    break
                if right.source_start < left.source_end and left.source_start < right.source_end:
                    first, second = sorted(
                        (left, right), key=lambda item: (item.name, float(item.timeline_start), item.id)
                    )
                    report.hard.append(
                        Finding(
                            "hard",
                            "source_overlap",
                            f"{first.name or first.id} and {second.name or second.id} reuse {asset_id} "
                            f"from {_f(max(left.source_start, right.source_start))}s",
                            first.id,
                            _f(first.timeline_start),
                            _f(first.timeline_end),
                        )
                    )


def _aligned(plan: EditPlan, clip: PlanClip, report: LintReport) -> None:
    frame = plan.frame_duration
    for label, value in (
        ("timeline in", clip.timeline_start),
        ("timeline out", clip.timeline_end),
        ("source in", clip.source_start),
        ("source out", clip.source_end),
    ):
        if value is None:
            continue
        if value < 0:
            report.hard.append(
                Finding("hard", "time_invalid", f"{clip.name or clip.id} {label} is negative", clip.id)
            )
        elif value % frame != 0:
            report.hard.append(
                Finding(
                    "hard",
                    "time_align",
                    f"{clip.name or clip.id} {label} {_f(value)}s is not a multiple of the frame",
                    clip.id,
                )
            )


def _media(clip: PlanClip, report: LintReport) -> None:
    if clip.role == GAP or clip.asset_id is None:
        return
    if not clip.asset_src:
        report.hard.append(
            Finding("hard", "media_ref", f"{clip.name or clip.id} has no media ref", clip.id)
        )
        return
    path = media_path(clip.asset_src)
    if path is None or not Path(path).is_file():
        report.hard.append(
            Finding(
                "hard",
                "media_missing",
                f"{clip.name or clip.id} media does not resolve ({clip.asset_src})",
                clip.id,
            )
        )


def _audio_gaps(plan: EditPlan, report: LintReport) -> None:
    picture = [clip for clip in plan.clips if clip.role == CUTAWAY]
    audio = [clip for clip in plan.clips if clip.role != GAP and clip.has_audio and clip.lane in {None, 0}]
    for cutaway in picture:
        if cutaway.intended_gap:
            continue
        if _covered(cutaway.timeline_start, cutaway.timeline_end, audio):
            continue
        report.hard.append(
            Finding(
                "hard",
                "audio_gap",
                f"cutaway {cutaway.name or cutaway.id} sits over a gap in the A-roll audio",
                cutaway.id,
                _f(cutaway.timeline_start),
                _f(cutaway.timeline_end),
            )
        )


def _words(plan: EditPlan, report: LintReport) -> None:
    if not plan.words:
        return
    if plan.cut_edges:
        boundaries = [(moment, "") for moment in plan.cut_edges]
    else:
        boundaries = []
        spine = sorted(plan.spine(), key=lambda item: item.timeline_start)
        for index, clip in enumerate(spine):
            if index:
                boundaries.append((clip.timeline_start, clip.id))
            if index < len(spine) - 1:
                boundaries.append((clip.timeline_end, clip.id))
    seen: set[tuple[str, float]] = set()
    for moment, clip_id in boundaries:
        for word in plan.words:
            if word.start < moment < word.end:
                key = (clip_id, _f(moment))
                if key in seen:
                    continue
                seen.add(key)
                report.hard.append(
                    Finding(
                        "hard",
                        "mid_word",
                        f"cut at {_f(moment)}s falls inside “{word.text}”",
                        clip_id,
                        _f(moment),
                        _f(word.end),
                    )
                )


def _music(plan: EditPlan, report: LintReport) -> None:
    beds = sorted(plan.music(), key=lambda item: item.timeline_start)
    if not beds and not plan.expects_music:
        return
    if not beds:
        report.hard.append(Finding("hard", "music_coverage", "the timeline has no music bed"))
        return
    if beds[0].timeline_start > 0 or beds[-1].timeline_end < plan.duration:
        report.hard.append(
            Finding(
                "hard",
                "music_coverage",
                "the music bed does not cover the timeline",
                beds[0].id,
                _f(beds[0].timeline_start),
                _f(beds[-1].timeline_end),
            )
        )
    for index in range(len(beds) - 1):
        if beds[index].timeline_end < beds[index + 1].timeline_start:
            report.hard.append(
                Finding(
                    "hard",
                    "music_coverage",
                    "the music bed has a hole",
                    beds[index].id,
                    _f(beds[index].timeline_end),
                    _f(beds[index + 1].timeline_start),
                )
            )
    if beds[0].fade_in <= 0:
        report.hard.append(
            Finding("hard", "music_fade", "the music bed has no fade at the head", beds[0].id)
        )
    if beds[-1].fade_out <= 0:
        report.hard.append(
            Finding("hard", "music_fade", "the music bed has no fade at the tail", beds[-1].id)
        )


def _titles(plan: EditPlan, report: LintReport) -> None:
    if plan.width <= 0 or plan.height <= 0:
        return
    inset_x = plan.width * TITLE_SAFE_INSET
    inset_y = plan.height * TITLE_SAFE_INSET
    for clip in plan.titles():
        x, y = clip.position or (0.0, 0.0)
        # Position is an offset from the centre of the frame.
        abs_x = plan.width / 2 + x
        abs_y = plan.height / 2 + y
        if abs_x < inset_x or abs_x > plan.width - inset_x or abs_y < inset_y or abs_y > plan.height - inset_y:
            report.hard.append(
                Finding(
                    "hard",
                    "title_safe",
                    f"{clip.name or clip.id} sits outside the title-safe area",
                    clip.id,
                    _f(clip.timeline_start),
                    _f(clip.timeline_end),
                )
            )


def _soft(plan: EditPlan, report: LintReport, bands: dict[str, tuple[float, float]]) -> None:
    spine = [clip for clip in plan.spine() if clip.duration > 0]
    durations = sorted(float(clip.duration) for clip in spine)
    minutes = float(plan.duration) / 60 if plan.duration else 0.0
    cuts = max(len(spine) - 1, 0)
    stats = {
        "median_shot_seconds": _median(durations),
        "cuts_per_minute": (cuts / minutes) if minutes else 0.0,
        "broll_coverage": _coverage(plan),
        "longest_hold_seconds": max(durations) if durations else 0.0,
        "sentence_end_share": _sentence_share(spine),
    }
    report.stats = {key: round(value, 6) for key, value in stats.items()}
    for name in SOFT_BANDS:
        band = bands.get(name)
        if band is None:
            report.skipped.append(name)
            continue
        low, high = band
        value = stats[name]
        if value < low or value > high:
            report.soft.append(
                Finding(
                    "soft",
                    name,
                    f"{name} is {value:.3f}, outside the style band {low:.3f}–{high:.3f}",
                )
            )
    _repeats(spine, report)
    _harmony(plan, report)


def _harmony(plan: EditPlan, report: LintReport) -> None:
    """Soft AV-harmony: visual-only cuts should sit within 0.1s of a keypoint."""
    if not plan.keypoints:
        report.stats["av_harmony"] = None
        return
    moments = _visual_cuts(plan)
    fraction = _near_keypoints(moments, plan.keypoints, tolerance=0.1)
    report.stats["av_harmony"] = None if fraction is None else round(fraction, 4)
    if fraction is None:
        return
    if fraction + 1e-9 < 0.8:
        report.soft.append(
            Finding(
                "soft",
                "av_harmony",
                f"{fraction:.0%} of visual cuts sit within 0.1s of a music keypoint",
            )
        )


def _near_keypoints(moments: list[float], keypoints: list[float], *, tolerance: float) -> float | None:
    if not moments or not keypoints:
        return None
    hits = sum(1 for moment in moments if any(abs(point - moment) <= tolerance + 1e-9 for point in keypoints))
    return hits / len(moments)


def _visual_cuts(plan: EditPlan) -> list[float]:
    moments: list[float] = []
    for clip in plan.cutaways():
        moments.append(float(clip.timeline_start))
        moments.append(float(clip.timeline_end))
    for clip in plan.spine():
        if clip.has_audio:
            continue
        moments.append(float(clip.timeline_end))
    return moments


def _repeats(spine: list[PlanClip], report: LintReport) -> None:
    sentences = [clip.text.strip() for clip in spine if clip.text.strip()]
    if len(sentences) < 2:
        return
    vectors = [_tfidf(text, sentences) for text in sentences]
    for index, left in enumerate(vectors):
        for right_index in range(index + 1, len(vectors)):
            score = _cosine(left, vectors[right_index])
            if score > REPEAT_COSINE:
                report.soft.append(
                    Finding(
                        "soft",
                        "repeated_point",
                        f"kept lines repeat (cosine {score:.2f}): “{_short(sentences[index])}” and "
                        f"“{_short(sentences[right_index])}”",
                    )
                )


def _bands(profile: dict | None) -> dict[str, tuple[float, float]]:
    if not profile:
        return {}
    raw = profile.get("bands", profile) if isinstance(profile, dict) else {}
    found: dict[str, tuple[float, float]] = {}
    if not isinstance(raw, dict):
        return found
    for name in SOFT_BANDS:
        band = raw.get(name)
        if isinstance(band, dict) and "p10" in band and "p90" in band:
            found[name] = (float(band["p10"]), float(band["p90"]))
        elif isinstance(band, (list, tuple)) and len(band) == 2:
            found[name] = (float(band[0]), float(band[1]))
    return found


def _covered(start: Fraction, end: Fraction, clips: list[PlanClip]) -> bool:
    cursor = start
    for clip in sorted(clips, key=lambda item: item.timeline_start):
        if clip.timeline_end <= cursor:
            continue
        if clip.timeline_start > cursor:
            return False
        cursor = max(cursor, clip.timeline_end)
        if cursor >= end:
            return True
    return cursor >= end


def _coverage(plan: EditPlan) -> float:
    if plan.duration <= 0:
        return 0.0
    spans = sorted((clip.timeline_start, clip.timeline_end) for clip in plan.cutaways())
    covered = Fraction(0)
    cursor = None
    end = None
    for start, stop in spans:
        if cursor is None:
            cursor, end = start, stop
            continue
        if start > end:
            covered += end - cursor
            cursor, end = start, stop
        else:
            end = max(end, stop)
    if cursor is not None and end is not None:
        covered += end - cursor
    return float(covered / plan.duration)


def _sentence_share(spine: list[PlanClip]) -> float:
    texts = [clip.text.strip() for clip in spine if clip.text.strip()]
    if not texts:
        return 1.0
    ended = sum(1 for text in texts if _SENTENCE_END.search(text))
    return ended / len(texts)


def _median(values: list[float]) -> float:
    if not values:
        return 0.0
    mid = len(values) // 2
    if len(values) % 2:
        return values[mid]
    return (values[mid - 1] + values[mid]) / 2


def _tfidf(text: str, corpus: list[str]) -> dict[str, float]:
    tokens = _TOKEN.findall(text.lower())
    if not tokens:
        return {}
    counts = Counter(tokens)
    docs = [set(_TOKEN.findall(item.lower())) for item in corpus]
    total = len(docs)
    vector: dict[str, float] = {}
    for token, count in counts.items():
        containing = sum(1 for doc in docs if token in doc)
        idf = math.log((1 + total) / (1 + containing)) + 1.0
        vector[token] = (count / len(tokens)) * idf
    return vector


def _cosine(left: dict[str, float], right: dict[str, float]) -> float:
    if not left or not right:
        return 0.0
    keys = set(left) | set(right)
    dot = sum(left.get(key, 0.0) * right.get(key, 0.0) for key in keys)
    left_n = math.sqrt(sum(value * value for value in left.values()))
    right_n = math.sqrt(sum(value * value for value in right.values()))
    if left_n == 0 or right_n == 0:
        return 0.0
    return dot / (left_n * right_n)


def _short(text: str) -> str:
    text = " ".join(text.split())
    return text if len(text) <= 80 else text[:77] + "..."


def _f(value: Fraction) -> float:
    return round(float(value), 6)


def lint_fcpxml(path: str | Path) -> list[Finding]:
    """Structural checks the DTD does not make: spine tiling, frame grid, refs, lanes.

    Returns hard findings. An empty list means these checks passed.
    """
    import xml.etree.ElementTree as ET

    from .fcpxml import local
    from .timeutil import parse_time

    root = ET.parse(str(path)).getroot()
    findings: list[Finding] = []
    assets = {
        el.get("id")
        for el in root.iter()
        if local(el.tag) == "asset" and el.get("id")
    }
    formats = {el.get("id"): el for el in root.iter() if local(el.tag) == "format"}
    sequence = next((el for el in root.iter() if local(el.tag) == "sequence"), None)
    if sequence is None:
        return [Finding("hard", "structure", "no sequence")]
    fmt = formats.get(sequence.get("format"))
    frame = parse_time(fmt.get("frameDuration") if fmt is not None else None, Fraction(1, 24))
    spine = next((el for el in sequence if local(el.tag) == "spine"), None)
    if spine is None:
        return [Finding("hard", "structure", "no spine")]
    cursor = Fraction(0)
    timed = {"asset-clip", "video", "audio", "clip", "gap", "ref-clip", "sync-clip", "title"}
    for child in list(spine):
        tag = local(child.tag)
        if tag == "transition":
            continue
        if tag not in timed:
            continue
        offset = parse_time(child.get("offset"), Fraction(0))
        duration = parse_time(child.get("duration"), Fraction(0))
        name = child.get("name") or tag
        if offset < cursor:
            findings.append(
                Finding(
                    "hard",
                    "spine_overlap",
                    f"{name} starts at {_f(offset)}s before the previous spine item ends at {_f(cursor)}s",
                    name,
                    _f(offset),
                    _f(cursor),
                )
            )
        cursor = max(cursor, offset + duration)
        _grid(findings, name, frame, offset, duration, child)
        _ref_and_lane(findings, child, assets, spine=True)
        _connected_structure(findings, child, assets, frame)
    return findings


def _grid(findings: list[Finding], name: str, frame: Fraction, offset: Fraction, duration: Fraction, element) -> None:
    from .timeutil import parse_time

    if duration <= 0:
        findings.append(Finding("hard", "time_invalid", f"{name} has no positive duration", name))
    for label, value in (("offset", offset), ("duration", duration)):
        if value % frame != 0:
            findings.append(
                Finding("hard", "time_align", f"{name} {label} {_f(value)}s is off the frame grid", name)
            )
    start = element.get("start")
    if start is not None:
        src = parse_time(start, Fraction(0))
        if src % frame != 0:
            findings.append(
                Finding("hard", "time_align", f"{name} start {_f(src)}s is off the frame grid", name)
            )


def _ref_and_lane(findings: list[Finding], element, assets: set[str], *, spine: bool) -> None:
    from .fcpxml import local

    tag = local(element.tag)
    name = element.get("name") or tag
    if tag == "asset-clip":
        ref = element.get("ref") or ""
        if ref not in assets:
            findings.append(Finding("hard", "asset_ref", f"{name} ref {ref or '(missing)'} does not resolve", name))
    lane = element.get("lane")
    if lane is None:
        return
    try:
        number = int(lane)
    except ValueError:
        findings.append(Finding("hard", "lane", f"{name} lane {lane!r} is not an integer", name))
        return
    if spine and number != 0:
        findings.append(Finding("hard", "lane", f"spine item {name} uses lane {number}", name))
    if not spine and number == 0:
        findings.append(Finding("hard", "lane", f"connected item {name} uses lane 0", name))


def _connected_structure(findings: list[Finding], parent, assets: set[str], frame: Fraction) -> None:
    from .fcpxml import local
    from .timeutil import parse_time

    grouped: dict[int, list[tuple[Fraction, Fraction, str]]] = {}
    for child in list(parent):
        tag = local(child.tag)
        if tag not in {"asset-clip", "video", "audio", "clip", "title", "ref-clip"}:
            continue
        if not child.get("lane"):
            continue
        name = child.get("name") or tag
        offset = parse_time(child.get("offset"), Fraction(0))
        duration = parse_time(child.get("duration"), Fraction(0))
        _grid(findings, name, frame, offset, duration, child)
        _ref_and_lane(findings, child, assets, spine=False)
        try:
            lane = int(child.get("lane") or "0")
        except ValueError:
            continue
        grouped.setdefault(lane, []).append((offset, duration, name))
    for lane, items in grouped.items():
        items.sort()
        cursor = None
        for offset, duration, name in items:
            if cursor is not None and offset < cursor:
                findings.append(
                    Finding(
                        "hard",
                        "lane_overlap",
                        f"{name} overlaps another item on lane {lane}",
                        name,
                        _f(offset),
                        _f(cursor),
                    )
                )
            cursor = offset + duration if cursor is None else max(cursor, offset + duration)
