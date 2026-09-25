"""Style profiles: measured editing parameters for a named look.

A profile lives at ``styles/<name>/profile.json``. The schema is documented
in ``styles/README.md``. In short, ``params`` is a tree of groups whose leaves
are parameters::

    {"value": 2.75, "unit": "s", "confidence": 0.7,
     "evidence": "montage thr0.3 shot median, 684.9 s labelled (3 videos)",
     "measured": "rhythm.by_section.montage.thr0p3.median"}

Every leaf carries a value, a confidence in [0, 1], and a one-line evidence
note. ``measured`` points into the study's ``measured.json``. ``adjusted``
explains why a value deliberately differs from its measurement. ``decided_by``
says who settles it at runtime: ``profile`` (use the value), ``jev`` (ask the
named runtime question; the value is the prior), or ``editor`` (text only a
person can supply; Jev never writes).

``runtime_questions`` are the judgement calls an assembly engine sends to Jev
through ``conductor.jev.ask``. They are built with ``cutmcp.jev.choice`` and
``noul`` so the option cap and the ``none`` option behave like every other
Conductor question. A profile only describes them; asking is the engine's job.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from cutmcp.jev import MAX_OPTIONS, Answer, choice, noul

from .errors import ConductorError
from .taste import DEFAULT_PREFS, _validate_prefs

SCHEMA = "jevid.style_profile/1"
STYLES_DIR = Path(__file__).resolve().parents[1] / "styles"
DECIDERS = frozenset({"profile", "jev", "editor"})
QUESTION_KINDS = frozenset({"choice", "noul"})
MEASURED_TOLERANCE = 0.05

_PARAM_KEYS = frozenset(
    {"value", "unit", "confidence", "evidence", "measured", "adjusted", "decided_by", "question", "note"}
)
_ID = re.compile(r"^[a-z][a-z0-9_]*$")
_MISSING = object()


@dataclass(frozen=True)
class StyleProfile:
    name: str
    version: str
    data: dict
    path: Path | None = None

    def param(self, dotted: str) -> dict:
        node: Any = self.data["params"]
        for part in dotted.split("."):
            if not isinstance(node, Mapping) or part not in node:
                raise KeyError(f"{self.name} profile has no param {dotted!r}")
            node = node[part]
        if not _is_param(node):
            raise KeyError(f"{dotted!r} is a group, not a param")
        return node

    def value(self, dotted: str, default: Any = _MISSING) -> Any:
        try:
            return self.param(dotted)["value"]
        except KeyError:
            if default is _MISSING:
                raise
            return default

    def params(self) -> Iterator[tuple[str, dict]]:
        yield from _walk(self.data["params"], "")

    def below(self, confidence: float) -> list[str]:
        """Params an engine should treat as soft defaults."""
        return [path for path, p in self.params() if p["confidence"] < confidence]

    def taste_prefs(self) -> dict:
        """The profile's ``conductor_taste`` values as Cut Conductor taste prefs."""
        prefs = dict(DEFAULT_PREFS)
        group = self.data["params"].get("conductor_taste") or {}
        for key in DEFAULT_PREFS:
            if key in group:
                prefs[key] = group[key]["value"]
        return prefs

    def question(self, qid: str) -> dict:
        spec = self._question_spec(qid)
        if spec["kind"] == "choice":
            return choice(spec["instructions"], spec["options"], add_none=spec.get("add_none", True))
        return noul(spec["instructions"], spec.get("true"), spec.get("false"))

    def questions(self, prefix: str = "") -> dict[str, dict]:
        """Every runtime question, keyed ``<prefix><id>`` for one batched ask."""
        return {f"{prefix}{q['id']}": self.question(q["id"]) for q in self.data.get("runtime_questions", [])}

    def prior(self, qid: str) -> Answer:
        """The profile's prior as an Answer: what a dry-run engine falls back to."""
        spec = self._question_spec(qid)
        prior = spec["prior"]
        if spec["kind"] == "noul":
            p = float(prior)
            return Answer(p, max(p, 1.0 - p), {"true": p, "false": round(1.0 - p, 6)})
        probs = {str(k): float(v) for k, v in prior.items()}
        best = max(sorted(probs), key=lambda k: probs[k])
        return Answer(best, probs[best], probs)

    def _question_spec(self, qid: str) -> dict:
        for spec in self.data.get("runtime_questions", []):
            if spec["id"] == qid:
                return spec
        raise KeyError(f"{self.name} profile has no runtime question {qid!r}")


def load_profile(name_or_path: str | Path, styles_dir: str | Path | None = None) -> StyleProfile:
    """Load by style name (``byjustinwu``) or by path to a profile JSON."""
    candidate = Path(name_or_path)
    if candidate.suffix == ".json" or candidate.is_file():
        file = candidate
    else:
        root = Path(styles_dir or os.environ.get("JEVID_STYLES_DIR") or STYLES_DIR)
        file = root / str(name_or_path) / "profile.json"
    if not file.is_file():
        raise ConductorError(f"no style profile at {file}")
    try:
        data = json.loads(file.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ConductorError(f"style profile is not JSON: {file}: {exc}") from exc
    errors = validate_profile(data)
    if errors:
        shown = "\n  ".join(errors[:20])
        more = f"\n  ... and {len(errors) - 20} more" if len(errors) > 20 else ""
        raise ConductorError(f"invalid style profile {file}:\n  {shown}{more}")
    return StyleProfile(data["style"], data["version"], data, file)


def validate_profile(data: Any) -> list[str]:
    """Every problem with a profile, as readable strings. Empty means valid."""
    if not isinstance(data, Mapping):
        return ["profile must be a JSON object"]
    errors: list[str] = []
    if data.get("schema") != SCHEMA:
        errors.append(f"schema must be {SCHEMA!r}, got {data.get('schema')!r}")
    for key in ("style", "version"):
        if not isinstance(data.get(key), str) or not data.get(key):
            errors.append(f"{key} must be a non-empty string")
    sources = data.get("sources")
    if not isinstance(sources, list) or not sources:
        errors.append("sources must be a non-empty list")
    else:
        for i, src in enumerate(sources):
            if not isinstance(src, Mapping) or not src.get("video_id"):
                errors.append(f"sources[{i}] needs a video_id")
            elif not _unit(src.get("weight")):
                errors.append(f"sources[{i}].weight must be a number in [0, 1]")
    params = data.get("params")
    if not isinstance(params, Mapping) or not params:
        errors.append("params must be a non-empty object")
        params = {}
    errors += _check_group(params, "params")
    qids = _check_questions(data.get("runtime_questions", []), params, errors)
    for path, p in _walk(params, ""):
        if p.get("decided_by") == "jev":
            if p.get("question") not in qids:
                errors.append(f"params.{path}: decided_by jev needs a question from runtime_questions")
    if "conductor_taste" in params:
        taste = params["conductor_taste"]
        prefs = dict(DEFAULT_PREFS)
        prefs.update({k: v["value"] for k, v in taste.items() if _is_param(v) and k in DEFAULT_PREFS})
        try:
            _validate_prefs(prefs)
        except ConductorError as exc:
            errors.append(f"params.conductor_taste: {exc}")
    return errors


def measured_mismatches(profile: StyleProfile, measured: Mapping[str, Any]) -> list[str]:
    """Params whose ``measured`` pointer is missing or disagrees with the study."""
    problems = []
    for path, p in profile.params():
        key = p.get("measured")
        if not key:
            continue
        found = _lookup(measured, key)
        if found is _MISSING:
            problems.append(f"{path}: measured key {key!r} not in measured.json")
            continue
        if "adjusted" in p:
            continue
        value = p["value"]
        if _number(value) and _number(found):
            scale = max(1.0, abs(float(found)))
            if abs(float(value) - float(found)) > MEASURED_TOLERANCE * scale:
                problems.append(f"{path}: value {value} but measured {found} (add 'adjusted' if deliberate)")
        elif value != found:
            problems.append(f"{path}: value {value!r} but measured {found!r}")
    return problems


def _check_group(node: Mapping, where: str) -> list[str]:
    errors = []
    for key, child in node.items():
        here = f"{where}.{key}"
        if key.startswith("_"):
            if not isinstance(child, str):
                errors.append(f"{here}: underscore keys are notes and must be strings")
            continue
        if not _ID.match(key):
            errors.append(f"{here}: keys are lower_snake_case")
        if not isinstance(child, Mapping):
            errors.append(f"{here}: bare value; wrap it as {{value, confidence, evidence}}")
        elif _is_param(child):
            errors += _check_param(child, here)
        elif not child:
            errors.append(f"{here}: empty group")
        else:
            errors += _check_group(child, here)
    return errors


def _check_param(p: Mapping, where: str) -> list[str]:
    errors = []
    unknown = set(p) - _PARAM_KEYS
    if unknown:
        errors.append(f"{where}: unknown keys {sorted(unknown)}")
    if not _unit(p.get("confidence")):
        errors.append(f"{where}: confidence must be a number in [0, 1]")
    if not isinstance(p.get("evidence"), str) or not p["evidence"].strip():
        errors.append(f"{where}: evidence must be a non-empty string")
    for key in ("unit", "measured", "adjusted", "note", "question"):
        if key in p and (not isinstance(p[key], str) or not p[key].strip()):
            errors.append(f"{where}: {key} must be a non-empty string")
    decider = p.get("decided_by", "profile")
    if decider not in DECIDERS:
        errors.append(f"{where}: decided_by must be one of {sorted(DECIDERS)}")
    return errors


def _check_questions(raw: Any, params: Mapping, errors: list[str]) -> set[str]:
    if not isinstance(raw, list):
        errors.append("runtime_questions must be a list")
        return set()
    seen: set[str] = set()
    for i, q in enumerate(raw):
        where = f"runtime_questions[{i}]"
        if not isinstance(q, Mapping):
            errors.append(f"{where}: must be an object")
            continue
        qid = q.get("id")
        if not isinstance(qid, str) or not _ID.match(qid):
            errors.append(f"{where}: id must be lower_snake_case")
            continue
        if qid in seen:
            errors.append(f"{where}: duplicate id {qid!r}")
        seen.add(qid)
        kind = q.get("kind")
        if kind not in QUESTION_KINDS:
            errors.append(f"{where}: kind must be choice or noul")
            continue
        if not isinstance(q.get("instructions"), str) or not q["instructions"].strip():
            errors.append(f"{where}: instructions must be a non-empty string")
        if not isinstance(q.get("applies_to"), str) or not q["applies_to"].strip():
            errors.append(f"{where}: applies_to must say what one question is asked about")
        if "sets" in q and _lookup(params, str(q["sets"])) is _MISSING:
            errors.append(f"{where}: sets names no param or group {q['sets']!r}")
        if kind == "choice":
            options = q.get("options")
            if not isinstance(options, Mapping) or len(options) < 2:
                errors.append(f"{where}: choice needs an options object with 2+ entries")
                continue
            if len(options) + (1 if q.get("add_none", True) else 0) > MAX_OPTIONS:
                errors.append(f"{where}: over MAX_OPTIONS={MAX_OPTIONS}")
            prior = q.get("prior")
            if not isinstance(prior, Mapping) or set(prior) - set(options):
                errors.append(f"{where}: prior must map option keys to probabilities")
            elif not all(_unit(v) for v in prior.values()) or abs(sum(prior.values()) - 1.0) > 0.01:
                errors.append(f"{where}: prior probabilities must be in [0, 1] and sum to 1")
        elif not _unit(q.get("prior")):
            errors.append(f"{where}: noul prior must be a probability")
    return seen


def _walk(node: Mapping, prefix: str) -> Iterator[tuple[str, dict]]:
    for key, child in node.items():
        if key.startswith("_") or not isinstance(child, Mapping):
            continue
        path = f"{prefix}.{key}" if prefix else key
        if _is_param(child):
            yield path, child
        else:
            yield from _walk(child, path)


def _lookup(tree: Mapping, dotted: str) -> Any:
    node: Any = tree
    for part in dotted.split("."):
        if isinstance(node, Mapping) and part in node:
            node = node[part]
        elif isinstance(node, list) and part.isdigit() and int(part) < len(node):
            node = node[int(part)]
        else:
            return _MISSING
    return node


def _is_param(node: Any) -> bool:
    return isinstance(node, Mapping) and "value" in node


def _number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _unit(value: Any) -> bool:
    return _number(value) and 0.0 <= float(value) <= 1.0
