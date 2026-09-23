"""Editor taste and the accept/reject log.

A taste file is JSON. Missing keys fall back to the defaults below. The log
is a list of events a room appends when a person accepts or rejects a
proposal. Jev's state includes the prefs and a short summary of the log.
Nothing here trains a model.

Schema (version 1)::

    {
      "version": 1,
      "prefs": {
        "jump_cut_tolerance": 0.5,
        "target_pace": "measured",
        "cold_open_bias": "neutral",
        "hold_seconds": 4.0
      },
      "gates": {
        "auto_confidence": 0.80,
        "review_confidence": 0.55,
        "auto_risk_max": 0.35
      },
      "log": [
        {"event": "reject", "candidate_id": "c0004", "action": "tighten",
         "pass": "dialogue", "note": "keep the breath"}
      ]
    }

``target_pace`` is ``tight``, ``measured``, or ``loose``.
``cold_open_bias`` is ``keep``, ``neutral``, or ``cut``.
``jump_cut_tolerance`` is 0 to 1. ``hold_seconds`` is how much of a long
hold a ``tighten`` keeps. Extra event fields (a room may add ``at``) are
preserved. Apply adds ``clip_name``, ``kind``, and the timeline range on each
accept so a later round can see what was cut. The loader keeps those fields.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from fractions import Fraction
from pathlib import Path

from .errors import ConductorError
from .gates import Gates

DEFAULT_PREFS = {
    "jump_cut_tolerance": 0.5,
    "target_pace": "measured",
    "cold_open_bias": "neutral",
    "hold_seconds": 4.0,
}

_PACES = frozenset({"tight", "measured", "loose"})
_BIASES = frozenset({"keep", "neutral", "cut"})
_EVENTS = frozenset({"accept", "reject"})


@dataclass
class Taste:
    prefs: dict
    gates: Gates
    log: list[dict] = field(default_factory=list)
    path: Path | None = None

    def to_state(self) -> dict:
        """The slice of taste that goes into a Jev request."""
        return {
            "prefs": dict(self.prefs),
            "feedback": {
                "accepts": sum(1 for event in self.log if event.get("event") == "accept"),
                "rejects": sum(1 for event in self.log if event.get("event") == "reject"),
                "recent": [dict(event) for event in self.log[-20:]],
            },
        }

    def hold(self) -> Fraction:
        return Fraction(str(self.prefs.get("hold_seconds", DEFAULT_PREFS["hold_seconds"])))

    def append(self, event: dict) -> None:
        self.log.append(dict(event))

    def dump(self) -> dict:
        return {
            "version": 1,
            "prefs": self.prefs,
            "gates": self.gates.to_dict(),
            "log": self.log,
        }


def load_taste(path: str | Path | None) -> Taste:
    if path is None:
        return Taste(dict(DEFAULT_PREFS), Gates(), [], None)
    file = Path(path)
    if not file.is_file():
        raise ConductorError(f"no such taste file: {file}")
    try:
        data = json.loads(file.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ConductorError(f"taste file is not JSON: {file}: {exc}") from exc
    if not isinstance(data, dict):
        raise ConductorError(f"taste file must be an object: {file}")
    prefs = dict(DEFAULT_PREFS)
    supplied = data.get("prefs") or {}
    if not isinstance(supplied, dict):
        raise ConductorError("taste prefs must be an object")
    prefs.update(supplied)
    _validate_prefs(prefs)
    gates = _gates(data.get("gates") or {})
    log = data.get("log") or []
    if not isinstance(log, list) or not all(isinstance(item, dict) for item in log):
        raise ConductorError("taste log must be a list of objects")
    for event in log:
        if event.get("event") not in _EVENTS:
            raise ConductorError(
                f"taste log event must be accept or reject, got {event.get('event')!r}"
            )
    return Taste(prefs, gates, [dict(item) for item in log], file)


def feedback_event(
    *,
    event: str,
    candidate_id: str,
    action: str,
    pass_name: str,
    note: str = "",
    fields: dict | None = None,
) -> dict:
    if event not in _EVENTS:
        raise ConductorError(f"feedback event must be accept or reject, got {event!r}")
    if not candidate_id:
        raise ConductorError("feedback needs a candidate id")
    row = {
        "event": event,
        "candidate_id": candidate_id,
        "action": action,
        "pass": pass_name,
    }
    if note:
        row["note"] = note
    for key, value in (fields or {}).items():
        if key not in row and value is not None:
            row[key] = value
    return row


def write_taste(taste: Taste, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(taste.dump(), indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _validate_prefs(prefs: dict) -> None:
    pace = prefs.get("target_pace")
    if pace not in _PACES:
        raise ConductorError(f"target_pace must be one of {sorted(_PACES)}, got {pace!r}")
    bias = prefs.get("cold_open_bias")
    if bias not in _BIASES:
        raise ConductorError(f"cold_open_bias must be one of {sorted(_BIASES)}, got {bias!r}")
    try:
        tolerance = float(prefs.get("jump_cut_tolerance"))
        hold = float(prefs.get("hold_seconds"))
    except (TypeError, ValueError) as exc:
        raise ConductorError("jump_cut_tolerance and hold_seconds must be numbers") from exc
    if not 0.0 <= tolerance <= 1.0:
        raise ConductorError("jump_cut_tolerance must be between 0 and 1")
    if hold < 0:
        raise ConductorError("hold_seconds must be >= 0")


def _gates(raw: dict) -> Gates:
    if not isinstance(raw, dict):
        raise ConductorError("taste gates must be an object")
    base = Gates()
    values = {
        "auto_confidence": raw.get("auto_confidence", base.auto_confidence),
        "review_confidence": raw.get("review_confidence", base.review_confidence),
        "auto_risk_max": raw.get("auto_risk_max", base.auto_risk_max),
    }
    try:
        gates = Gates(
            auto_confidence=float(values["auto_confidence"]),
            review_confidence=float(values["review_confidence"]),
            auto_risk_max=float(values["auto_risk_max"]),
        )
    except (TypeError, ValueError) as exc:
        raise ConductorError("gate thresholds must be numbers") from exc
    for name, value in gates.to_dict().items():
        if not 0.0 <= value <= 1.0:
            raise ConductorError(f"{name} must be between 0 and 1")
    if gates.auto_confidence < gates.review_confidence:
        raise ConductorError("auto_confidence must be >= review_confidence")
    return gates
