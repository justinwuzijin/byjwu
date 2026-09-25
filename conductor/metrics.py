"""Stop metrics for ``iterate``. All of these are counts the timeline already has.

Silence is the sum of structural ``silence_gap`` candidates: bare primary
gaps, the uncovered stretches of a gap that also holds connected clips, and
timeline holes of at least 1.25s. A stretch that sits under a connected clip
is not silence. It is not a decoded quiet measurement. Dead air measured from
a media file is its own ``silence_gap`` candidate and is not added here. Shot
length and cuts per minute come from the spine: a cut is the join between two
non-gap clips.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from fractions import Fraction

from .candidates import generate
from .fcpxml import Sequence as Timeline
from .timeutil import seconds


@dataclass(frozen=True)
class Targets:
    """Configured stop rules. An unset field is not a constraint.

    The round is clear only when at least one field is set and every set
    field passes. Duration uses a symmetric window: ``target ± tolerance``.
    """

    target_seconds: float | None = None
    tolerance: float = 1.0
    max_escalate: int | None = None
    max_review: int | None = None
    max_silence_seconds: float | None = None
    min_shot_seconds: float | None = None
    max_cuts_per_minute: float | None = None

    def configured(self) -> list[str]:
        names: list[str] = []
        if self.target_seconds is not None:
            names.append("duration")
        if self.max_escalate is not None:
            names.append("escalate")
        if self.max_review is not None:
            names.append("review")
        if self.max_silence_seconds is not None:
            names.append("silence")
        if self.min_shot_seconds is not None:
            names.append("shot_length")
        if self.max_cuts_per_minute is not None:
            names.append("cuts_per_minute")
        return names

    def failures(self, metrics: dict) -> list[str]:
        bad: list[str] = []
        if self.target_seconds is not None:
            delta = abs(float(metrics["duration_seconds"]) - float(self.target_seconds))
            if delta > float(self.tolerance) + 1e-9:
                bad.append("duration")
        if self.max_escalate is not None and int(metrics["escalate_count"]) > self.max_escalate:
            bad.append("escalate")
        if self.max_review is not None and int(metrics["review_count"]) > self.max_review:
            bad.append("review")
        if (
            self.max_silence_seconds is not None
            and float(metrics["silence_seconds"]) > float(self.max_silence_seconds) + 1e-9
        ):
            bad.append("silence")
        if (
            self.min_shot_seconds is not None
            and float(metrics["average_shot_seconds"]) + 1e-9 < float(self.min_shot_seconds)
        ):
            bad.append("shot_length")
        if (
            self.max_cuts_per_minute is not None
            and float(metrics["cuts_per_minute"]) > float(self.max_cuts_per_minute) + 1e-9
        ):
            bad.append("cuts_per_minute")
        return bad

    def clear(self, metrics: dict) -> bool:
        names = self.configured()
        return bool(names) and not self.failures(metrics)

    def to_dict(self) -> dict:
        return {
            "target_seconds": self.target_seconds,
            "tolerance": self.tolerance,
            "max_escalate": self.max_escalate,
            "max_review": self.max_review,
            "max_silence_seconds": self.max_silence_seconds,
            "min_shot_seconds": self.min_shot_seconds,
            "max_cuts_per_minute": self.max_cuts_per_minute,
        }


def section_pacing(sequence: Timeline, *, target_seconds: float = 300.0) -> list[dict]:
    """Average shot length and cuts per minute in a handful of stretches.

    A timeline under three minutes is one stretch. Longer timelines split
    into sections of about five minutes, and never more than eight.
    """
    duration = _duration(sequence)
    if duration <= 0:
        return []
    if float(duration) < 180:
        count = 1
    else:
        count = max(1, min(8, int(round(float(duration) / target_seconds))))
    edges = [duration * index / count for index in range(count + 1)]
    shots = [clip for clip in sequence.spine if clip.kind != "gap" and clip.duration > 0]
    sections = []
    for index in range(count):
        start, end = edges[index], edges[index + 1]
        group = []
        for clip in shots:
            mid = clip.timeline_start + clip.duration / 2
            if start <= mid < end or (index == count - 1 and mid == end):
                group.append(clip)
        span = end - start
        if group:
            average = sum((clip.duration for clip in group), Fraction(0)) / len(group)
            if len(group) > 1 and span > 0:
                cuts_per_minute = (len(group) - 1) / (float(span) / 60.0)
            else:
                cuts_per_minute = 0.0
        else:
            average = Fraction(0)
            cuts_per_minute = 0.0
        sections.append(
            {
                "start_seconds": seconds(start),
                "end_seconds": seconds(end),
                "shot_count": len(group),
                "average_shot_seconds": seconds(average) if group else 0.0,
                "cuts_per_minute": round(cuts_per_minute, 4),
            }
        )
    return sections


def measure(sequences: Sequence[Timeline], proposals=None, changes=None) -> dict:
    """Spine numbers plus review and escalate counts.

    Pass ``proposals`` (objects with ``disposition``) or ``changes`` (report
    rows with ``section``). Omit both and the judgment counts are zero.
    """
    total = sum((_duration(sequence) for sequence in sequences), Fraction(0))
    silence = Fraction(0)
    shots = []
    for sequence in sequences:
        for candidate in generate(sequence, [], transcript_present=False):
            if candidate.kind == "silence_gap":
                silence += candidate.duration
        for clip in sequence.spine:
            if clip.kind != "gap" and clip.duration > 0:
                shots.append(clip)
    if shots:
        average = seconds(sum((clip.duration for clip in shots), Fraction(0)) / len(shots))
    else:
        average = 0.0
    duration = seconds(total)
    if len(shots) > 1 and duration > 0:
        cuts_per_minute = (len(shots) - 1) / (duration / 60.0)
    else:
        cuts_per_minute = 0.0
    review, escalate = _judgments(proposals, changes)
    return {
        "duration_seconds": duration,
        "silence_seconds": seconds(silence),
        "average_shot_seconds": average,
        "cuts_per_minute": round(cuts_per_minute, 4),
        "shot_count": len(shots),
        "review_count": review,
        "escalate_count": escalate,
    }


def _duration(sequence: Timeline) -> Fraction:
    if sequence.duration is not None:
        return sequence.duration
    return sum((clip.duration for clip in sequence.spine), Fraction(0))


def _judgments(proposals, changes) -> tuple[int, int]:
    if proposals is not None:
        review = sum(1 for item in proposals if item.disposition == "review")
        escalate = sum(1 for item in proposals if item.disposition == "escalate")
        return review, escalate
    if changes is not None:
        review = sum(1 for row in changes if row.get("section") == "review")
        escalate = sum(1 for row in changes if row.get("section") == "escalate")
        return review, escalate
    return 0, 0
