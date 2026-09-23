"""Stop metrics for an iterate round.

Everything here is read off the timeline and the report. No model calls.
Shot length and cuts per minute are reported every round. They only block
a stop when a bound was set.
"""

from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path

from .fcpxml import parse_fcpxml
from .timeutil import seconds


@dataclass(frozen=True)
class Targets:
    target_seconds: float | None = None
    duration_tolerance: float = 2.0
    max_silence: float = 1.25
    max_escalations: int = 0
    max_reviews: int | None = None
    min_shot: float | None = None
    max_shot: float | None = None
    min_cuts_per_minute: float | None = None
    max_cuts_per_minute: float | None = None

    def failures(self, metrics: dict) -> list[str]:
        missed: list[str] = []
        if self.target_seconds is not None:
            delta = abs(metrics["duration_seconds"] - self.target_seconds)
            if delta > self.duration_tolerance:
                missed.append("duration")
        if metrics["silence_seconds"] > self.max_silence:
            missed.append("silence")
        if metrics["escalations"] > self.max_escalations:
            missed.append("escalations")
        if self.max_reviews is not None and metrics["reviews"] > self.max_reviews:
            missed.append("reviews")
        shot = metrics["avg_shot_seconds"]
        if self.min_shot is not None and shot < self.min_shot:
            missed.append("avg_shot")
        if self.max_shot is not None and shot > self.max_shot:
            missed.append("avg_shot")
        rate = metrics["cuts_per_minute"]
        if self.min_cuts_per_minute is not None and rate < self.min_cuts_per_minute:
            missed.append("cuts_per_minute")
        if self.max_cuts_per_minute is not None and rate > self.max_cuts_per_minute:
            missed.append("cuts_per_minute")
        return missed


def measure(path: str | Path, *, reviews: int, escalations: int) -> dict:
    """Duration, silence, and shot pace for one FCPXML spine."""
    document = parse_fcpxml(path)
    sequence = document.sequences[0]
    silence = Fraction(0)
    shots: list[Fraction] = []
    previous_end: Fraction | None = None
    for clip in sequence.spine:
        if previous_end is not None:
            hole = clip.timeline_start - previous_end
            if hole > 0:
                silence += hole
        previous_end = clip.timeline_end
        if clip.kind == "gap":
            silence += clip.duration
        else:
            shots.append(clip.duration)
    if sequence.duration is not None:
        duration = sequence.duration
    else:
        duration = sum((clip.duration for clip in sequence.spine), Fraction(0))
    duration_seconds = seconds(duration)
    avg = seconds(sum(shots, Fraction(0)) / len(shots)) if shots else 0.0
    minutes = duration_seconds / 60.0 if duration_seconds else 0.0
    cuts = max(0, len(shots) - 1)
    rate = round(cuts / minutes, 3) if minutes else 0.0
    return {
        "duration_seconds": duration_seconds,
        "silence_seconds": seconds(silence),
        "reviews": reviews,
        "escalations": escalations,
        "avg_shot_seconds": avg,
        "cuts_per_minute": rate,
        "shots": len(shots),
    }


def metric_key(metrics: dict) -> tuple:
    """Rounded values used to decide that a round made no progress."""
    return (
        round(float(metrics["duration_seconds"]), 3),
        round(float(metrics["silence_seconds"]), 3),
        int(metrics["reviews"]),
        int(metrics["escalations"]),
        round(float(metrics["avg_shot_seconds"]), 3),
        round(float(metrics["cuts_per_minute"]), 3),
    )
