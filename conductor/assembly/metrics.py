"""Style metrics, read back from an FCPXML file.

These run on the written XML rather than the in-memory timeline, so the same
numbers can be taken on an assembled v0, on a later round, or on a cut the editor
re-exported from Final Cut (the taste-learning input).

Sections come from ``chapter-marker`` notes (``jevid.section=<kind>``). A
file without them is one ``talking`` section. Visible shots are spine clips
plus lane-1+ cutaways. Beats come from decoding the music assets the file
references (the same detector the engine uses), so "on beat" is checked
against the music actually laid, not against the plan.
"""

from __future__ import annotations

import bisect
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from fractions import Fraction
from pathlib import Path
from urllib.parse import unquote, urlparse

from ..fcpxml import local
from ..style import CUT_SECTIONS, StyleProfile
from ..timeutil import parse_time
from .beats import detect_beats


@dataclass
class Placed:
    tag: str
    element: ET.Element
    lane: int
    start: Fraction
    end: Fraction
    local_start: Fraction
    parent: Placed | None = None

    def local_to_timeline(self, t: Fraction) -> Fraction:
        return self.start + (t - self.local_start)


@dataclass
class Reading:
    duration: Fraction
    frame: Fraction
    spine: list[Placed] = field(default_factory=list)
    connected: list[Placed] = field(default_factory=list)
    sections: list[tuple[str, Fraction, Fraction]] = field(default_factory=list)
    assets: dict[str, str] = field(default_factory=dict)
    marked_sections: bool = False


def read(path: str | Path) -> Reading:
    return read_root(ET.parse(str(path)).getroot())


def read_root(root: ET.Element) -> Reading:
    formats = {el.get("id"): el for el in root.iter() if local(el.tag) == "format"}
    assets: dict[str, str] = {}
    for el in root.iter():
        if local(el.tag) == "asset":
            rep = next((c for c in el if local(c.tag) == "media-rep"), None)
            if rep is not None and rep.get("src"):
                assets[el.get("id") or ""] = rep.get("src") or ""
    sequence = next(el for el in root.iter() if local(el.tag) == "sequence")
    fmt = formats.get(sequence.get("format"))
    frame = parse_time(fmt.get("frameDuration"), Fraction(1, 24)) if fmt is not None else Fraction(1, 24)
    spine_el = next(el for el in sequence if local(el.tag) == "spine")
    reading = Reading(parse_time(sequence.get("duration"), Fraction(0)), frame, assets=assets)
    chapters: list[tuple[Fraction, str]] = []
    for child in spine_el:
        tag = local(child.tag)
        if tag in {"transition"}:
            continue
        start = parse_time(child.get("offset"), Fraction(0))
        duration = parse_time(child.get("duration"), Fraction(0))
        local_start = parse_time(child.get("start"), Fraction(0))
        placed = Placed(tag, child, 0, start, start + duration, local_start)
        reading.spine.append(placed)
        _walk(child, placed, reading, chapters)
    if not reading.duration and reading.spine:
        reading.duration = reading.spine[-1].end
    chapters.sort()
    for index, (at, kind) in enumerate(chapters):
        end = chapters[index + 1][0] if index + 1 < len(chapters) else reading.duration
        reading.sections.append((kind, at, end))
    if chapters:
        reading.marked_sections = True
    if not reading.sections:
        reading.sections.append(("talking", Fraction(0), reading.duration))
    return reading


def _walk(element: ET.Element, placed: Placed, reading: Reading, chapters: list[tuple[Fraction, str]]) -> None:
    for child in element:
        tag = local(child.tag)
        if tag == "chapter-marker":
            note = child.get("note") or ""
            kind = next((part.split("=", 1)[1] for part in note.split(";") if part.strip().startswith("jevid.section=")), None)
            if kind:
                at = placed.local_to_timeline(parse_time(child.get("start"), Fraction(0)))
                chapters.append((at, kind.strip()))
        elif tag in {"asset-clip", "video", "title", "audio", "clip", "ref-clip", "sync-clip"} and child.get("lane"):
            offset = parse_time(child.get("offset"), Fraction(0))
            start = placed.local_to_timeline(offset)
            duration = parse_time(child.get("duration"), Fraction(0))
            node = Placed(tag, child, int(child.get("lane") or 0), start, start + duration, parse_time(child.get("start"), Fraction(0)), placed)
            reading.connected.append(node)
            _walk(child, node, reading, chapters)


def measure(path: str | Path, profile: StyleProfile, *, expect_subtitles: bool | None = None, beats: list[Fraction] | None = None) -> dict:
    return measure_root(ET.parse(str(path)).getroot(), profile, expect_subtitles=expect_subtitles, beats=beats)


def measure_root(root: ET.Element, profile: StyleProfile, *, expect_subtitles: bool | None = None, beats: list[Fraction] | None = None) -> dict:
    reading = read_root(root)
    frame = reading.frame
    music = [p for p in reading.connected if p.tag == "asset-clip" and p.element.get("audioRole", "").startswith(str(profile.get("music.role")))]
    if beats is None:
        beats = _beats_from_music(music, reading, profile)
    cuts_by_section: dict[str, list[Fraction]] = {}
    sections_out: list[dict] = []
    cutaways = [p for p in reading.connected if p.tag == "asset-clip" and p.lane > 0]
    for kind, start, end in reading.sections:
        spine_items = [p for p in reading.spine if start <= p.start < end and p.tag != "gap"]
        joins = sorted({p.start for p in spine_items if p.start > start})
        edges = sorted({t for c in cutaways for t in (c.start, c.end) if start < t < end})
        boundaries = sorted(set(joins) | set(edges))
        shots = len(boundaries) + 1 if spine_items else 0
        duration = end - start
        asl = float(duration) / shots if shots else 0.0
        cuts = [p.start for p in spine_items if p.start > start] + [c.start for c in cutaways if start < c.start < end]
        cuts_by_section.setdefault(kind, []).extend(cuts)
        sections_out.append(
            {
                "kind": kind,
                "start_seconds": round(float(start), 3),
                "end_seconds": round(float(end), 3),
                "duration_seconds": round(float(duration), 3),
                "shots": shots,
                "asl_seconds": round(asl, 3),
            }
        )
    asl_by_kind: dict[str, float] = {}
    for kind in CUT_SECTIONS:
        rows = [row for row in sections_out if row["kind"] == kind and row["shots"]]
        if rows:
            asl_by_kind[kind] = round(sum(r["duration_seconds"] for r in rows) / sum(r["shots"] for r in rows), 3)
    tolerance = max(frame * 3 / 2, Fraction(1, 25))
    on_beat_total = on_beat_hit = 0
    for kind, cuts in cuts_by_section.items():
        if kind not in CUT_SECTIONS or profile.pacing(kind)["cut_on_beat"] != "always" or not beats:
            continue
        for cut in cuts:
            on_beat_total += 1
            index = bisect.bisect_left(beats, cut)
            near = [beats[i] for i in (index - 1, index) if 0 <= i < len(beats)]
            if near and min(abs(b - cut) for b in near) <= tolerance:
                on_beat_hit += 1
    fades = _fades(music, profile, frame)
    ducking = _ducking(music, reading, profile)
    subtitles = [p for p in reading.connected if p.tag == "title" and _is_subtitle(p, profile)]
    titles = [p for p in reading.connected if p.tag == "title"]
    dialogue = _dialogue(reading)
    coverage = _coverage(dialogue, [(s.start, s.end) for s in subtitles])
    if expect_subtitles is None:
        expect_subtitles = bool(subtitles)
    family = str(profile.get("typography.family"))
    fonts = [_font(t) for t in titles]
    font_ok = sum(1 for f in fonts if f and f.startswith(family))
    bg_sections = [(s, e) for kind, s, e in reading.sections if profile.background_on(kind)]
    stills = [(p.start, p.end) for p in reading.connected if p.tag == "video"]
    bg_total = sum((e - s for s, e in bg_sections), Fraction(0))
    bg_cover = _coverage(bg_sections, stills) if bg_total > 0 else None
    return {
        "duration_seconds": round(float(reading.duration), 3),
        "sections": sections_out,
        "section_markers": reading.marked_sections,
        "asl_by_section": asl_by_kind,
        "on_beat": {
            "checked": on_beat_total,
            "hit": on_beat_hit,
            "ratio": round(on_beat_hit / on_beat_total, 4) if on_beat_total else None,
            "beats_known": bool(beats),
        },
        "music": {"clips": len(music), **fades},
        "ducking": ducking,
        "subtitles": {
            "count": len(subtitles),
            "expected": bool(expect_subtitles),
            "coverage": round(coverage, 4) if dialogue and expect_subtitles else None,
        },
        "fonts": {
            "titles": len(titles),
            "family": family,
            "compliance": round(font_ok / len(fonts), 4) if fonts else None,
        },
        "background": {
            "stills": len(stills),
            "coverage": round(bg_cover, 4) if bg_cover is not None else None,
        },
    }


@dataclass(frozen=True)
class StyleTargets:
    profile: StyleProfile
    target_seconds: float | None = None

    def failures(self, metrics: dict) -> list[str]:
        bad: list[str] = []
        profile = self.profile
        tolerance = float(profile.get("pacing.tolerance"))
        for kind, measured in metrics["asl_by_section"].items():
            target = float(profile.pacing(kind)["asl_seconds"])
            if measured > 0 and abs(measured - target) > tolerance * target:
                bad.append(f"asl:{kind}")
        if self.target_seconds is not None:
            window = float(profile.get("structure.tolerance_seconds"))
            if abs(metrics["duration_seconds"] - self.target_seconds) > window:
                bad.append("duration")
        ratio = metrics["on_beat"]["ratio"]
        if ratio is not None and ratio + 1e-9 < float(profile.get("metrics.on_beat_min")):
            bad.append("on_beat")
        if metrics["music"]["clips"] and not metrics["music"]["compliant"]:
            bad.append("music_fades")
        if metrics["ducking"]["checked"] and metrics["ducking"]["compliant"] < metrics["ducking"]["checked"]:
            bad.append("ducking")
        coverage = metrics["subtitles"]["coverage"]
        if coverage is not None and coverage + 1e-9 < float(profile.get("metrics.subtitle_coverage_min")):
            bad.append("subtitle_coverage")
        compliance = metrics["fonts"]["compliance"]
        if compliance is not None and compliance + 1e-9 < float(profile.get("metrics.font_compliance_min")):
            bad.append("font")
        bg = metrics["background"]["coverage"]
        if bg is not None and bg + 1e-9 < float(profile.get("metrics.background_coverage_min")):
            bad.append("background")
        return bad

    def to_dict(self) -> dict:
        profile = self.profile
        return {
            "asl_seconds": {kind: profile.pacing(kind)["asl_seconds"] for kind in CUT_SECTIONS},
            "asl_tolerance": profile.get("pacing.tolerance"),
            "target_seconds": self.target_seconds,
            "duration_tolerance_seconds": profile.get("structure.tolerance_seconds"),
            **{key: profile.get(f"metrics.{key}") for key in profile.get("metrics")},
        }


def _beats_from_music(music: list[Placed], reading: Reading, profile: StyleProfile) -> list[Fraction]:
    beats: list[Fraction] = []
    cache: dict[str, list[float]] = {}
    for clip in music:
        src = reading.assets.get(clip.element.get("ref") or "", "")
        if not src.startswith("file://"):
            continue
        file = Path(unquote(urlparse(src).path))
        if not file.is_file():
            continue
        if src not in cache:
            grid = detect_beats(
                file,
                duration=10**6,
                min_bpm=float(profile.get("music.beats.min_bpm")),
                max_bpm=float(profile.get("music.beats.max_bpm")),
                fallback_bpm=profile.get("music.beats.fallback_bpm"),
            )
            cache[src] = grid.beats if grid else []
        for value in cache[src]:
            t = clip.local_to_timeline(Fraction(str(value)))
            if clip.start <= t < clip.end:
                beats.append(Fraction(round(t / reading.frame)) * reading.frame)
    return sorted(beats)


def _keys(clip: Placed) -> list[tuple[Fraction, float]]:
    out: list[tuple[Fraction, float]] = []
    for el in clip.element.iter():
        if local(el.tag) == "keyframe" and el.get("value", "").endswith("dB"):
            out.append((clip.local_to_timeline(parse_time(el.get("time"))), float(el.get("value")[:-2])))
    return sorted(out)


def _ramp_up(keys: list[tuple[Fraction, float]], floor: float) -> float | None:
    """Seconds of the rise off the floor. A hold at the floor is a gap, not a fade."""
    audible = next((i for i, (_, db) in enumerate(keys) if db > floor + 1), None)
    if audible is None:
        return None
    if audible == 0:
        return 0.0
    return float(keys[audible][0] - keys[audible - 1][0])


def _ramp_down(keys: list[tuple[Fraction, float]], floor: float) -> float | None:
    """Seconds of the drop back to the floor. A trailing hold at the floor is a gap."""
    audible = [i for i, (_, db) in enumerate(keys) if db > floor + 1]
    if not audible:
        return None
    last = audible[-1]
    if last == len(keys) - 1:
        return 0.0
    return float(keys[last + 1][0] - keys[last][0])


def _fades(music: list[Placed], profile: StyleProfile, frame: Fraction) -> dict:
    ordered = sorted(music, key=lambda p: p.start)
    floor = float(profile.get("music.floor_db"))
    tol = float(profile.get("metrics.fade_tolerance_seconds")) + float(frame)
    rows: list[dict] = []
    transition = str(profile.get("music.song_change.transition"))
    change = {
        "crossfade": float(profile.get("music.song_change.crossfade_seconds")),
        "cut": 2 * float(frame),
        "fade_through": None,
    }[transition]
    for index, clip in enumerate(ordered):
        keys = _keys(clip)
        length = float(clip.end - clip.start)
        want_in = float(profile.get("music.fade_in.seconds")) if index == 0 or change is None else change
        want_out = float(profile.get("music.fade_out.seconds")) if index == len(ordered) - 1 or change is None else change
        want_in, want_out = min(want_in, length / 2), min(want_out, length / 2)
        got_in = _ramp_up(keys, floor)
        got_out = _ramp_down(keys, floor)
        silent = got_in is None and got_out is None
        ok = silent or (
            got_in is not None
            and got_out is not None
            and abs(got_in - want_in) <= tol
            and abs(got_out - want_out) <= tol
        )
        rows.append(
            {
                "clip": clip.element.get("name"),
                "fade_in": [round(want_in, 3), None if got_in is None else round(got_in, 3)],
                "fade_out": [round(want_out, 3), None if got_out is None else round(got_out, 3)],
                "ok": ok,
            }
        )
    return {"fades": rows, "compliant": all(row["ok"] for row in rows)}


def _ducking(music: list[Placed], reading: Reading, profile: StyleProfile) -> dict:
    depth = float(profile.get("music.duck.depth_db"))
    tol = float(profile.get("metrics.duck_tolerance_db"))
    checked = compliant = 0
    worst: float | None = None
    depths: list[float] = []
    for a, b in _dialogue(reading):
        mid = (a + b) / 2
        kind = next((k for k, s, e in reading.sections if s <= mid < e), "talking")
        spec = profile.music_section(kind)
        if not spec["duck"] or not profile.get("music.duck.enabled"):
            continue
        for clip in music:
            if not clip.start <= mid < clip.end:
                continue
            keys = _keys(clip)
            if not keys:
                continue
            level = _interp(keys, mid)
            limit = float(spec["bed_db"]) + depth + tol
            checked += 1
            if level <= limit:
                compliant += 1
            worst = level - limit if worst is None else max(worst, level - limit)
            depths.append(level - float(spec["bed_db"]))
    depth = round(sorted(depths)[len(depths) // 2], 2) if depths else None
    return {
        "checked": checked,
        "compliant": compliant,
        "worst_over_db": None if worst is None else round(worst, 2),
        "depth_db": depth,
    }


def _dialogue(reading: Reading) -> list[tuple[Fraction, Fraction]]:
    spans = []
    for p in reading.spine:
        if p.tag != "asset-clip" or p.element.get("audioRole") != "dialogue":
            continue
        audio_start = parse_time(p.element.get("audioStart"), p.local_start)
        audio_duration = parse_time(p.element.get("audioDuration"), p.end - p.start)
        begin = p.local_to_timeline(audio_start)
        spans.append((begin, begin + audio_duration))
    return sorted(spans)


def _coverage(spans: list[tuple[Fraction, Fraction]], covers: list[tuple[Fraction, Fraction]]) -> float:
    total = sum((b - a for a, b in spans), Fraction(0))
    if total <= 0:
        return 0.0
    merged: list[tuple[Fraction, Fraction]] = []
    for a, b in sorted(covers):
        if merged and a <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], b))
        else:
            merged.append((a, b))
    hit = Fraction(0)
    for a, b in spans:
        for c, d in merged:
            overlap = min(b, d) - max(a, c)
            if overlap > 0:
                hit += overlap
    return float(hit / total)


def _interp(keys: list[tuple[Fraction, float]], t: Fraction) -> float:
    if t <= keys[0][0]:
        return keys[0][1]
    for (t0, v0), (t1, v1) in zip(keys, keys[1:]):
        if t0 <= t <= t1:
            if t1 == t0:
                return v1
            return v0 + (v1 - v0) * float((t - t0) / (t1 - t0))
    return keys[-1][1]


def _is_subtitle(title: Placed, profile: StyleProfile) -> bool:
    name = title.element.get("name") or ""
    if name.startswith("Subtitle"):
        return True
    return title.lane == int(profile.get("typography.subtitle.lane")) and _font(title) == profile.get("typography.subtitle.font")


def _font(title: Placed) -> str | None:
    for el in title.element.iter():
        if local(el.tag) == "text-style" and el.get("font"):
            return el.get("font")
    return None
