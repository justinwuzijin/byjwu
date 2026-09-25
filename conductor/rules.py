"""Measured edit rules. A model may veto one. It does not have to bless it.

Each rule compares a number the parser already measured with a named
threshold. Confidence is how far the measurement sits past that threshold,
not a model score. A measurement on the threshold is 0.5. Further past it
climbs toward 0.99.

Thresholds resolve in this order, later wins:

1. ``DEFAULTS`` in this module (placeholder labels until a style profile exists)
2. ``styles/<name>/profile`` when that file is on disk
3. learned priors on the taste file (``rule_thresholds``)
4. per-run overrides

Silence numbers are seeded from Diffusion Studio core's ``removeSilences``
(``@diffusionstudio/core`` 4.0.3, read from the published bundle, not imported).

Their detector (``AudioSource.silences``):

- decode mono, then RMS of each hop of ``hopSize`` samples (default 1024)
  on channel 0: ``sqrt(mean(sample^2))``
- a hop is silent when RMS < ``threshold`` (default 0.02 linear, about -34 dBFS)
- a run is a silence when it lasts at least ``minDuration`` (the code default
  is 500, and it divides by 1000 before scaling by the sample rate, so 500 ms
  = 0.5 s). The type comment calls the same default 0.5 seconds
- ``removeSilences`` keeps ``padding`` seconds (default 0.5) at the start of
  each silence, which is the tail after speech, then drops the rest

``CONDUCTOR_DECISION_MODE`` is ``logic-first`` (default) or ``model-gated``.
``logic-first`` applies a firing rule. The model only sees the proposed cut
and the evidence, and may veto with one of the named reasons.
``model-gated`` is the previous gate: a model score of at least 0.80.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from dataclasses import asdict, dataclass, replace
from pathlib import Path

from .errors import ConductorError

MODES = ("logic-first", "model-gated")
DEFAULT_MODE = "logic-first"

# Placeholder defaults. A style profile replaces any key it sets.
# bare_gap_seconds matches the candidate generator's silence floor.
# max_shot_seconds matches the unverified-hold floor until a profile
# supplies cut_rhythm.talking.max_single_take_s.
DEFAULTS: dict[str, float] = {
    "bare_gap_seconds": 1.25,
    "silence_rms_threshold": 0.02,
    "silence_padding_seconds": 0.5,
    "silence_min_gap_seconds": 0.5,
    "max_pause_seconds": 0.5,
    "flash_frames": 5.0,
    "max_shot_seconds": 45.0,
}

# A reject moves the threshold so the rule fires less often.
# flash_frames fires when the clip is *under* the line, so a reject lowers it.
UNDER_THRESHOLD = frozenset({"flash_frames"})

#: Relative step, and the band around the default a learned value may occupy.
THRESHOLD_STEP = 0.08
THRESHOLD_FLOOR = 0.5
THRESHOLD_CEILING = 2.0

RULE_OF_KIND = {
    "silence_gap": "bare_uncovered_gap",
    "short_clip": "flash_frame",
    "long_static": "untrimmed_hold",
    "source_reuse": "exact_duplicate",
}

THRESHOLD_OF_RULE = {
    "bare_uncovered_gap": "bare_gap_seconds",
    "dead_air": "max_pause_seconds",
    "flash_frame": "flash_frames",
    "untrimmed_hold": "max_shot_seconds",
}

VETO_OPTIONS = {
    "allow": "The measured cut stands. The evidence is a fact, not a taste call.",
    "veto_story": "Cutting this loses a beat the brief still needs.",
    "veto_breath": "This pause is a deliberate breath or a reaction.",
    "veto_reprise": "This repeat is a deliberate reprise.",
    "veto_hold": "This hold is the performance, not dead time.",
}

_STYLES = Path(__file__).resolve().parents[1] / "styles"


@dataclass(frozen=True)
class RuleHit:
    """One rule that cleared its threshold."""

    name: str
    action: str
    confidence: float
    measurement: float
    measurement_label: str
    threshold: float
    threshold_name: str
    evidence: str
    applies: bool = True

    def to_dict(self) -> dict:
        return asdict(self)


def decision_mode(value: str | None = None) -> str:
    """``logic-first`` or ``model-gated``. The env is the default."""
    raw = value if value is not None else os.environ.get("CONDUCTOR_DECISION_MODE", DEFAULT_MODE)
    mode = str(raw or DEFAULT_MODE).strip().lower()
    if mode not in MODES:
        raise ConductorError(
            f"CONDUCTOR_DECISION_MODE must be one of {MODES}, got {raw!r}"
        )
    return mode


def resolve_thresholds(
    *,
    style: str | None = None,
    learned: Mapping[str, float] | None = None,
    overrides: Mapping[str, float] | None = None,
) -> dict[str, float]:
    """defaults < style profile < learned taste priors < per-run overrides."""
    found = dict(DEFAULTS)
    found.update(_profile_thresholds(style or os.environ.get("CONDUCTOR_STYLE") or "byjustinwu"))
    found.update(_numbers(learned or {}))
    found.update(_numbers(overrides or {}))
    return found


def evaluate(candidate, thresholds: Mapping[str, float]) -> RuleHit | None:
    """The first rule that clears its threshold for this candidate, or none."""
    kind = getattr(candidate, "kind", None) or (candidate.get("kind") if isinstance(candidate, Mapping) else None)
    signals = getattr(candidate, "signals", None)
    if signals is None and isinstance(candidate, Mapping):
        signals = candidate.get("signals") or {}
    signals = signals or {}
    duration = _duration(candidate, signals)
    if signals.get("do_not_cut") and kind != "source_reuse":
        return None
    if kind == "silence_gap" and signals.get("bare") and not signals.get("audio"):
        return _over(
            "bare_uncovered_gap",
            "remove",
            _num(signals.get("gap_seconds"), duration),
            "gap_seconds",
            float(thresholds["bare_gap_seconds"]),
            "bare_gap_seconds",
            "bare uncovered gap",
        )
    if _dead_air(kind, signals):
        measured = _num(signals.get("pause_seconds") or signals.get("gap_seconds"), duration)
        hit = _over(
            "dead_air",
            "remove",
            measured,
            "silence_seconds",
            float(thresholds["max_pause_seconds"]),
            "max_pause_seconds",
            (
                "dead air past max pause "
                f"(rms {thresholds['silence_rms_threshold']}, "
                f"padding {thresholds['silence_padding_seconds']}s, "
                f"min gap {thresholds['silence_min_gap_seconds']}s)"
            ),
        )
        # A pause inside a spoken cue is still a breath the dialogue pass owns.
        # Audio silence on the mechanical pass is the cut.
        if hit is not None and kind == "filler_pause":
            return replace(hit, applies=False)
        return hit
    if kind == "short_clip":
        frames = _frames(duration, signals)
        return _under(
            "flash_frame",
            "remove",
            frames,
            "frames",
            float(thresholds["flash_frames"]),
            "flash_frames",
            "flash frame",
        )
    if kind == "source_reuse" and signals.get("identical"):
        return _over(
            "exact_duplicate",
            "remove",
            _num(signals.get("source_duration_seconds"), duration),
            "source_seconds",
            0.0,
            "identical_source_range",
            "exact duplicate source range",
            allow_zero=True,
        )
    if kind == "long_static" and _no_dialogue(signals):
        return _over(
            "untrimmed_hold",
            "tighten",
            duration,
            "hold_seconds",
            float(thresholds["max_shot_seconds"]),
            "max_shot_seconds",
            "untrimmed hold with no dialogue",
        )
    return None


def margin_confidence(measured: float, threshold: float, *, under: bool) -> float:
    """A rule that has cleared its line starts at 0.80 and climbs toward 0.99.

    The 0.80 floor is the existing apply command's ``--min-confidence``. The
    rest of the score is the margin past the threshold, so a barely-over
    measurement and a long bare gap do not look the same.
    """
    scale = threshold if threshold > 0 else 1.0
    margin = (threshold - measured) / scale if under else (measured - threshold) / scale
    if margin <= 0:
        return 0.5
    return round(min(0.99, 0.80 + 0.19 * (margin / (1.0 + margin))), 4)


def nudge_thresholds(thresholds: Mapping[str, float], events: list[dict]) -> tuple[dict[str, float], list[str]]:
    """Move one step per editor accept or reject. Auto accepts do not move it.

    A reject makes the rule fire less often. An accept makes it fire a little
    more often. The value stays inside half to double the built-in default.
    """
    found = dict(thresholds)
    notes: list[str] = []
    for event in events:
        if event.get("source") in {"auto"}:
            continue
        name = event.get("event")
        if name not in {"accept", "reject"}:
            continue
        rule = event.get("rule") or RULE_OF_KIND.get(str(event.get("kind") or ""))
        key = THRESHOLD_OF_RULE.get(str(rule or ""))
        if key is None or key not in found:
            continue
        if rule == "exact_duplicate":
            continue
        current = float(found[key])
        step = current * THRESHOLD_STEP
        reject = name == "reject"
        under = key in UNDER_THRESHOLD
        updated = current + step if reject != under else current - step
        default = float(DEFAULTS[key])
        low = default * THRESHOLD_FLOOR
        high = default * THRESHOLD_CEILING
        updated = min(high, max(low, updated))
        updated = round(updated, 4)
        if abs(updated - current) < 1e-9:
            continue
        found[key] = updated
        direction = "up" if updated > current else "down"
        notes.append(f"{key} nudged {direction} to {updated} after a {name} of {rule}")
    return found, notes


def format_rules(report: Mapping) -> list[str]:
    """Lines for the markdown report and the room summary."""
    mode = report.get("mode") or DEFAULT_MODE
    fired = list(report.get("fired") or [])
    vetoed = list(report.get("vetoed") or [])
    applied = int(report.get("applied") or 0)
    lines = [
        f"Mode: `{mode}`.",
        f"Rules fired: {len(fired)}. Cuts applied: {applied}. Vetoed: {len(vetoed)}.",
    ]
    thresholds = report.get("thresholds") or {}
    if thresholds:
        shown = ", ".join(f"{key}={thresholds[key]}" for key in sorted(thresholds))
        lines.append(f"Thresholds: {shown}.")
    for row in fired:
        lines.append(
            f"- {row['rule']} on {row.get('candidate_id', '')}: "
            f"{row.get('measurement_label', 'measured')} {row.get('measurement')} "
            f"vs {row.get('threshold_name')} {row.get('threshold')} "
            f"({row.get('action')}, confidence {row.get('confidence')})"
        )
    for row in vetoed:
        lines.append(
            f"- vetoed {row.get('rule')} on {row.get('candidate_id', '')}: {row.get('reason')}"
        )
    return lines


def _over(name, action, measured, label, threshold, threshold_name, what, *, allow_zero=False):
    if measured is None:
        return None
    if not allow_zero and not measured > threshold:
        return None
    if allow_zero and measured < 0:
        return None
    confidence = 0.99 if allow_zero and threshold == 0 else margin_confidence(measured, threshold or measured or 1.0, under=False)
    return RuleHit(
        name=name,
        action=action,
        confidence=confidence,
        measurement=round(float(measured), 4),
        measurement_label=label,
        threshold=float(threshold),
        threshold_name=threshold_name,
        evidence=(
            f"{what}: {label} {round(float(measured), 4)} "
            f"> {threshold_name} {threshold}"
        ),
    )


def _under(name, action, measured, label, threshold, threshold_name, what):
    if measured is None or not measured < threshold:
        return None
    return RuleHit(
        name=name,
        action=action,
        confidence=margin_confidence(measured, threshold, under=True),
        measurement=round(float(measured), 4),
        measurement_label=label,
        threshold=float(threshold),
        threshold_name=threshold_name,
        evidence=f"{what}: {label} {round(float(measured), 4)} < {threshold_name} {threshold}",
    )


def _dead_air(kind, signals: Mapping) -> bool:
    if kind == "silence_gap" and signals.get("audio"):
        return True
    if kind == "filler_pause" and signals.get("pause_seconds") is not None and not signals.get("pure_filler"):
        return bool(signals.get("word_timings") or signals.get("pause_seconds") is not None)
    return False


def _no_dialogue(signals: Mapping) -> bool:
    if signals.get("cue_count"):
        return False
    words = signals.get("words_per_second")
    if words is None:
        return True
    return float(words) <= 0.0


def _frames(duration: float, signals: Mapping) -> float | None:
    frame = signals.get("frame_seconds")
    if not frame:
        frame = 1.0 / 24.0
    if duration <= 0:
        return None
    return duration / float(frame)


def _duration(candidate, signals: Mapping) -> float:
    value = getattr(candidate, "duration", None)
    if value is not None:
        return float(value)
    if isinstance(candidate, Mapping):
        raw = candidate.get("duration_seconds")
        if raw is not None:
            return float(raw)
    raw = signals.get("duration_seconds") or signals.get("gap_seconds")
    return float(raw or 0.0)


def _num(value, fallback: float) -> float:
    if value is None:
        return fallback
    return float(value)


def _numbers(raw: Mapping) -> dict[str, float]:
    found: dict[str, float] = {}
    for key, value in raw.items():
        if key not in DEFAULTS:
            continue
        try:
            found[key] = float(value)
        except (TypeError, ValueError):
            continue
    return found


def _profile_thresholds(name: str) -> dict[str, float]:
    """Read ``styles/<name>/profile.json`` when it exists. Missing file is fine."""
    path = _STYLES / name / "profile.json"
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(data, dict):
        return {}
    found: dict[str, float] = {}
    block = data.get("rules")
    if isinstance(block, dict):
        found.update(_numbers(_unwrap(block)))
    params = data.get("params")
    if isinstance(params, dict):
        nested = params.get("rules")
        if isinstance(nested, dict):
            found.update(_numbers(_unwrap(nested)))
        if "max_shot_seconds" not in found:
            take = _dig(params, "cut_rhythm", "talking", "max_single_take_s")
            if take is not None:
                found["max_shot_seconds"] = take
    return found


def _unwrap(block: Mapping) -> dict:
    found = {}
    for key, value in block.items():
        if isinstance(value, Mapping) and "value" in value:
            found[key] = value["value"]
        else:
            found[key] = value
    return found


def _dig(node: Mapping, *keys: str) -> float | None:
    current: object = node
    for key in keys:
        if not isinstance(current, Mapping) or key not in current:
            return None
        current = current[key]
    if isinstance(current, Mapping) and "value" in current:
        current = current["value"]
    try:
        return float(current)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
