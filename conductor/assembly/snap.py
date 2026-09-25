"""Snap visual cuts to music keypoints. Dialogue cuts stay where they are.

Keypoints come from an existing :class:`~conductor.assembly.beats.BeatGrid`:
beats, downbeats, and onset peaks. Detection itself stays in ``beats.py``.

Score is ``weight[type] * intensity``, then divided by the loudest keypoint
in the same music section (a run of bars, or the whole track). Defaults:
downbeat 1.0, energy change 0.8, onset 0.4, beat 0.55.

Ideas only, reimplemented here: CutClaw keypoints / AV-harmony (no licence;
the source was not read), Cardboard percussion beat-sync, Mosaic montage
BPM bands. No code copied.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..router import Ask, Router
from .beats import BeatGrid

#: Drop a weaker keypoint when a stronger one sits inside this gap.
DEDUPE_SECONDS = 0.08
#: Cutaway window when the caller does not pass frames. Montage uses its own.
DEFAULT_WINDOW = 0.25
MONTAGE_WINDOW = 0.25
WEIGHTS = {
    "downbeat": 1.0,
    "energy": 0.8,
    "beat": 0.55,
    "onset": 0.4,
}
_TYPE_RANK = {"downbeat": 3, "energy": 2, "beat": 1, "onset": 0}


@dataclass(frozen=True)
class Keypoint:
    time: float
    type: str
    intensity: float
    score: float

    def to_state(self) -> dict:
        return {
            "time": round(self.time, 4),
            "type": self.type,
            "intensity": round(self.intensity, 4),
            "score": round(self.score, 4),
        }


@dataclass(frozen=True)
class WordSpan:
    start: float
    end: float


@dataclass(frozen=True)
class OffsetChoice:
    """One music in-point the taste model may keep or veto."""

    source_start: float
    score: float
    reason: str

    @property
    def key(self) -> str:
        return f"{self.source_start:.4f}"


def keypoints_from_grid(
    grid: BeatGrid | None,
    *,
    onset_strength: list[float] | None = None,
    bars_per_section: int = 4,
) -> list[Keypoint]:
    """``(time, type, intensity)`` rows, de-duplicated and section-normalised.

    ``None`` and an empty grid return ``[]``. Callers treat that as a no-op.
    """
    if grid is None or not grid.beats:
        return []
    down = {round(t, 4) for t in grid.downbeats}
    raw: list[tuple[float, str, float]] = []
    for beat in grid.beats:
        kind = "downbeat" if round(beat, 4) in down or _near(beat, grid.downbeats) else "beat"
        raw.append((float(beat), kind, 1.0))
    strengths = list(onset_strength or [])
    for index, onset in enumerate(grid.onsets):
        intensity = float(strengths[index]) if index < len(strengths) else 1.0
        raw.append((float(onset), "onset", max(0.0, min(1.0, intensity))))
    for moment, intensity in _energy_changes(grid):
        raw.append((moment, "energy", intensity))
    merged = _dedupe(raw)
    return _normalise(merged, grid, bars_per_section)


def snap_time(
    t: float,
    keypoints: list[Keypoint],
    *,
    window: float = DEFAULT_WINDOW,
    lo: float | None = None,
    hi: float | None = None,
    words: list[WordSpan] | None = None,
    accept=None,
) -> float:
    """Move ``t`` to the highest-scoring keypoint inside ``±window``.

    No keypoint in range leaves ``t`` unchanged. A candidate that lands
    inside a spoken word, or whose move crosses one, is skipped.
    """
    if not keypoints or window <= 0:
        return t
    ranked = sorted(
        (point for point in keypoints if abs(point.time - t) <= window + 1e-9),
        key=lambda point: (-point.score, abs(point.time - t), point.time),
    )
    for point in ranked:
        if lo is not None and point.time < lo - 1e-9:
            continue
        if hi is not None and point.time > hi + 1e-9:
            continue
        if _crosses_word(t, point.time, words or []):
            continue
        if accept is not None and not accept(point.time):
            continue
        return point.time
    return t


def snap_window(*, frames: int = 6, frame_seconds: float = 1 / 24, montage: bool = False) -> float:
    """Six frames for cutaways. Montage uses a quarter-second window."""
    if montage:
        return MONTAGE_WINDOW
    return max(frames * frame_seconds, 1e-4)


@dataclass(frozen=True)
class VisualEdit:
    """One visual-only boundary. ``kind`` is ``cutaway`` or ``montage``."""

    id: str
    time: float
    kind: str
    lo: float | None = None
    hi: float | None = None
    dialogue: bool = False


def snap_visual_edits(
    edits: list[VisualEdit],
    keypoints: list[Keypoint],
    *,
    window: float = DEFAULT_WINDOW,
    montage_window: float = MONTAGE_WINDOW,
    words: list[WordSpan] | None = None,
) -> list[VisualEdit]:
    """Snap cutaway and montage times. Dialogue rows are copied unchanged."""
    snapped: list[VisualEdit] = []
    for edit in edits:
        if edit.dialogue or edit.kind == "dialogue":
            snapped.append(edit)
            continue
        if not keypoints:
            snapped.append(edit)
            continue
        limit = montage_window if edit.kind == "montage" else window
        moved = snap_time(edit.time, keypoints, window=limit, lo=edit.lo, hi=edit.hi, words=words)
        snapped.append(
            VisualEdit(edit.id, moved, edit.kind, edit.lo, edit.hi, dialogue=False)
        )
    return snapped


def dialogue_times_unchanged(before: list[VisualEdit], after: list[VisualEdit]) -> bool:
    """True when every dialogue boundary has the same time after the snap pass."""
    prior = {edit.id: edit.time for edit in before if edit.dialogue or edit.kind == "dialogue"}
    later = {edit.id: edit.time for edit in after if edit.dialogue or edit.kind == "dialogue"}
    return prior == later


def montage_durations(
    length: float,
    keypoints: list[Keypoint],
    *,
    start: float = 0.0,
    energy: float = 0.5,
    min_shot: float = 0.2,
) -> list[float]:
    """Shot lengths inside one music section. They sum to ``length``.

    Higher ``energy`` (0–1, onset density or RMS) keeps fewer beats per shot.
    With no keypoints the section is one shot of ``length``.
    """
    if length <= 0:
        return []
    if not keypoints:
        return [length]
    inside = [point for point in keypoints if start + min_shot <= point.time <= start + length + 1e-9]
    beats_per_shot = 1 if energy >= 0.66 else 2 if energy >= 0.33 else 4
    cuts: list[float] = []
    held = 0
    cursor = start
    for point in inside:
        if point.time - cursor < min_shot - 1e-9:
            continue
        if point.time > start + length - min_shot + 1e-9:
            break
        held += 1
        if held >= beats_per_shot:
            cuts.append(point.time)
            cursor = point.time
            held = 0
    edges = [start, *cuts, start + length]
    durations = [round(edges[i + 1] - edges[i], 6) for i in range(len(edges) - 1)]
    durations = [value for value in durations if value > 1e-9]
    if not durations:
        return [length]
    durations[-1] = round(length - sum(durations[:-1]), 6)
    if durations[-1] <= 1e-9 and len(durations) > 1:
        durations[-2] = round(durations[-2] + durations[-1], 6)
        durations.pop()
    return durations


def section_energy(grid: BeatGrid | None, start: float, end: float) -> float:
    """Onset density in ``[start, end]`` relative to the rest of the grid, in 0–1."""
    if grid is None or end <= start:
        return 0.5
    span = end - start
    local = sum(1 for onset in grid.onsets if start <= onset < end) / span
    if not grid.onsets:
        local = sum(1 for beat in grid.beats if start <= beat < end) / span
        whole = (len(grid.beats) / max(grid.beats[-1], 1e-6)) if grid.beats else local
    else:
        whole = len(grid.onsets) / max(grid.beats[-1] if grid.beats else span, 1e-6)
    if whole <= 0:
        return 0.5
    return max(0.0, min(1.0, local / (2 * whole)))


def music_offset_candidates(
    grid: BeatGrid | None,
    story_times: list[float],
    *,
    limit: int = 3,
) -> list[OffsetChoice]:
    """Top in-points, best alignment first. Empty without a grid."""
    if grid is None or not grid.downbeats:
        return []
    anchors = [0.0, *grid.downbeats]
    seen: set[float] = set()
    choices: list[OffsetChoice] = []
    for offset in anchors:
        key = round(offset, 4)
        if key in seen or offset < 0:
            continue
        seen.add(key)
        score, reason = _alignment(grid, story_times, offset)
        choices.append(OffsetChoice(offset, score, reason))
    choices.sort(key=lambda item: (-item.score, item.source_start))
    return choices[:limit]


def choose_music_offset(
    grid: BeatGrid | None,
    story_times: list[float],
    *,
    router: Router | None = None,
    brief: str = "",
) -> OffsetChoice | None:
    """Pick an offered in-point. The model may only veto among the top three.

    A value that is not one of those offsets is ignored and the logical best
    is kept. No router keeps the best without a call.
    """
    offered = music_offset_candidates(grid, story_times)
    if not offered:
        return None
    best = offered[0]
    if router is None or len(offered) == 1:
        return best
    options = {item.key: item.reason for item in offered}
    by_key = {item.key: item for item in offered}

    def rule(_ask: Ask) -> tuple[str, float]:
        return best.key, best.score

    ask = Ask(
        id="music_start_offset",
        type="music_offset",
        subject={
            "offered": [item.source_start for item in offered],
            "story_times": story_times,
        },
        options=options,
        question="Which of these music in-points should the cut use? Veto only; do not invent a time.",
        rule=rule,
        mock=lambda: (best.key, best.score),
    )
    answered, _receipts = router.decide([ask], brief=brief, context={"stage": "snap"})
    if not answered:
        return best
    chosen = by_key.get(str(answered[0].value))
    return chosen if chosen is not None else best


def av_harmony(cut_times: list[float], keypoints: list[Keypoint], *, tolerance: float = 0.1) -> float | None:
    """Fraction of visual cut times within ``tolerance`` of a keypoint."""
    if not cut_times or not keypoints:
        return None
    hits = 0
    for moment in cut_times:
        if any(abs(point.time - moment) <= tolerance + 1e-9 for point in keypoints):
            hits += 1
    return hits / len(cut_times)


def _near(moment: float, others: list[float], gap: float = 0.02) -> bool:
    return any(abs(moment - other) <= gap for other in others)


def _dedupe(raw: list[tuple[float, str, float]]) -> list[tuple[float, str, float, float]]:
    ordered = sorted(raw, key=lambda row: (row[0], -_TYPE_RANK.get(row[1], 0)))
    kept: list[tuple[float, str, float, float]] = []
    for time, kind, intensity in ordered:
        score = WEIGHTS.get(kind, 0.4) * intensity
        if kept and time - kept[-1][0] <= DEDUPE_SECONDS:
            if score > kept[-1][3] or (
                score == kept[-1][3] and _TYPE_RANK.get(kind, 0) > _TYPE_RANK.get(kept[-1][1], 0)
            ):
                kept[-1] = (time, kind, intensity, score)
            continue
        kept.append((time, kind, intensity, score))
    return kept


def _normalise(
    rows: list[tuple[float, str, float, float]],
    grid: BeatGrid,
    bars_per_section: int,
) -> list[Keypoint]:
    bounds = _sections(grid, bars_per_section)
    section_peak = [0.0 for _ in bounds]
    owners = []
    for time, _kind, _intensity, score in rows:
        owner = 0
        for index, (left, right) in enumerate(bounds):
            if left - 1e-9 <= time < right:
                owner = index
                break
        owners.append(owner)
        section_peak[owner] = max(section_peak[owner], score)
    points: list[Keypoint] = []
    for (time, kind, intensity, score), owner in zip(rows, owners):
        peak = section_peak[owner] or 1.0
        points.append(Keypoint(round(time, 6), kind, intensity, round(score / peak, 6)))
    return points


def _sections(grid: BeatGrid, bars_per_section: int) -> list[tuple[float, float]]:
    marks = list(grid.downbeats) or list(grid.beats[::4])
    if len(marks) < 2:
        end = grid.beats[-1] + grid.period if grid.beats else 0.0
        return [(0.0, end + 1.0)]
    step = max(1, bars_per_section)
    edges = marks[::step]
    if edges[0] > 0:
        edges = [0.0, *edges]
    end = grid.beats[-1] + grid.period
    if edges[-1] < end:
        edges.append(end)
    return [(edges[i], edges[i + 1]) for i in range(len(edges) - 1)]


def _energy_changes(grid: BeatGrid) -> list[tuple[float, float]]:
    """A keypoint where bar onset-density jumps. Intensity is the relative jump."""
    marks = list(grid.downbeats)
    if len(marks) < 3:
        return []
    densities: list[float] = []
    for left, right in zip(marks, marks[1:]):
        width = max(right - left, 1e-6)
        count = sum(1 for onset in grid.onsets if left <= onset < right)
        if not grid.onsets:
            count = sum(1 for beat in grid.beats if left <= beat < right)
        densities.append(count / width)
    found: list[tuple[float, float]] = []
    for index in range(1, len(densities)):
        previous = densities[index - 1]
        current = densities[index]
        base = max(previous, 1e-6)
        change = abs(current - previous) / base
        if change >= 0.5 and current != previous:
            found.append((marks[index], max(0.0, min(1.0, change / 2))))
    return found


def _crosses_word(origin: float, target: float, words: list[WordSpan]) -> bool:
    if abs(target - origin) <= 1e-9:
        return False
    left, right = (origin, target) if origin < target else (target, origin)
    for word in words:
        if word.start < target < word.end:
            return True
        if left < word.start < right or left < word.end < right:
            return True
    return False


def _alignment(grid: BeatGrid, story_times: list[float], offset: float) -> tuple[float, str]:
    if not story_times:
        score = 1.0 if offset == grid.downbeats[0] or offset == 0.0 else 0.5
        why = "no story times; first downbeat" if offset in (0.0, grid.downbeats[0]) else "later downbeat"
        if offset == 0.0 and grid.downbeats and grid.downbeats[0] != 0.0:
            score = 0.6
            why = "music start, before the first downbeat"
        return score, why
    hits = 0
    for moment in story_times:
        shifted = moment + offset
        if any(abs(shifted - beat) <= 0.15 for beat in grid.beats):
            hits += 1
    score = hits / len(story_times)
    return round(score, 4), f"{hits}/{len(story_times)} story times land on a beat"
