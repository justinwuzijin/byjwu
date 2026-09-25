"""Style profiles: measured editing parameters under ``styles/<name>/``.

A profile is ``styles/<name>/profile.json`` (format ``jevid.style-profile/1``,
documented in ``styles/README.md``). The taste log's ``style`` block and room
notes may refer to it as ``styles/<name>/profile``; :func:`resolve` accepts
that, a bare name, or a path.

Leaves under ``params`` are objects with ``value``, ``confidence`` in [0, 1]
and a non-empty ``evidence`` string. ``decided_by`` says who settles the value
at build time: ``profile`` (use it as is), ``jev`` (the value is a prior; the
runtime decision is one of the profile's ``jev_questions``, asked through
``conductor/jev.py``), or ``taste_model`` (a creative call, the value is the brief
for the taste model).

This module only loads and validates. It never calls a model.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from cutmcp.jev import MAX_OPTIONS, choice, noul

from .errors import ConductorError

SCHEMA = "jevid.style-profile/1"
STYLES_DIR = Path(__file__).resolve().parent.parent / "styles"
DECIDERS = ("profile", "jev", "taste_model")
PRIMITIVES = ("choice", "noul")
_MISSING = object()


@dataclass(frozen=True)
class Param:
    path: str
    value: Any
    confidence: float
    evidence: str
    unit: str | None
    decided_by: str
    range: tuple[float, float] | None = None


@dataclass(frozen=True)
class StyleProfile:
    name: str
    path: Path
    data: dict

    def params(self) -> list[Param]:
        return [_param(path, leaf) for path, leaf in _leaves(self.data.get("params", {}))]

    def param(self, dotted: str) -> Param:
        node: Any = self.data.get("params", {})
        for part in dotted.split("."):
            if not isinstance(node, dict) or part not in node:
                raise ConductorError(f"style {self.name!r} has no param {dotted!r}")
            node = node[part]
        if not _is_leaf(node):
            raise ConductorError(f"style param {dotted!r} is a group, not a value")
        return _param(dotted, node)

    def value(self, dotted: str, default: Any = _MISSING) -> Any:
        try:
            return self.param(dotted).value
        except ConductorError:
            if default is _MISSING:
                raise
            return default

    def jev_questions(self, runtime_options: dict[str, dict] | None = None) -> dict[str, dict]:
        """Question specs keyed by question key, built with the cutmcp constructors.

        A choice declared with ``options_from`` has its options filled at build
        time; pass them in ``runtime_options`` under the question key. Such a
        question is left out until its options are given.
        """
        runtime_options = runtime_options or {}
        out = {}
        for q in self.data.get("jev_questions", []):
            if q["primitive"] == "noul":
                out[q["key"]] = noul(q["instructions"], q.get("true"), q.get("false"))
                continue
            options = q.get("options") or runtime_options.get(q["key"])
            if options:
                out[q["key"]] = choice(q["instructions"], options, add_none=q.get("add_none", True))
        return out


def resolve(ref: str | Path, styles_dir: Path | None = None) -> Path:
    """Map a name, ``styles/<name>/profile``, or a path to a profile file."""
    base = Path(styles_dir) if styles_dir else STYLES_DIR
    ref_path = Path(ref)
    candidates = []
    if ref_path.suffix == ".json":
        candidates.append(ref_path)
    else:
        if len(ref_path.parts) == 1:
            candidates.append(base / ref_path / "profile.json")
        if ref_path.name == "profile":
            candidates.append(ref_path.with_suffix(".json"))
            if ref_path.parts[0] == "styles":
                candidates.append(base.joinpath(*ref_path.parts[1:]).with_suffix(".json"))
        candidates.append(ref_path / "profile.json")
    for path in candidates:
        if path.is_file():
            return path.resolve()
    raise ConductorError(f"no style profile at {ref!s} (looked in {', '.join(map(str, candidates))})")


def load_profile(ref: str | Path, styles_dir: Path | None = None) -> StyleProfile:
    path = resolve(ref, styles_dir)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ConductorError(f"style profile {path} is not valid JSON: {exc}") from exc
    errors = validate(data)
    if errors:
        raise ConductorError(f"style profile {path} is invalid:\n  " + "\n  ".join(errors))
    return StyleProfile(name=data["name"], path=path, data=data)


def validate(data: Any) -> list[str]:
    """Every problem with a profile, as readable lines. Empty means valid."""
    if not isinstance(data, dict):
        return ["profile must be a JSON object"]
    errors = []
    if data.get("schema") != SCHEMA:
        errors.append(f"schema must be {SCHEMA!r}, got {data.get('schema')!r}")
    if not isinstance(data.get("name"), str) or not data.get("name"):
        errors.append("name must be a non-empty string")
    sources = data.get("sources")
    source_ids = set()
    if not isinstance(sources, list) or not sources:
        errors.append("sources must be a non-empty list")
    else:
        for i, src in enumerate(sources):
            if not isinstance(src, dict) or not src.get("id"):
                errors.append(f"sources[{i}] needs an id")
                continue
            source_ids.add(src["id"])
            weight = src.get("weight")
            if not isinstance(weight, (int, float)) or not 0 <= weight <= 1:
                errors.append(f"sources[{i}].weight must be a number in [0, 1]")
    params = data.get("params")
    if not isinstance(params, dict) or not params:
        errors.append("params must be a non-empty object")
        params = {}
    paths = set()
    for path, node in _walk(params):
        if not _is_leaf(node):
            if not isinstance(node, dict) or not node:
                errors.append(f"params.{path} must be a value leaf or a non-empty group")
            continue
        paths.add(path)
        errors.extend(_leaf_errors(path, node))
    for i, q in enumerate(data.get("jev_questions", [])):
        errors.extend(_question_errors(i, q, paths))
    for i, item in enumerate(data.get("unknowns", [])):
        if not isinstance(item, dict) or not item.get("topic") or not item.get("why"):
            errors.append(f"unknowns[{i}] needs a topic and a why")
    return errors


def _leaf_errors(path: str, leaf: dict) -> list[str]:
    errors = []
    conf = leaf.get("confidence")
    if isinstance(conf, bool) or not isinstance(conf, (int, float)) or not 0 <= conf <= 1:
        errors.append(f"params.{path}.confidence must be a number in [0, 1]")
    if not isinstance(leaf.get("evidence"), str) or not leaf["evidence"].strip():
        errors.append(f"params.{path}.evidence must be a non-empty string")
    if leaf.get("decided_by", "profile") not in DECIDERS:
        errors.append(f"params.{path}.decided_by must be one of {', '.join(DECIDERS)}")
    rng = leaf.get("range")
    if rng is not None:
        if not (isinstance(rng, list) and len(rng) == 2 and all(isinstance(x, (int, float)) for x in rng)
                and rng[0] <= rng[1]):
            errors.append(f"params.{path}.range must be [low, high]")
        elif isinstance(leaf.get("value"), (int, float)) and not rng[0] <= leaf["value"] <= rng[1]:
            errors.append(f"params.{path}.value {leaf['value']} is outside its range {rng}")
    return errors


def _question_errors(i: int, q: Any, paths: set[str]) -> list[str]:
    if not isinstance(q, dict):
        return [f"jev_questions[{i}] must be an object"]
    errors = []
    where = f"jev_questions[{i}] ({q.get('key', '?')})"
    if not q.get("key") or not q.get("instructions"):
        errors.append(f"{where} needs a key and instructions")
    if q.get("primitive") not in PRIMITIVES:
        errors.append(f"{where}.primitive must be one of {', '.join(PRIMITIVES)}")
    if q.get("primitive") == "choice":
        opts = q.get("options")
        if opts is None and isinstance(q.get("options_from"), str) and q["options_from"]:
            pass
        elif not isinstance(opts, dict) or not opts:
            errors.append(f"{where}.options must be a non-empty object of key: description, "
                          "or options_from must name the runtime source")
        elif len(opts) + (1 if q.get("add_none", True) else 0) > MAX_OPTIONS:
            errors.append(f"{where} has more than {MAX_OPTIONS} options")
    feeds = q.get("feeds")
    if feeds is not None and feeds not in paths:
        errors.append(f"{where}.feeds names unknown param {feeds!r}")
    return errors


def _is_leaf(node: Any) -> bool:
    return isinstance(node, dict) and "value" in node


def _walk(node: dict, prefix: str = ""):
    for key, child in node.items():
        path = f"{prefix}.{key}" if prefix else key
        yield path, child
        if isinstance(child, dict) and not _is_leaf(child):
            yield from _walk(child, path)


def _leaves(node: dict):
    for path, child in _walk(node):
        if _is_leaf(child):
            yield path, child


def _param(path: str, leaf: dict) -> Param:
    return Param(path=path, value=leaf["value"], confidence=float(leaf["confidence"]),
                 evidence=leaf["evidence"], unit=leaf.get("unit"),
                 decided_by=leaf.get("decided_by", "profile"),
                 range=tuple(leaf["range"]) if leaf.get("range") else None)
