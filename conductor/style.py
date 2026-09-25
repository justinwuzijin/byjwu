"""Style profile: measured channel defaults the cut rules read.

Ideas for the retake and gap fields come from Descript (Remove Retakes,
Shorten Word Gaps), Gling, Selects, ButterCut, auto-editor, and Mosaic.
Reimplemented from public descriptions; no code copied.

The schema is the ``style`` object a profile file may carry. Missing keys
use the defaults below. Times are seconds.
"""

from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction
from typing import Any

from .schema import validate

# Conversational words per second when the channel has not been measured.
DEFAULT_SPEECH_RATE = 2.5


PROFILE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "utterance_pause": {"type": "number", "minimum": 0, "default": 0.35},
        "group_window": {"type": "number", "minimum": 0, "default": 45},
        "similarity": {"type": "number", "minimum": 0, "maximum": 1, "default": 0.6},
        "prefix_tokens": {"type": "integer", "minimum": 1, "default": 3},
        "incomplete_coverage": {"type": "number", "minimum": 0, "maximum": 1, "default": 0.70},
        "cutoff_window": {"type": "number", "minimum": 0, "default": 0.15},
        "cutoff_confidence": {"type": "number", "minimum": 0, "maximum": 1, "default": 0.5},
        "take_score_margin": {"type": "number", "minimum": 0, "default": 0.08},
        "speech_rate_median": {"type": "number", "minimum": 0, "default": DEFAULT_SPEECH_RATE},
        "dead_air_shorten_min": {"type": "number", "minimum": 0, "default": 0.5},
        "dead_air_remove_min": {"type": "number", "minimum": 0, "default": 1.25},
        "gap_target": {"type": "number", "minimum": 0, "default": 0.18},
        "pre_roll": {"type": "number", "minimum": 0, "default": 0.12},
        "post_roll": {"type": "number", "minimum": 0, "default": 0.20},
        "min_cut": {"type": "number", "minimum": 0, "default": 0.20},
        "min_clip": {"type": "number", "minimum": 0, "default": 0.30},
        "filler_silence": {"type": "number", "minimum": 0, "default": 0.08},
        "dissolve_min_removed": {"type": "number", "minimum": 0, "default": 1.0},
        "allow_dissolves": {"type": "boolean", "default": False},
    },
}


def _defaults() -> dict[str, Any]:
    return {
        name: spec["default"]
        for name, spec in PROFILE_SCHEMA["properties"].items()
    }


@dataclass(frozen=True)
class StyleProfile:
    """Thresholds for retake selection and word-boundary hygiene."""

    utterance_pause: Fraction = Fraction("0.35")
    group_window: Fraction = Fraction(45)
    similarity: float = 0.6
    prefix_tokens: int = 3
    incomplete_coverage: float = 0.70
    cutoff_window: Fraction = Fraction("0.15")
    cutoff_confidence: float = 0.5
    take_score_margin: float = 0.08
    speech_rate_median: float = DEFAULT_SPEECH_RATE
    dead_air_shorten_min: Fraction = Fraction("0.5")
    dead_air_remove_min: Fraction = Fraction("1.25")
    gap_target: Fraction = Fraction("0.18")
    pre_roll: Fraction = Fraction("0.12")
    post_roll: Fraction = Fraction("0.20")
    min_cut: Fraction = Fraction("0.20")
    min_clip: Fraction = Fraction("0.30")
    filler_silence: Fraction = Fraction("0.08")
    dissolve_min_removed: Fraction = Fraction(1)
    allow_dissolves: bool = False

    @classmethod
    def from_dict(cls, raw: dict | None = None) -> StyleProfile:
        data = _defaults()
        if raw:
            data.update(validate(dict(raw), PROFILE_SCHEMA))
        return cls(
            utterance_pause=_frac(data["utterance_pause"]),
            group_window=_frac(data["group_window"]),
            similarity=float(data["similarity"]),
            prefix_tokens=int(data["prefix_tokens"]),
            incomplete_coverage=float(data["incomplete_coverage"]),
            cutoff_window=_frac(data["cutoff_window"]),
            cutoff_confidence=float(data["cutoff_confidence"]),
            take_score_margin=float(data["take_score_margin"]),
            speech_rate_median=float(data["speech_rate_median"]),
            dead_air_shorten_min=_frac(data["dead_air_shorten_min"]),
            dead_air_remove_min=_frac(data["dead_air_remove_min"]),
            gap_target=_frac(data["gap_target"]),
            pre_roll=_frac(data["pre_roll"]),
            post_roll=_frac(data["post_roll"]),
            min_cut=_frac(data["min_cut"]),
            min_clip=_frac(data["min_clip"]),
            filler_silence=_frac(data["filler_silence"]),
            dissolve_min_removed=_frac(data["dissolve_min_removed"]),
            allow_dissolves=bool(data["allow_dissolves"]),
        )


def _frac(value: Any) -> Fraction:
    return Fraction(str(value))
