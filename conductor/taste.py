"""Editor taste, the feedback log, and per-kind priors.

A taste file is JSON. Missing keys fall back to the defaults below. The log
is events a room appends when a person accepts, rejects, or modifies a
proposal, plus cuts they made that Conductor did not suggest. Nothing here
trains a model. Priors are a bounded shift, recomputed from the log every
load, so the same history always moves confidence the same way.

Schema (version 1). ``rules`` and ``pending`` are optional::

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
      "rules": [
        {"kind": "short_clip", "event": "accept", "action": "remove",
         "when": {"flash": true}, "note": "always cut flash frames",
         "loosen_auto": true}
      ],
      "pending": [],
      "log": [
        {"event": "reject", "candidate_id": "c0004", "action": "tighten",
         "pass": "dialogue", "kind": "filler_pause", "note": "keep the breath"}
      ]
    }

``target_pace`` is ``tight``, ``measured``, or ``loose``.
``cold_open_bias`` is ``keep``, ``neutral``, or ``cut``.
``jump_cut_tolerance`` is 0 to 1. ``hold_seconds`` is how much of a long
hold a ``tighten`` keeps. Extra event fields (a room may add ``at``) are
preserved. Apply adds ``clip_name``, ``kind``, and the timeline range on each
accept so a later round can see what was cut. The loader keeps those fields.

Priors key on ``kind``. Rejections lower confidence and can only raise the
mechanical auto threshold. Accepts may raise confidence, but they cannot open
auto-apply on a call that was under the threshold unless a rule sets
``loosen_auto``. That flag is the explicit opt-in.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from fractions import Fraction
from pathlib import Path

from .errors import ConductorError
from .gates import Gates

# A prior never moves confidence by more than this, in either direction.
MAX_DELTA = 0.20
# Full rejection of a kind raises auto_confidence by at most this much.
AUTO_BUMP = 0.12
# Stay strictly under the configured auto line when a positive prior is capped.
AUTO_EPSILON = 0.001
# A standing rule nudges, then the combined shift is clamped to MAX_DELTA.
RULE_NUDGE = 0.10

DEFAULT_PREFS = {
    "jump_cut_tolerance": 0.5,
    "target_pace": "measured",
    "cold_open_bias": "neutral",
    "hold_seconds": 4.0,
}

_PACES = frozenset({"tight", "measured", "loose"})
_BIASES = frozenset({"keep", "neutral", "cut"})
_EVENTS = frozenset({"accept", "reject", "modify", "extra"})


@dataclass(frozen=True)
class Adjustment:
    """How one candidate's confidence and auto threshold move.

    ``confidence`` is what the gate sees. ``confidence_raw`` is what Jev
    returned. ``reason`` is the sentence the report shows, or ``None`` when
    this kind has no history and no matching rule.
    """

    confidence: float
    confidence_raw: float
    auto_confidence: float
    reason: str | None


@dataclass
class Taste:
    prefs: dict
    gates: Gates
    log: list[dict] = field(default_factory=list)
    path: Path | None = None
    rules: list[dict] = field(default_factory=list)
    pending: list[dict] = field(default_factory=list)
    global_log: list[dict] = field(default_factory=list)
    global_rules: list[dict] = field(default_factory=list)
    supplied_prefs: dict = field(default_factory=dict)
    supplied_gates: dict = field(default_factory=dict)

    def to_state(self) -> dict:
        """The slice of taste that goes into a Jev request."""
        events = self._events()
        return {
            "prefs": dict(self.prefs),
            "feedback": {
                "accepts": _count(events, "accept"),
                "rejects": _count(events, "reject"),
                "modifies": _count(events, "modify"),
                "extras": _count(events, "extra"),
                "recent": [dict(event) for event in events[-20:]],
            },
            "priors": self.priors(),
            "rules": [dict(rule) for rule in self._rules()],
        }

    def hold(self) -> Fraction:
        return Fraction(str(self.prefs.get("hold_seconds", DEFAULT_PREFS["hold_seconds"])))

    def append(self, event: dict) -> bool:
        """Append a project-log event. A repeated fingerprint is skipped."""
        row = dict(event)
        fingerprint = row.get("fingerprint")
        if fingerprint and self._seen(fingerprint):
            return False
        self.log.append(row)
        return True

    def add_rule(self, rule: dict) -> bool:
        row = dict(rule)
        key = _rule_key(row)
        if any(_rule_key(existing) == key for existing in self.rules):
            return False
        self.rules.append(row)
        return True

    def add_pending(self, item: dict) -> bool:
        row = dict(item)
        fingerprint = row.get("fingerprint")
        if fingerprint and (
            self._seen(fingerprint)
            or any(existing.get("fingerprint") == fingerprint for existing in self.pending)
        ):
            return False
        self.pending.append(row)
        return True

    def priors(self) -> dict[str, dict]:
        """Per-kind counts and the auto threshold they imply. Derived, not stored."""
        found: dict[str, dict] = {}
        for kind in _kinds(self._events()):
            found[kind] = _prior_row(self._stats(kind), self.gates.auto_confidence)
        return found

    def adjust(self, kind: str, signals: Mapping, raw: float, gates: Gates) -> Adjustment:
        """Shift one candidate. The configured gate is never loosened without a rule."""
        stats = self._stats(kind)
        matched = [rule for rule in self._rules() if _rule_matches(rule, kind, signals)]
        loosen = any(rule.get("event") == "accept" and rule.get("loosen_auto") for rule in matched)
        block = any(rule.get("event") == "reject" for rule in matched)
        rule_delta = 0.0
        notes: list[str] = []
        for rule in matched:
            direction = 1.0 if rule.get("event") == "accept" else -1.0
            rule_delta += direction * RULE_NUDGE
            if rule.get("note"):
                notes.append(str(rule["note"]))
        log_delta = _delta(stats)
        combined = _clamp(log_delta + rule_delta, -MAX_DELTA, MAX_DELTA)
        base_auto = float(gates.auto_confidence)
        bumped = _auto_threshold(stats, base_auto, loosen=loosen)
        adjusted = _clamp(float(raw) + combined, 0.0, 1.0)
        capped = False
        if block or (adjusted > float(raw) + 1e-9 and not loosen and float(raw) < base_auto):
            ceiling = max(0.0, base_auto - AUTO_EPSILON)
            if adjusted > ceiling:
                adjusted = ceiling
                capped = True
        reason = _reason(
            stats,
            combined,
            auto=bumped,
            base_auto=base_auto,
            capped=capped,
            rule_notes="; ".join(notes),
        )
        if reason is None and abs(combined) < 1e-9 and abs(bumped - base_auto) < 1e-9:
            adjusted = float(raw)
        return Adjustment(adjusted, float(raw), bumped, reason)

    def dump(self) -> dict:
        payload = {
            "version": 1,
            "prefs": self.prefs,
            "gates": self.gates.to_dict(),
            "log": self.log,
        }
        if self.rules:
            payload["rules"] = self.rules
        if self.pending:
            payload["pending"] = self.pending
        return payload

    def _events(self) -> list[dict]:
        seen: set[str] = set()
        rows: list[dict] = []
        for event in [*self.global_log, *self.log]:
            fingerprint = event.get("fingerprint")
            if fingerprint:
                if fingerprint in seen:
                    continue
                seen.add(fingerprint)
            rows.append(event)
        return rows

    def _rules(self) -> list[dict]:
        by_key: dict[str, dict] = {}
        for rule in [*self.global_rules, *self.rules]:
            by_key[_rule_key(rule)] = rule
        return list(by_key.values())

    def _stats(self, kind: str) -> dict[str, int]:
        counts = {"accepts": 0, "rejects": 0, "modifies": 0, "extras": 0}
        names = {
            "accept": "accepts",
            "reject": "rejects",
            "modify": "modifies",
            "extra": "extras",
        }
        for event in self._events():
            slot = names.get(event.get("event"))
            if slot and event.get("kind") == kind:
                counts[slot] += 1
        return counts

    def _seen(self, fingerprint: str) -> bool:
        return any(
            event.get("fingerprint") == fingerprint
            for event in [*self.log, *self.global_log]
        )


def load_taste(
    path: str | Path | None,
    global_path: str | Path | None = None,
) -> Taste:
    """Load a project taste file, then overlay an optional global profile.

    Project prefs, gates, and rules win when both files set the same key.
    Logs are kept apart: priors read both, and :meth:`Taste.dump` writes
    only the project log so a global profile is not copied into the project.
    """
    project = _read_taste(path) if path else _empty_taste()
    if global_path:
        project = _merge_global(project, _read_taste(global_path))
    return project


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
        raise ConductorError(
            f"feedback event must be accept, reject, modify, or extra, got {event!r}"
        )
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


def normalize_rule(item: dict) -> dict:
    """A standing preference. ``loosen_auto`` is honored only on an accept."""
    if not isinstance(item, dict):
        raise ConductorError("a taste rule must be an object")
    kind = item.get("kind")
    if not isinstance(kind, str) or not kind.strip():
        raise ConductorError("a taste rule needs a kind")
    event = item.get("event") or "accept"
    if event not in {"accept", "reject"}:
        raise ConductorError(f"a taste rule event must be accept or reject, got {event!r}")
    when = item.get("when") or {}
    if not isinstance(when, dict):
        raise ConductorError("rule when must be an object")
    rule = {
        "kind": kind.strip(),
        "event": event,
        "loosen_auto": bool(item.get("loosen_auto")) and event == "accept",
    }
    action = item.get("action")
    if isinstance(action, str) and action:
        rule["action"] = action
    if when:
        rule["when"] = dict(when)
    note = item.get("note")
    if isinstance(note, str) and note:
        rule["note"] = note
    return rule


def _empty_taste() -> Taste:
    return Taste(dict(DEFAULT_PREFS), Gates(), [], None)


def _read_taste(path: str | Path) -> Taste:
    file = Path(path)
    if not file.is_file():
        raise ConductorError(f"no such taste file: {file}")
    try:
        data = json.loads(file.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ConductorError(f"taste file is not JSON: {file}: {exc}") from exc
    if not isinstance(data, dict):
        raise ConductorError(f"taste file must be an object: {file}")
    supplied = data.get("prefs") or {}
    if not isinstance(supplied, dict):
        raise ConductorError("taste prefs must be an object")
    prefs = dict(DEFAULT_PREFS)
    prefs.update(supplied)
    _validate_prefs(prefs)
    supplied_gates = data.get("gates") or {}
    if not isinstance(supplied_gates, dict):
        raise ConductorError("taste gates must be an object")
    gates = _gates(supplied_gates)
    log = _object_list(data.get("log") or [], "taste log")
    for event in log:
        if event.get("event") not in _EVENTS:
            raise ConductorError(
                "taste log event must be accept, reject, modify, or extra, "
                f"got {event.get('event')!r}"
            )
    rules = [normalize_rule(item) for item in _object_list(data.get("rules") or [], "taste rules")]
    pending = _object_list(data.get("pending") or [], "taste pending")
    return Taste(
        prefs,
        gates,
        [dict(item) for item in log],
        file,
        rules,
        [dict(item) for item in pending],
        supplied_prefs=dict(supplied),
        supplied_gates=dict(supplied_gates),
    )


def _merge_global(project: Taste, glob: Taste) -> Taste:
    prefs = dict(DEFAULT_PREFS)
    prefs.update(glob.supplied_prefs)
    prefs.update(project.supplied_prefs)
    _validate_prefs(prefs)
    gates = _gates({**glob.supplied_gates, **project.supplied_gates})
    project.prefs = prefs
    project.gates = gates
    project.global_log = [dict(item) for item in glob.log]
    project.global_rules = [dict(item) for item in glob.rules]
    return project


def _object_list(raw, label: str) -> list:
    if not isinstance(raw, list) or not all(isinstance(item, dict) for item in raw):
        raise ConductorError(f"{label} must be a list of objects")
    return raw


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


def _count(events: list[dict], name: str) -> int:
    return sum(1 for event in events if event.get("event") == name)


def _kinds(events: list[dict]) -> list[str]:
    found: list[str] = []
    for event in events:
        kind = event.get("kind")
        if isinstance(kind, str) and kind and kind not in found:
            found.append(kind)
    return found


def _prior_row(stats: dict[str, int], base_auto: float) -> dict:
    decided = stats["accepts"] + stats["rejects"] + stats["modifies"]
    auto = _auto_threshold(stats, base_auto, loosen=False)
    return {
        "accepts": stats["accepts"],
        "rejects": stats["rejects"],
        "modifies": stats["modifies"],
        "extras": stats["extras"],
        "decided": decided,
        "confidence_delta": _delta(stats),
        "auto_confidence": auto,
        "reason": _reason(stats, _delta(stats), auto=auto, base_auto=base_auto, capped=False, rule_notes=""),
    }


def _delta(stats: dict[str, int]) -> float:
    decided = stats["accepts"] + stats["rejects"] + stats["modifies"]
    if decided == 0:
        if stats["extras"]:
            return round(min(MAX_DELTA, 0.05 * stats["extras"]), 4)
        return 0.0
    support = stats["accepts"] + 0.5 * stats["extras"]
    oppose = stats["rejects"] + 0.5 * stats["modifies"]
    total = support + oppose
    if total <= 0:
        return 0.0
    balance = (support - oppose) / total
    return round(_clamp(balance * MAX_DELTA, -MAX_DELTA, MAX_DELTA), 4)


def _auto_threshold(stats: dict[str, int], base_auto: float, *, loosen: bool) -> float:
    """Rejections can only raise the auto line. Accepts never lower it."""
    if loosen:
        return round(float(base_auto), 4)
    decided = stats["accepts"] + stats["rejects"] + stats["modifies"]
    if decided <= 0 or stats["rejects"] <= 0:
        return round(float(base_auto), 4)
    rate = stats["rejects"] / decided
    bumped = min(0.99, float(base_auto) + rate * AUTO_BUMP)
    return round(max(float(base_auto), bumped), 4)


def _reason(
    stats: dict[str, int],
    delta: float,
    *,
    auto: float,
    base_auto: float,
    capped: bool,
    rule_notes: str,
) -> str | None:
    decided = stats["accepts"] + stats["rejects"] + stats["modifies"]
    parts: list[str] = []
    if delta < -1e-9 and decided and stats["rejects"]:
        parts.append(
            f"confidence lowered because you rejected {stats['rejects']}/{decided} similar suggestions"
        )
    elif delta < -1e-9 and decided and stats["modifies"]:
        parts.append(
            f"confidence lowered because you modified {stats['modifies']}/{decided} similar suggestions"
        )
    elif delta > 1e-9 and decided and stats["accepts"]:
        parts.append(
            f"confidence raised because you accepted {stats['accepts']}/{decided} similar suggestions"
        )
    elif delta > 1e-9 and stats["extras"] and not decided:
        noun = "cut" if stats["extras"] == 1 else "cuts"
        parts.append(
            f"confidence raised because you made {stats['extras']} similar {noun} we did not suggest"
        )
    if auto > float(base_auto) + 1e-9 and decided and stats["rejects"]:
        parts.append(
            "auto-apply threshold raised to "
            f"{auto:.2f} because you rejected {stats['rejects']}/{decided} similar suggestions"
        )
    if capped:
        parts.append("auto-apply stays closed without an explicit opt-in")
    if rule_notes and (not parts or abs(delta) > 1e-9 or capped):
        if rule_notes not in " ".join(parts):
            parts.append(rule_notes)
    if not parts and abs(delta) > 1e-9:
        if delta < 0:
            parts.append("confidence lowered from a standing rule")
        else:
            parts.append("confidence raised from a standing rule")
    if not parts:
        return None
    return "; ".join(parts)


def _rule_key(rule: dict) -> str:
    when = rule.get("when") or {}
    action = rule.get("action") or ""
    return json.dumps(
        [rule.get("kind"), rule.get("event"), action, when],
        sort_keys=True,
        separators=(",", ":"),
    )


def _rule_matches(rule: dict, kind: str, signals: Mapping) -> bool:
    if rule.get("kind") != kind:
        return False
    when = rule.get("when") or {}
    for key, expected in when.items():
        if signals.get(key) != expected:
            return False
    return True


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))
