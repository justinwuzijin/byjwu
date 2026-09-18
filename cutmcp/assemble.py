"""Tier 3 — deterministic assembly. Never calls a model.

Takes scores in, puts a timeline out, and does it the same way every time.
The same scores always produce the same timeline byte for byte, which is what
makes undo, diffable edits and reproducing a cut six months later possible.
Any judgment that wants to live here belongs in tier 2, arriving as a number
on a `Decision`.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

from .decide import CUT_LEVELS, Decision
from .extract import Media, Segment, cache_dir

__all__ = [
    "Clip",
    "Cut",
    "Timeline",
    "select",
    "build",
    "review_queue",
    "export",
    "load_timeline",
]

_MAX_CUT_LEVEL = len(CUT_LEVELS) - 1
_CONTEXT_CHARS = 140


@dataclass
class Clip:
    """One contiguous run of kept segments, spanning first.start → last.end."""

    idx: int
    start: float
    end: float
    duration: float
    first_segment: int
    last_segment: int
    speakers: list[str]
    text: str


@dataclass
class Cut:
    """A boundary at the end of a clip, and how good it is likely to feel."""

    idx: int
    at: float
    source_time: float
    segment: int
    cut_quality: str
    confidence: float
    trailing_text: str
    next_text: str


@dataclass
class Timeline:
    timeline_id: str
    media_id: str
    media_path: str
    brief: str
    target: float
    quantum: float
    cut_penalty: float
    duration: float
    clips: list[Clip] = field(default_factory=list)
    cuts: list[Cut] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["clips"] = [asdict(c) for c in self.clips]
        d["cuts"] = [asdict(c) for c in self.cuts]
        return d

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> Timeline:
        return cls(
            **{k: d[k] for k in cls.__dataclass_fields__ if k not in ("clips", "cuts")},
            clips=[Clip(**c) for c in d["clips"]],
            cuts=[Cut(**c) for c in d["cuts"]],
        )

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2, sort_keys=True)

    def save(self) -> Path:
        p = cache_dir() / f"{self.timeline_id}.timeline.json"
        p.write_text(self.to_json())
        return p


def load_timeline(timeline_id: str) -> Timeline:
    p = cache_dir() / f"{timeline_id}.timeline.json"
    if not p.exists():
        raise FileNotFoundError(f"no timeline {timeline_id!r}; run cut first")
    return Timeline.from_dict(json.loads(p.read_text()))


# --------------------------------------------------------------------------
# selection
# --------------------------------------------------------------------------


def select(
    segs: Sequence[Segment],
    decs: Sequence[Decision],
    target: float,
    quantum: float = 0.25,
    cut_penalty: float = 0.6,
) -> list[int]:
    """Choose the segment indices that maximize value within `target` seconds.

    A two-state knapsack over the ordered transcript, vectorized across the
    budget axis. State is whether the previous segment was kept, which is
    what lets the DP price every keep-state flip against how clean that cut
    would feel — a cheap cut between mediocre lines can beat an ugly one
    between good ones.

    Segment cost rounds *up* and includes `gap_before`, because a kept run
    spans first.start → last.end and the pauses inside it are material you
    pay for. Overshooting a delivery target is a real failure; undershooting
    by a quantum is not.
    """
    n = len(segs)
    if n == 0 or target <= 0:
        return []
    by_idx = {d.idx: d for d in decs}
    missing = [s.idx for s in segs if s.idx not in by_idx]
    if missing:
        raise KeyError(f"no Decision for segment(s) {missing[:5]}")

    value = np.array([by_idx[s.idx].value for s in segs], dtype=np.float64)
    cutq = np.array([by_idx[s.idx].cutq for s in segs], dtype=np.float64)
    span = np.array([s.duration + s.gap_before for s in segs], dtype=np.float64)

    # floor, not round: a budget axis that rounds up would let the optimum
    # overrun the target by most of a quantum.
    budget = int(np.floor(round(target / quantum, 9)))
    B = budget + 1
    cost = np.maximum(1, np.ceil(np.round(span / quantum, 9))).astype(np.int64)
    flip = cut_penalty * (1.0 - cutq / _MAX_CUT_LEVEL)

    NEG = -np.inf
    dp = np.full((2, B), NEG, dtype=np.float64)
    dp[0, 0] = 0.0
    back = np.zeros((n, 2, B), dtype=np.int8)

    for i in range(n):
        f = flip[i - 1] if i > 0 else 0.0
        drop_from_kept = dp[1] - f
        new0 = np.maximum(dp[0], drop_from_kept)
        back[i, 0] = drop_from_kept > dp[0]  # strict: a tie keeps the flip out

        keep_from_kept = dp[1]
        keep_from_dropped = dp[0] - f
        gain = np.maximum(keep_from_kept, keep_from_dropped) + value[i]
        src = keep_from_kept >= keep_from_dropped  # tie continues the run

        new1 = np.full(B, NEG, dtype=np.float64)
        c = int(cost[i])
        if c < B:
            new1[c:] = gain[: B - c]
            back[i, 1, c:] = src[: B - c]
        dp = np.stack((new0, new1))

    # first max in flatten order: state 0 before state 1, shortest budget
    # first. Deterministic, and prefers the cut with fewer clips on a tie.
    flat = int(np.argmax(dp))
    s, b = divmod(flat, B)
    if not np.isfinite(dp[s, b]):
        return []

    kept: list[int] = []
    for i in range(n - 1, -1, -1):
        prev = int(back[i, s, b])
        if s == 1:
            kept.append(i)
            b -= int(cost[i])
        s = prev
    kept.reverse()
    return [segs[i].idx for i in kept]


# --------------------------------------------------------------------------
# building
# --------------------------------------------------------------------------


def build(
    media: Media,
    decs: Sequence[Decision],
    target: float,
    brief: str = "",
    quantum: float = 0.25,
    cut_penalty: float = 0.6,
) -> Timeline:
    """Select, group into clips, and record every cut with its quality."""
    kept = select(media.segments, decs, target, quantum, cut_penalty)
    by_idx = {s.idx: s for s in media.segments}
    dec_by_idx = {d.idx: d for d in decs}

    runs: list[list[int]] = []
    for idx in kept:
        if runs and idx == runs[-1][-1] + 1:
            runs[-1].append(idx)
        else:
            runs.append([idx])

    clips: list[Clip] = []
    cuts: list[Cut] = []
    playhead = 0.0
    for n, run in enumerate(runs):
        first, last = by_idx[run[0]], by_idx[run[-1]]
        dur = last.end - first.start
        clips.append(
            Clip(
                idx=n,
                start=first.start,
                end=last.end,
                duration=dur,
                first_segment=first.idx,
                last_segment=last.idx,
                speakers=sorted({by_idx[i].speaker for i in run}),
                text=" ".join(by_idx[i].text for i in run),
            )
        )
        playhead += dur
        nxt = runs[n + 1][0] if n + 1 < len(runs) else None
        d = dec_by_idx[last.idx]
        cuts.append(
            Cut(
                idx=n,
                at=playhead,
                source_time=last.end,
                segment=last.idx,
                cut_quality=d.cut_quality,
                confidence=d.cutq_conf,
                trailing_text=_tail(last.text),
                next_text=_head(by_idx[nxt].text) if nxt is not None else "",
            )
        )

    duration = sum(c.duration for c in clips)
    tl = Timeline(
        timeline_id=_timeline_id(media.media_id, brief, target, quantum, cut_penalty, kept),
        media_id=media.media_id,
        media_path=media.path,
        brief=brief,
        target=target,
        quantum=quantum,
        cut_penalty=cut_penalty,
        duration=duration,
        clips=clips,
        cuts=cuts,
    )
    tl.save()
    return tl


def _timeline_id(
    media_id: str, brief: str, target: float, quantum: float, cut_penalty: float, kept: Sequence[int]
) -> str:
    """blake2b over everything that determines the cut.

    Never `hash()`: it is salted per process, so an id built in one session
    would not resolve in the next.
    """
    h = hashlib.blake2b(digest_size=10)
    h.update(media_id.encode())
    h.update(b"\x00" + brief.encode())
    h.update(f"|{target:.6f}|{quantum:.6f}|{cut_penalty:.6f}|".encode())
    h.update(",".join(str(i) for i in kept).encode())
    return h.hexdigest()


def _tail(text: str) -> str:
    t = text.strip()
    return t if len(t) <= _CONTEXT_CHARS else "…" + t[-_CONTEXT_CHARS:]


def _head(text: str) -> str:
    t = text.strip()
    return t if len(t) <= _CONTEXT_CHARS else t[:_CONTEXT_CHARS] + "…"


# --------------------------------------------------------------------------
# review
# --------------------------------------------------------------------------


def review_queue(timeline: Timeline, limit: int = 40) -> list[Cut]:
    """The cuts a human should actually look at, least confident first.

    This is the real deliverable. Ordering is only meaningful if Jev is
    calibrated, which is what `eval/calibrate.py` exists to prove — until it
    has been run against a hand cut, treat this order as unproven.
    """
    ordered = sorted(timeline.cuts, key=lambda c: (c.confidence, c.idx))
    return ordered[: max(0, limit)]


# --------------------------------------------------------------------------
# export
# --------------------------------------------------------------------------


def export(timeline: Timeline, out_dir: str | Path, fps: int = 24) -> dict[str, str]:
    """Write JSON and a CMX3600 EDL, plus `.otio` when the library is there."""
    d = Path(out_dir)
    d.mkdir(parents=True, exist_ok=True)
    stem = f"cutmcp-{timeline.timeline_id}"
    written: dict[str, str] = {}

    j = d / f"{stem}.json"
    j.write_text(timeline.to_json())
    written["json"] = str(j)

    e = d / f"{stem}.edl"
    e.write_text(_edl(timeline, fps))
    written["edl"] = str(e)

    otio_path = _write_otio(timeline, d / f"{stem}.otio", fps)
    if otio_path:
        written["otio"] = otio_path
    return written


def _tc(seconds: float, fps: int) -> str:
    """Seconds → `HH:MM:SS:FF`, non-drop frame."""
    frames = int(round(max(0.0, seconds) * fps))
    ff = frames % fps
    total_s = frames // fps
    return f"{total_s // 3600:02d}:{total_s // 60 % 60:02d}:{total_s % 60:02d}:{ff:02d}"


def _untc(tc: str, fps: int) -> float:
    """`HH:MM:SS:FF` → seconds. Inverse of `_tc` to within half a frame."""
    hh, mm, ss, ff = (int(x) for x in tc.split(":"))
    return hh * 3600.0 + mm * 60.0 + ss + ff / fps


def _edl(timeline: Timeline, fps: int) -> str:
    name = Path(timeline.media_path).name or "SOURCE"
    lines = [
        f"TITLE: CUTMCP {timeline.timeline_id}",
        "FCM: NON-DROP FRAME",
        "",
    ]
    rec = 0  # accumulate in frames so record and source durations never drift
    for c in timeline.clips:
        src_in = int(round(c.start * fps))
        src_out = int(round(c.end * fps))
        length = max(1, src_out - src_in)
        lines.append(
            f"{c.idx + 1:03d}  AX       AA/V  C        "
            f"{_tc(src_in / fps, fps)} {_tc((src_in + length) / fps, fps)} "
            f"{_tc(rec / fps, fps)} {_tc((rec + length) / fps, fps)}"
        )
        lines.append(f"* FROM CLIP NAME: {name}")
        lines.append(f"* SEGMENTS {c.first_segment}-{c.last_segment}")
        lines.append("")
        rec += length
    return "\n".join(lines)


def _write_otio(timeline: Timeline, path: Path, fps: int) -> str | None:
    try:
        import opentimelineio as otio
    except ImportError:
        return None
    tl = otio.schema.Timeline(name=f"cutmcp {timeline.timeline_id}")
    track = otio.schema.Track(name="A-roll", kind=otio.schema.TrackKind.Video)
    tl.tracks.append(track)
    for c in timeline.clips:
        track.append(
            otio.schema.Clip(
                name=f"clip{c.idx + 1:03d}",
                media_reference=otio.schema.ExternalReference(
                    target_url=Path(timeline.media_path).as_uri()
                ),
                source_range=otio.opentime.TimeRange(
                    start_time=otio.opentime.RationalTime(round(c.start * fps), fps),
                    duration=otio.opentime.RationalTime(
                        max(1, round(c.end * fps) - round(c.start * fps)), fps
                    ),
                ),
            )
        )
    otio.adapters.write_to_file(tl, str(path))
    return str(path)
