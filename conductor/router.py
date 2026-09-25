"""Decision router. Which engine makes each call, batched, cached, and counted.

The product rule: a decision that is linear and logical — a bounded choice
with clear criteria — goes to **Jev**. An open-ended creative or taste
decision goes to **Claude Opus 5.5**. No Grok or xAI model is anywhere in
this path; :func:`conductor.jev.refuse_xai` rejects one if configured.

Classification (``DECISION_TYPES``):

=================  ======  ==================================================
type               engine  decided question
=================  ======  ==================================================
silence_gap        jev     Is this gap removable dead air?
short_clip         jev     Is this a flash frame, or a real shot?
filler_pause       jev     Is this whole-cue filler or pause safe to tighten?
long_static        jev     Does this hold break the pacing target?
colour_role        jev     Does a clip with no role need a person?
colour_aspect      jev     Does this frame-size mismatch need a fix?
take_keep          jev     Keep or cut this take under the stated rules?
take_compare       jev     Which of two takes is better on objective signals?
cut_gate           jev     Does this cut point meet the cut rules?
pacing_violation   jev     Which of these candidates breaks the pacing target?
subtitle_break     jev     Where does this subtitle line break, by the rules?
audio_check        jev     Is this breath, room tone, or clipped word a fault?
covered_gap        jev     Does this gap under connected clips need a person?
rhythm_shift       jev     Does this jump in average shot length need a look?
rate_mix           jev     Does this run of conformed frame rates need a look?
untrimmed_run      jev     Is this run of whole source clips an unselected string-out?
silent_card        jev     Does this silent generator card need a title or a trim?
colour_unseen      opus    What look, exposure, and skin treatment is needed?
story_structure    opus    What order and shape does the story take?
key_moments        opus    Which moments carry the video?
music              opus    Which track, and where does it sit and breathe?
typography         opus    How is on-screen text set and treated?
visual_treatment   opus    What grade, look, or effect does this shot want?
montage            opus    Which shots make the montage, in what order?
broll_selection    opus    Which coverage plays over this line?
source_reuse       opus    Is this repeat of earlier footage a deliberate reprise?
music_tail         opus    How should the cut end against a music bed that stops early?
=================  ======  ==================================================

Unknown kinds fall back to the pass default (``story`` and ``broll`` are
creative, ``audio`` is linear) or raise. A new decision is a
:func:`register_decision`, not an ``if`` somewhere else.

The engine says who decides. ``gates.route`` still says who may act: the
dialogue pass is judged by Jev, and is still review-only because the pass
is creative. Threshold comparisons inside the gate are arithmetic, not a
decision, so they stay code.

Fallbacks:

- Jev unavailable (no key, HTTP failure, unusable answer): the deterministic
  rules in :func:`conductor.jev.policy`, confidence × ``FALLBACK_DISCOUNT``.
  Never another model. ``decide`` never gives a rules answer ``auto``.
- Opus unavailable: the decision becomes a review marker with no action.

The first failed request marks that engine down for the rest of the run, so
a 40-minute timeline does not retry a dead host once per window.

Batching and caching: Jev gets one request per window of ``JEV_WINDOW``
candidates (two questions each), Opus one per ``OPUS_WINDOW``. Windows also
split at ``MAX_STATE_CHARS``. Answers are cached on the router by content,
not by id or timeline position, so ``iterate`` rounds re-use every call whose
region did not change. The cache key includes the brief, the taste prefs, the
question, the engine, the model, and live-vs-mock. It leaves out the taste
feedback log: inside one run the only log growth is the loop's own accepts,
for regions that no longer exist. A new router starts cold.

Every run gets a :class:`Ledger`: calls per engine (live and mock), items,
cache hits, fallbacks, and tokens and cost when the provider returns them.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Callable, Iterable, Mapping
from dataclasses import asdict, dataclass, field
from typing import Any

from cutmcp.jev import MAX_OPTIONS, USD_PER_M_INPUT_TOKENS, choice, noul

from . import jev, opus
from .errors import ConductorError
from .rules import VETO_OPTIONS, RuleHit, decision_mode as resolve_decision_mode, evaluate
from .schema import SchemaError, example, validate

JEV = "jev"
OPUS = "opus"
ENGINES = (JEV, OPUS)

JEV_WINDOW = 24
OPUS_WINDOW = 12
MAX_STATE_CHARS = 60_000
FALLBACK_DISCOUNT = 0.85
RATIONALE_CHARS = 240
HTTP_TIMEOUT = 120.0

_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
_POSITIONAL = frozenset({"id", "timeline_start_seconds", "timeline_end_seconds"})
_SECRET_ENV = ("OPENROUTER_API_KEY", "TYPESAFE_API_KEY", "ANTHROPIC_API_KEY")
_TOKEN = re.compile(r"(sk-|Bearer\s+)[A-Za-z0-9_\-.]{6,}")

OPUS_SYSTEM = (
    "You are the creative editor on a Final Cut Pro timeline for byjwu. You make "
    "taste decisions only: story shape, which moments carry the piece, music feel "
    "and placement, typography and visual treatment, montage. Mechanical checks "
    "are decided elsewhere. Answer every object in `items` exactly once, by `id`. "
    "`value` must follow the schema and, when `options` are given, be one of them. "
    "`confidence` is your probability, 0 to 1, that a senior editor would agree. "
    "`rationale` is one short sentence an editor will read on a marker. Nothing "
    "you return is applied without a gate or a person; when unsure, prefer the "
    "option that sends it to review."
)

CANDIDATE_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "action": {"type": "string", "enum": list(jev.ACTIONS)},
        "risk": {"type": "number"},
    },
    "required": ["action", "risk"],
    "additionalProperties": False,
}


@dataclass(frozen=True)
class DecisionType:
    name: str
    engine: str
    question: str
    why: str

    def to_dict(self) -> dict:
        return {"engine": self.engine, "question": self.question, "why": self.why}


def _type(name: str, engine: str, question: str, why: str) -> DecisionType:
    return DecisionType(name, engine, question, why)


_LINEAR = "Linear: a bounded choice with clear criteria. "
_CREATIVE = "Creative: open-ended taste with no single right answer. "

DECISION_TYPES: dict[str, DecisionType] = {
    item.name: item
    for item in (
        _type("silence_gap", JEV, "Is this gap removable dead air?",
              _LINEAR + "A measured gap length against the silence threshold and the brief."),
        _type("short_clip", JEV, "Is this a flash frame, or a real shot?",
              _LINEAR + "Clip duration against fixed flash and short-clip thresholds."),
        _type("filler_pause", JEV, "Is this whole-cue filler or pause safe to tighten?",
              _LINEAR + "The whole-cue filler list and the pause length are fixed rules."),
        _type("long_static", JEV, "Does this hold break the pacing target?",
              _LINEAR + "Hold length and words per second against the pace preference."),
        _type("colour_role", JEV, "Does a clip with no role need a person?",
              _LINEAR + "Whether a role attribute is present in the XML."),
        _type("colour_aspect", JEV, "Does this frame-size mismatch need a fix?",
              _LINEAR + "Asset and sequence aspect ratios against a fixed threshold."),
        _type("take_keep", JEV, "Keep or cut this take under the stated rules?",
              _LINEAR + "The keep rules are stated; the take either meets them or not."),
        _type("take_compare", JEV, "Which of two takes is better on objective signals?",
              _LINEAR + "Objective signals (flubs, level, focus, length) pick between two."),
        _type("cut_gate", JEV, "Does this cut point meet the cut rules?",
              _LINEAR + "A cut either clips a word or lands clean; the rules are listed."),
        _type("pacing_violation", JEV, "Which of these candidates breaks the pacing target?",
              _LINEAR + "One pick among N against a numeric pacing target."),
        _type("subtitle_break", JEV, "Where does this subtitle line break, by the rules?",
              _LINEAR + "Line-length and phrase-boundary rules over listed break points."),
        _type("audio_check", JEV, "Is this breath, room tone, or clipped word a fault?",
              _LINEAR + "Audio faults are defined by level and duration rules."),
        _type("covered_gap", JEV, "Does this gap under connected clips need a person?",
              _LINEAR + "Lane coverage over a primary gap is measured from the XML."),
        _type("rhythm_shift", JEV, "Does this jump in average shot length need a look?",
              _LINEAR + "A sliding average shot length against a fixed jump ratio."),
        _type("rate_mix", JEV, "Does this run of conformed frame rates need a look?",
              _LINEAR + "Conform rates against the sequence rate, counted from the XML."),
        _type("untrimmed_run", JEV, "Is this run of whole source clips an unselected string-out?",
              _LINEAR + "Clip in and out points against the asset's own start and duration."),
        _type("silent_card", JEV, "Does this silent generator card need a title or a trim?",
              _LINEAR + "A generator with no audio element and nothing on a lane, by length."),
        _type("colour_unseen", OPUS, "What look, exposure, and skin treatment is needed?",
              _CREATIVE + "Visual treatment is a look, not a rule."),
        _type("story_structure", OPUS, "What order and shape does the story take?",
              _CREATIVE + "Story structure is authorship."),
        _type("key_moments", OPUS, "Which moments carry the video?",
              _CREATIVE + "Which beat lands is a taste call."),
        _type("music", OPUS, "Which track, and where does it sit and breathe?",
              _CREATIVE + "Music choice and placement are feel."),
        _type("typography", OPUS, "How is on-screen text set and treated?",
              _CREATIVE + "Type and on-screen treatment are design."),
        _type("visual_treatment", OPUS, "What grade, look, or effect does this shot want?",
              _CREATIVE + "Grade and effects are a look."),
        _type("montage", OPUS, "Which shots make the montage, in what order?",
              _CREATIVE + "Montage selection and rhythm are taste."),
        _type("broll_selection", OPUS, "Which coverage plays over this line?",
              _CREATIVE + "Which image illustrates a line is an editorial read."),
        _type("source_reuse", OPUS, "Is this repeat of earlier footage a deliberate reprise?",
              _CREATIVE + "Whether a recap earns its repeat is a montage call."),
        _type("music_tail", OPUS, "How should the cut end against a music bed that stops early?",
              _CREATIVE + "Ending on the song or on picture is a music call."),
    )
}

#: Engine for a registered pass whose candidate kinds have no decision type.
PASS_DEFAULTS: dict[str, str] = {"story": OPUS, "broll": OPUS, "audio": JEV}


def register_decision(name: str, *, engine: str, question: str, why: str) -> DecisionType:
    """Install or replace a decision type. Tests that call this should restore it."""
    if engine not in ENGINES:
        raise ConductorError(f"engine must be one of {ENGINES}, got {engine!r}")
    if not question.strip() or not why.strip():
        raise ConductorError("a decision type needs a question and a reason")
    item = DecisionType(name, engine, question.strip(), why.strip())
    DECISION_TYPES[name] = item
    return item


def classify(kind: str, pass_name: str | None = None) -> DecisionType:
    """The decision type (and so the engine) for a candidate kind or ask type."""
    found = DECISION_TYPES.get(kind)
    if found is not None:
        return found
    engine = PASS_DEFAULTS.get(pass_name or "")
    if engine is not None:
        label = "Creative" if engine == OPUS else "Linear"
        return DecisionType(
            kind, engine, f"What should happen to this {kind} region?",
            f"{label}: default for the {pass_name} pass; register_decision to be specific.",
        )
    raise ConductorError(
        f"no decision type for {kind!r}. register_decision({kind!r}, engine='jev'|'opus', ...)"
    )


def routing_table() -> dict[str, dict]:
    return {name: item.to_dict() for name, item in sorted(DECISION_TYPES.items())}


@dataclass
class Ask:
    """One decision for :meth:`Router.decide`.

    Jev asks need ``options`` (key → criteria), at most 250, and no schema:
    Jev only selects. Opus asks take ``options`` or a JSON ``schema`` for the
    value. ``rule`` returns ``(value, confidence)`` from deterministic code;
    it is the Jev dry-run answer and the fallback when Jev is down. ``mock``
    is the Opus dry-run answer, a value or a ``() -> (value, confidence)``.
    """

    id: str
    type: str
    subject: dict
    options: dict[str, str] | None = None
    schema: dict | None = None
    question: str = ""
    rule: Callable[[Ask], tuple[Any, float]] | None = None
    mock: Any = None


@dataclass
class Decision:
    id: str
    type: str
    engine: str
    source: str
    value: Any
    confidence: float
    why: str
    detail: str = ""
    model: str | None = None
    rationale: str = ""
    cached: bool = False

    @property
    def needs_review(self) -> bool:
        return self.source == "unavailable" or self.value is None

    def to_dict(self) -> dict:
        row = asdict(self)
        row["needs_review"] = self.needs_review
        return row


@dataclass
class Verdict:
    """A candidate's action, confidence, and risk, with who decided it."""

    action: str
    confidence: float
    risk: float
    engine: str
    source: str
    decision_type: str
    why: str
    detail: str = ""
    model: str | None = None
    rationale: str = ""
    cached: bool = False
    rule: dict | None = None


@dataclass
class EngineUsage:
    engine: str
    calls: int = 0
    live_calls: int = 0
    mock_calls: int = 0
    failed_calls: int = 0
    items: int = 0
    cache_hits: int = 0
    fallback_items: int = 0
    unavailable_items: int = 0
    input_tokens: int | None = None
    output_tokens: int | None = None
    provider_cost_usd: float | None = None
    estimated_cost_usd: float | None = None
    unpriced_calls: int = 0
    models: set[str] = field(default_factory=set)
    down_reason: str | None = None
    errors: list[str] = field(default_factory=list)

    @property
    def status(self) -> str:
        if self.down_reason:
            return "down"
        if self.live_calls:
            return "live"
        if self.mock_calls:
            return "mock"
        if self.cache_hits:
            return "cache"
        return "unused"

    def record(self, *, live: bool, model: str | None, usage: Mapping | None) -> None:
        self.calls += 1
        if live:
            self.live_calls += 1
        else:
            self.mock_calls += 1
        if model:
            self.models.add(model)
        if not live:
            return
        tokens_in, tokens_out, cost = _usage_numbers(usage)
        if tokens_in is not None:
            self.input_tokens = (self.input_tokens or 0) + tokens_in
        if tokens_out is not None:
            self.output_tokens = (self.output_tokens or 0) + tokens_out
        if cost is not None:
            self.provider_cost_usd = round((self.provider_cost_usd or 0.0) + cost, 8)
        rate = _rate(self.engine, model)
        if rate is None or tokens_in is None:
            self.unpriced_calls += 1
            return
        estimate = tokens_in * rate[0] / 1e6 + (tokens_out or 0) * rate[1] / 1e6
        self.estimated_cost_usd = round((self.estimated_cost_usd or 0.0) + estimate, 8)

    def fail(self, reason: str) -> None:
        self.failed_calls += 1
        self.down_reason = self.down_reason or reason
        if len(self.errors) < 5:
            self.errors.append(reason)

    def merge(self, other: EngineUsage) -> None:
        for name in (
            "calls", "live_calls", "mock_calls", "failed_calls", "items", "cache_hits",
            "fallback_items", "unavailable_items", "unpriced_calls",
        ):
            setattr(self, name, getattr(self, name) + getattr(other, name))
        for name in ("input_tokens", "output_tokens"):
            if getattr(other, name) is not None:
                setattr(self, name, (getattr(self, name) or 0) + getattr(other, name))
        for name in ("provider_cost_usd", "estimated_cost_usd"):
            if getattr(other, name) is not None:
                setattr(self, name, round((getattr(self, name) or 0.0) + getattr(other, name), 8))
        self.models |= other.models
        self.down_reason = self.down_reason or other.down_reason
        self.errors = (self.errors + other.errors)[:5]

    def to_dict(self) -> dict:
        rate = _rate(self.engine, next(iter(sorted(self.models)), None))
        return {
            "status": self.status,
            "calls": self.calls,
            "live_calls": self.live_calls,
            "mock_calls": self.mock_calls,
            "failed_calls": self.failed_calls,
            "items": self.items,
            "cache_hits": self.cache_hits,
            "fallback_items": self.fallback_items,
            "unavailable_items": self.unavailable_items,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "provider_cost_usd": self.provider_cost_usd,
            "estimated_cost_usd": self.estimated_cost_usd,
            "rate_usd_per_m_tokens": list(rate) if rate else None,
            "unpriced_calls": self.unpriced_calls,
            "models": sorted(self.models),
            "down_reason": self.down_reason,
            "errors": list(self.errors),
        }


@dataclass
class Ledger:
    """Per-run call counter. ``iterate`` merges one per round."""

    engines: dict[str, EngineUsage] = field(
        default_factory=lambda: {name: EngineUsage(name) for name in ENGINES}
    )
    warnings: list[str] = field(default_factory=list)

    def __getitem__(self, engine: str) -> EngineUsage:
        return self.engines[engine]

    def warn(self, text: str) -> None:
        if text not in self.warnings:
            self.warnings.append(text)

    def merge(self, other: Ledger) -> Ledger:
        for name, usage in other.engines.items():
            self.engines[name].merge(usage)
        for text in other.warnings:
            self.warn(text)
        return self

    def to_dict(self) -> dict:
        engines = {name: usage.to_dict() for name, usage in self.engines.items()}
        costs = [row["estimated_cost_usd"] for row in engines.values() if row["estimated_cost_usd"] is not None]
        return {
            "engines": engines,
            "totals": {
                "calls": sum(row["calls"] for row in engines.values()),
                "live_calls": sum(row["live_calls"] for row in engines.values()),
                "items": sum(row["items"] for row in engines.values()),
                "cache_hits": sum(row["cache_hits"] for row in engines.values()),
                "estimated_cost_usd": round(sum(costs), 8) if costs else None,
            },
            "warnings": list(self.warnings),
        }

    def summary(self) -> str:
        return format_usage(self.to_dict())


def format_usage(usage: Mapping) -> str:
    """One line per run: ``jev 2 calls (40 items, 3 cached) live · opus …``."""
    parts = []
    for name, row in (usage.get("engines") or {}).items():
        calls = row["calls"]
        items = row["items"]
        text = (
            f"{name} {calls} call{'s' if calls != 1 else ''} "
            f"({items} item{'s' if items != 1 else ''}, {row['cache_hits']} cached"
        )
        if row.get("failed_calls"):
            text += f", {row['failed_calls']} failed"
        if row.get("fallback_items"):
            text += f", {row['fallback_items']} by rules"
        if row.get("unavailable_items"):
            text += f", {row['unavailable_items']} left for review"
        text += f") {row['status']}"
        if row.get("input_tokens") is not None:
            text += f", {row['input_tokens']} in / {row.get('output_tokens') or 0} out tokens"
        parts.append(text)
    cost = (usage.get("totals") or {}).get("estimated_cost_usd")
    if cost is not None:
        parts.append(f"est ${cost:.4f}")
    return " · ".join(parts)


def redact(text: str) -> str:
    """Strip key material from an error string before it lands in a report."""
    for name in _SECRET_ENV:
        value = os.environ.get(name, "").strip()
        if len(value) >= 4:
            text = text.replace(value, "[redacted]")
    return _TOKEN.sub(lambda match: match.group(1) + "[redacted]", text)


class Router:
    """Routes decisions to Jev or Opus. One per run, or one per ``iterate`` loop.

    ``live=False`` (the default) never opens a socket and reads no key. With
    ``live=True`` at least one engine key must be set. An engine whose key is
    missing starts down, and its decisions take the fallback.
    """

    def __init__(
        self,
        *,
        live: bool = False,
        jev_client: Any | None = None,
        opus_client: Any | None = None,
        jev_window: int = JEV_WINDOW,
        opus_window: int = OPUS_WINDOW,
        fallback_discount: float = FALLBACK_DISCOUNT,
        decision_mode: str | None = None,
        mock_veto: str | None = None,
    ) -> None:
        if jev_window < 1 or opus_window < 1:
            raise ConductorError("router windows must be at least 1")
        if not 0.0 <= fallback_discount <= 1.0:
            raise ConductorError("fallback_discount must be between 0 and 1")
        self.live = bool(live) and not jev.dry_run_forced()
        self.jev_window = jev_window
        self.opus_window = opus_window
        self.fallback_discount = fallback_discount
        self.decision_mode = decision_mode if decision_mode is not None else resolve_decision_mode()
        self.mock_veto = mock_veto
        self.thresholds: dict[str, float] = {}
        self.ledger = Ledger()
        self._clients: dict[str, Any] = {JEV: jev_client, OPUS: opus_client}
        self._owned: list[Any] = []
        self._down: dict[str, str] = {}
        self._cache: dict[str, dict] = {}
        self._models = {JEV: jev.MOCK_MODEL, OPUS: opus.MOCK_MODEL}
        self._opus_endpoint: opus.Endpoint | None = None
        if not self.live:
            return
        jev_ready = jev.has_key()
        opus_ready = opus.has_key()
        if not jev_ready and not opus_ready:
            raise ConductorError(
                "No Jev key. Set OPENROUTER_API_KEY or TYPESAFE_API_KEY "
                "(and ANTHROPIC_API_KEY for creative decisions), or drop --live "
                "to dry-run with the local mock."
            )
        if jev_ready:
            self._models[JEV] = jev.resolve_endpoint().model
        else:
            self._down[JEV] = "no Jev key (OPENROUTER_API_KEY or TYPESAFE_API_KEY)"
        if opus_ready:
            self._opus_endpoint = opus.resolve_endpoint()
            self._models[OPUS] = self._opus_endpoint.model
        else:
            self._down[OPUS] = "ANTHROPIC_API_KEY is unset"

    def __enter__(self) -> Router:
        return self

    def __exit__(self, *_exc) -> None:
        self.close()

    def close(self) -> None:
        for engine in ENGINES:
            if any(self._clients[engine] is client for client in self._owned):
                self._clients[engine] = None
        for client in self._owned:
            client.close()
        self._owned.clear()

    def status(self) -> dict[str, str]:
        """``mock``, ``live``, or ``down: <reason>`` per engine, before any call."""
        if not self.live:
            return {engine: "mock" for engine in ENGINES}
        return {
            engine: f"down: {self._down[engine]}" if engine in self._down else "live"
            for engine in ENGINES
        }

    # ------------------------------------------------------------------
    # timeline candidates
    # ------------------------------------------------------------------

    def judge_candidates(
        self,
        candidates: list,
        *,
        brief: str,
        taste: Mapping[str, Any],
        ledger: Ledger | None = None,
        thresholds: Mapping[str, float] | None = None,
    ) -> tuple[dict[str, Verdict], list[dict]]:
        """Verdicts keyed by candidate id, plus one receipt per batch."""
        if thresholds is not None:
            self.thresholds = dict(thresholds)
        ledger = ledger or self.ledger
        verdicts: dict[str, Verdict] = {}
        receipts: list[dict] = []
        linear: list[tuple[Any, DecisionType]] = []
        creative: list[tuple[Any, DecisionType]] = []
        for item in candidates:
            dtype = classify(item.kind, item.pass_name)
            (linear if dtype.engine == JEV else creative).append((item, dtype))
        self._judge_linear(linear, brief, taste, ledger, verdicts, receipts)
        if creative:
            asks = [_candidate_ask(item, dtype, taste) for item, dtype in creative]
            decisions, opus_receipts = self._decide_opus(
                asks, brief=brief, context={"taste": {"prefs": dict(taste.get("prefs") or {})}}, ledger=ledger
            )
            receipts.extend(opus_receipts)
            for (item, dtype), decision in zip(creative, decisions, strict=True):
                verdicts[item.id] = _creative_verdict(decision, dtype)
        return verdicts, receipts

    def _judge_linear(self, pairs, brief, taste, ledger, verdicts, receipts) -> None:
        usage = ledger[JEV]
        prefs = {"prefs": dict(taste.get("prefs") or {})}
        pending: dict[str, list[tuple[Any, DecisionType]]] = {}
        for item, dtype in pairs:
            usage.items += 1
            key = self._key(
                JEV, dtype.name, _content(item.to_state()), brief, prefs,
                ["candidate-v2", self.decision_mode],
            )
            hit = self._cache.get(key)
            if hit is not None:
                usage.cache_hits += 1
                verdicts[item.id] = Verdict(**{**hit, "cached": True})
                continue
            if key in pending:
                usage.cache_hits += 1
            pending.setdefault(key, []).append((item, dtype))
        heads = [(key, group[0]) for key, group in pending.items()]
        for window in _windows(heads, self.jev_window, lambda entry: entry[1][0].to_state()):
            items = [item for _key, (item, _dtype) in window]
            hits = {
                item.id: hit
                for item in items
                if (hit := _rule_hit(self, item)) is not None
            }
            state = {"brief": brief, "taste": dict(taste), "candidates": [item.to_state() for item in items]}
            if hits:
                state["proposed_cuts"] = {
                    item_id: {
                        "action": hit.action,
                        "evidence": hit.evidence,
                        "rule": hit.name,
                        "measurement": hit.measurement,
                        "threshold": hit.threshold,
                        "threshold_name": hit.threshold_name,
                    }
                    for item_id, hit in hits.items()
                }
            questions: dict = {}
            for item in items:
                if item.id in hits:
                    questions[f"{item.id}_veto"] = _veto_question(item.id, hits[item.id])
                else:
                    questions.update(questions_for(item.id))
            open_questions = {
                key: spec for key, spec in questions.items() if not str(key).endswith("_veto")
            }
            asked = questions if self.live else open_questions
            fresh = self._ask_jev(state, asked, usage) if asked else None
            answers: dict[str, Verdict] = {}
            if fresh is None and open_questions:
                reason = self._down.get(JEV) or "Jev unavailable"
                usage.down_reason = usage.down_reason or reason
                missed = [item for item in items if item.id not in hits]
                usage.fallback_items += len(missed)
                ledger.warn(
                    f"Jev unavailable ({reason}). Linear calls used the deterministic rules "
                    f"at confidence x{self.fallback_discount}."
                )
                for _key, (item, dtype) in window:
                    if item.id in hits:
                        continue
                    action, confidence, risk = jev.policy(item.to_state(), taste)
                    answers[item.id] = Verdict(
                        action=action,
                        confidence=round(confidence * self.fallback_discount, 4),
                        risk=risk,
                        engine=JEV,
                        source="rules",
                        decision_type=dtype.name,
                        why=dtype.why,
                        detail=f"Jev unavailable: {reason}. Deterministic rule, confidence x{self.fallback_discount}.",
                        model=None,
                    )
                receipts.append(_receipt(JEV, "rules", None, state, questions, {
                    key: {"action": verdict.action, "confidence": verdict.confidence, "risk": verdict.risk}
                    for key, verdict in answers.items()
                }, error=reason))
            elif fresh is not None:
                receipts.append(_jev_receipt(fresh, state, questions))
                for key, (item, dtype) in window:
                    if item.id in hits:
                        continue
                    action_answer = fresh.answers[f"{item.id}_action"]
                    risk_answer = fresh.answers[f"{item.id}_risk"]
                    raw = str(action_answer.value)
                    answers[item.id] = Verdict(
                        action=raw if raw in jev.ACTIONS else "mark_review",
                        confidence=float(action_answer.confidence),
                        risk=float(risk_answer.value),
                        engine=JEV,
                        source="mock" if fresh.dry_run else "live",
                        decision_type=dtype.name,
                        why=dtype.why,
                        model=fresh.model,
                    )
                    self._cache[key] = _cacheable(answers[item.id])
            for key, (item, dtype) in window:
                if item.id not in hits:
                    continue
                veto_value, veto_confidence = _veto_answer(self, fresh, item.id)
                answers[item.id] = _rule_verdict(
                    dtype, hits[item.id], veto=veto_value, model_confidence=veto_confidence,
                    detail=_rule_detail(self, fresh),
                )
                self._cache[key] = _cacheable(answers[item.id])
            if hits and fresh is None and not open_questions:
                receipts.append(_receipt(
                    JEV, "logic", None, state, questions,
                    {
                        item_id: {"action": verdict.action, "confidence": verdict.confidence, "rule": verdict.rule}
                        for item_id, verdict in answers.items()
                    },
                ))
            for key, (head, _dtype) in window:
                verdict = answers[head.id]
                for index, (item, _dt) in enumerate(pending[key]):
                    verdicts[item.id] = verdict if index == 0 else Verdict(**{**asdict(verdict), "cached": True})

    def _ask_jev(self, state: dict, questions: dict, usage: EngineUsage):
        if self.live and JEV in self._down:
            return None
        try:
            batch = jev.ask(state, questions, live=self.live, client=self._client(JEV))
        except ConductorError as exc:
            reason = redact(str(exc))
            self._down[JEV] = reason
            usage.fail(reason)
            return None
        usage.record(live=not batch.dry_run, model=batch.model, usage=batch.usage)
        return batch

    # ------------------------------------------------------------------
    # generic decisions (assembly engine, future passes)
    # ------------------------------------------------------------------

    def decide(
        self,
        asks: Iterable[Ask],
        *,
        brief: str = "",
        context: Mapping[str, Any] | None = None,
        ledger: Ledger | None = None,
    ) -> tuple[list[Decision], list[dict]]:
        """Route each ask by its type. Returns decisions in ask order, plus receipts."""
        asks = list(asks)
        ledger = ledger or self.ledger
        context = dict(context or {})
        seen: set[str] = set()
        linear: list[Ask] = []
        creative: list[Ask] = []
        for ask in asks:
            _check_ask(ask, seen)
            (linear if classify(ask.type).engine == JEV else creative).append(ask)
        found: dict[str, Decision] = {}
        receipts: list[dict] = []
        decisions, got = self._decide_jev(linear, brief=brief, context=context, ledger=ledger)
        found.update({item.id: item for item in decisions})
        receipts.extend(got)
        decisions, got = self._decide_opus(creative, brief=brief, context=context, ledger=ledger)
        found.update({item.id: item for item in decisions})
        receipts.extend(got)
        return [found[ask.id] for ask in asks], receipts

    def _decide_jev(self, asks, *, brief, context, ledger) -> tuple[list[Decision], list[dict]]:
        usage = ledger[JEV]
        out: dict[str, Decision] = {}
        receipts: list[dict] = []
        pending: dict[str, list[Ask]] = {}
        for ask in asks:
            usage.items += 1
            dtype = classify(ask.type)
            key = self._key(JEV, dtype.name, ask.subject, brief, context, ["pick", ask.options, ask.question])
            hit = self._cache.get(key)
            if hit is not None:
                usage.cache_hits += 1
                out[ask.id] = Decision(id=ask.id, **{**hit, "cached": True})
                continue
            if key in pending:
                usage.cache_hits += 1
            pending.setdefault(key, []).append(ask)
        by_type: dict[str, list[tuple[str, Ask]]] = {}
        for key, group in pending.items():
            by_type.setdefault(group[0].type, []).append((key, group[0]))
        for type_name, heads in by_type.items():
            dtype = classify(type_name)
            for window in _windows(heads, self.jev_window, lambda entry: entry[1].subject):
                answers: dict[str, Decision] = {}
                state = {
                    "brief": brief,
                    "context": context,
                    "decision": dtype.question,
                    "items": [{"id": ask.id, **ask.subject} for _key, ask in window],
                }
                questions = {
                    f"{ask.id}_pick": choice(
                        f"Item {ask.id} is one object in state.items. {dtype.question} {ask.question}".strip(),
                        ask.options,
                        add_none=False,
                    )
                    for _key, ask in window
                }
                if not self.live:
                    usage.record(live=False, model=jev.MOCK_MODEL, usage=None)
                    for key, ask in window:
                        value, confidence = _rule(ask) if ask.rule else (next(iter(ask.options)), 0.5)
                        answers[ask.id] = Decision(
                            ask.id, ask.type, JEV, "mock", value, float(confidence), dtype.why,
                            model=jev.MOCK_MODEL,
                        )
                        self._cache[key] = _cacheable(answers[ask.id])
                    receipts.append(_receipt(JEV, "mock", jev.MOCK_MODEL, state, questions, {
                        ask_id: {"value": item.value, "confidence": item.confidence} for ask_id, item in answers.items()
                    }))
                else:
                    batch = self._ask_jev(state, questions, usage)
                    if batch is None:
                        reason = self._down[JEV]
                        usage.down_reason = usage.down_reason or reason
                        ledger.warn(
                            f"Jev unavailable ({reason}). Linear calls used the deterministic rules "
                            f"at confidence x{self.fallback_discount}."
                        )
                        for _key, ask in window:
                            answers[ask.id] = self._jev_fallback(ask, dtype, reason, usage)
                        receipts.append(_receipt(JEV, "rules", None, state, questions, {
                            ask_id: {"value": item.value, "confidence": item.confidence} for ask_id, item in answers.items()
                        }, error=reason))
                    else:
                        receipts.append(_jev_receipt(batch, state, questions))
                        for key, ask in window:
                            answer = batch.answers[f"{ask.id}_pick"]
                            answers[ask.id] = Decision(
                                ask.id, ask.type, JEV, "live", answer.value, float(answer.confidence),
                                dtype.why, model=batch.model,
                            )
                            self._cache[key] = _cacheable(answers[ask.id])
                for key, head in window:
                    decision = answers[head.id]
                    for ask in pending[key]:
                        out[ask.id] = decision if ask is head else Decision(
                            **{**asdict(decision), "id": ask.id, "cached": True}
                        )
        return [out[ask.id] for ask in asks], receipts

    def _jev_fallback(self, ask: Ask, dtype: DecisionType, reason: str, usage: EngineUsage) -> Decision:
        if ask.rule is None:
            usage.unavailable_items += 1
            return Decision(
                ask.id, ask.type, JEV, "unavailable", None, 0.0, dtype.why,
                detail=f"Jev unavailable: {reason}. No deterministic rule was given; needs a person.",
            )
        usage.fallback_items += 1
        value, confidence = _rule(ask)
        return Decision(
            ask.id, ask.type, JEV, "rules", value, round(float(confidence) * self.fallback_discount, 4),
            dtype.why,
            detail=f"Jev unavailable: {reason}. Deterministic rule, confidence x{self.fallback_discount}.",
        )

    def _decide_opus(self, asks, *, brief, context, ledger) -> tuple[list[Decision], list[dict]]:
        usage = ledger[OPUS]
        out: dict[str, Decision] = {}
        receipts: list[dict] = []
        pending: dict[str, list[Ask]] = {}
        for ask in asks:
            usage.items += 1
            dtype = classify(ask.type)
            key = self._key(
                OPUS, dtype.name, _content(ask.subject), brief, context,
                [_value_schema(ask), ask.options, ask.question],
            )
            hit = self._cache.get(key)
            if hit is not None:
                usage.cache_hits += 1
                out[ask.id] = Decision(id=ask.id, **{**hit, "cached": True})
                continue
            if key in pending:
                usage.cache_hits += 1
            pending.setdefault(key, []).append(ask)
        groups: dict[str, list[tuple[str, Ask]]] = {}
        for key, group in pending.items():
            head = group[0]
            signature = json.dumps([head.type, _value_schema(head)], sort_keys=True)
            groups.setdefault(signature, []).append((key, head))
        for heads in groups.values():
            dtype = classify(heads[0][1].type)
            value_schema = _value_schema(heads[0][1])
            for window in _windows(heads, self.opus_window, lambda entry: entry[1].subject):
                answers = self._opus_window(window, dtype, value_schema, brief, context, usage, ledger, receipts)
                for key, head in window:
                    decision = answers[head.id]
                    if decision.source in {"live", "mock"}:
                        self._cache[key] = _cacheable(decision)
                    for ask in pending[key]:
                        out[ask.id] = decision if ask is head else Decision(
                            **{**asdict(decision), "id": ask.id, "cached": decision.source != "unavailable"}
                        )
                        if ask is not head and decision.source == "unavailable":
                            usage.unavailable_items += 1
        return [out[ask.id] for ask in asks], receipts

    def _opus_window(self, window, dtype, value_schema, brief, context, usage, ledger, receipts) -> dict[str, Decision]:
        ids = [ask.id for _key, ask in window]
        payload = {
            "brief": brief,
            "context": context,
            "decision": {"type": dtype.name, "question": dtype.question},
            "items": [
                {
                    "id": ask.id,
                    "subject": ask.subject,
                    **({"question": ask.question} if ask.question else {}),
                    **({"options": ask.options} if ask.options else {}),
                }
                for _key, ask in window
            ],
        }
        schema = _response_schema(ids, value_schema)
        answers: dict[str, Decision] = {}
        if not self.live:
            usage.record(live=False, model=opus.MOCK_MODEL, usage=None)
            for _key, ask in window:
                value, confidence = _opus_mock(ask, value_schema)
                answers[ask.id] = Decision(
                    ask.id, ask.type, OPUS, "mock", value, float(confidence), dtype.why,
                    model=opus.MOCK_MODEL, rationale="dry-run mock; no model was called",
                )
            receipts.append(_receipt(OPUS, "mock", opus.MOCK_MODEL, payload, {"schema": schema}, {
                ask_id: item.to_dict() for ask_id, item in answers.items()
            }))
            return answers
        reply = None
        reason = self._down.get(OPUS)
        if reason is None:
            try:
                reply = opus.complete(
                    system=OPUS_SYSTEM,
                    payload=payload,
                    schema=schema,
                    endpoint=self._opus_endpoint,
                    client=self._client(OPUS),
                    max_tokens=min(32_000, 2_048 + 400 * len(window)),
                )
            except ConductorError as exc:
                reason = redact(str(exc))
                self._down[OPUS] = reason
                usage.fail(reason)
        if reply is None:
            usage.down_reason = usage.down_reason or reason
            usage.unavailable_items += len(window)
            ledger.warn(f"Opus unavailable ({reason}). Creative calls were left as review markers.")
            for _key, ask in window:
                answers[ask.id] = _unavailable(ask, dtype, reason)
            receipts.append(_receipt(OPUS, "unavailable", None, payload, {"schema": schema}, {}, error=reason))
            return answers
        usage.record(live=True, model=reply.model, usage=reply.usage)
        picked: dict[str, dict] = {}
        for row in reply.data["decisions"]:
            picked.setdefault(row["id"], row)
        for _key, ask in window:
            row = picked.get(ask.id)
            if row is None:
                usage.unavailable_items += 1
                answers[ask.id] = _unavailable(ask, dtype, "Opus did not answer this id")
                continue
            answers[ask.id] = Decision(
                ask.id, ask.type, OPUS, "live", row["value"], _clamp(row["confidence"]), dtype.why,
                model=reply.model, rationale=_trim(row.get("rationale") or "", RATIONALE_CHARS),
            )
        receipts.append(_receipt(OPUS, "live", reply.model, payload, {"schema": schema}, {
            ask_id: item.to_dict() for ask_id, item in answers.items()
        }, request_id=reply.request_id, usage=reply.usage))
        return answers

    # ------------------------------------------------------------------

    def _client(self, engine: str) -> Any:
        if not self.live:
            return None
        client = self._clients[engine]
        if client is None:
            import httpx

            client = httpx.Client(timeout=HTTP_TIMEOUT)
            self._clients[engine] = client
            self._owned.append(client)
        return client

    def _key(self, engine: str, type_name: str, subject: Any, brief: str, context: Any, spec: Any) -> str:
        blob = json.dumps(
            [engine, self._models[engine], self.live, type_name, subject, brief, context, spec],
            sort_keys=True,
            default=str,
            ensure_ascii=False,
        )
        return hashlib.blake2b(blob.encode("utf-8"), digest_size=16).hexdigest()


def _rule_hit(router: Router, item) -> RuleHit | None:
    if router.decision_mode != "logic-first" or not router.thresholds:
        return None
    hit = evaluate(item, router.thresholds)
    if hit is None or not hit.applies:
        return None
    return hit


def _veto_question(item_id: str, hit: RuleHit):
    return choice(
        f"Candidate {item_id} already has a measured cut: {hit.action}. {hit.evidence}. "
        "You may allow that cut or veto it. You do not choose a different action.",
        VETO_OPTIONS,
        add_none=False,
    )


def _veto_answer(router: Router, fresh, item_id: str) -> tuple[str, float]:
    if fresh is not None:
        answer = fresh.answers.get(f"{item_id}_veto")
        if answer is not None:
            return str(answer.value), float(answer.confidence)
    if router.mock_veto:
        return router.mock_veto, 0.9
    return "allow", 0.5


def _rule_detail(router: Router, fresh) -> str:
    if fresh is None and router.live:
        return f"Jev unavailable: {router._down.get(JEV, '')}. Measured rule applied with no veto."
    if not router.live:
        return "logic-first. The mock model is advisory at 0.5 and does not gate the cut."
    return "logic-first. The model may veto this measured cut. It does not set the score."


def _rule_verdict(dtype: DecisionType, hit: RuleHit, *, veto: str, model_confidence: float, detail: str) -> Verdict:
    vetoed = bool(veto) and veto != "allow"
    reason = VETO_OPTIONS.get(veto, "") if vetoed else ""
    if vetoed and not reason:
        reason = veto
    rule = hit.to_dict()
    rule["vetoed"] = vetoed
    rule["veto_reason"] = reason
    rule["model_confidence"] = model_confidence
    if vetoed:
        return Verdict(
            action="mark_review",
            confidence=hit.confidence,
            risk=0.0,
            engine="rules",
            source="veto",
            decision_type=dtype.name,
            why=dtype.why,
            detail=detail,
            rationale=reason,
            rule=rule,
        )
    return Verdict(
        action=hit.action,
        confidence=hit.confidence,
        risk=0.1,
        engine="rules",
        source="logic",
        decision_type=dtype.name,
        why=dtype.why,
        detail=detail,
        rule=rule,
    )


def questions_for(candidate_id: str) -> dict:
    """The two Jev questions for one candidate. ``none`` is not an option."""
    action = choice(
        f"Candidate {candidate_id} is one object in state.candidates. "
        "Which edit should the editor consider for that candidate, given the brief "
        "and state.taste? A person will see this as a marker. "
        "Nothing is applied automatically.",
        jev.ACTION_CRITERIA,
        add_none=False,
    )
    risk = noul(
        f"Candidate {candidate_id}: would cutting or tightening this region "
        "damage the story or clip off a thought the brief still needs?",
        true="Cutting risks losing meaning, a reaction, or a breath the edit needs.",
        false="Cutting is safe. The region is dead air, a flash frame, or disposable filler.",
    )
    return {f"{candidate_id}_action": action, f"{candidate_id}_risk": risk}


def _candidate_ask(item, dtype: DecisionType, taste: Mapping[str, Any]) -> Ask:
    state = item.to_state()

    def mock() -> tuple[dict, float]:
        action, confidence, risk = jev.policy(state, taste)
        return {"action": action, "risk": risk}, confidence

    return Ask(
        id=item.id,
        type=dtype.name,
        subject=state,
        options=dict(jev.ACTION_CRITERIA),
        schema=CANDIDATE_SCHEMA,
        question=(
            "Pick one action for this timeline region, and `risk`: the probability "
            "(0 to 1) that acting on it would damage the story."
        ),
        mock=mock,
    )


def _creative_verdict(decision: Decision, dtype: DecisionType) -> Verdict:
    if decision.source == "unavailable" or not isinstance(decision.value, dict):
        return Verdict(
            action="mark_review", confidence=0.0, risk=1.0, engine=OPUS, source="unavailable",
            decision_type=dtype.name, why=dtype.why, detail=decision.detail or "Opus unavailable",
        )
    return Verdict(
        action=str(decision.value["action"]),
        confidence=decision.confidence,
        risk=_clamp(decision.value["risk"]),
        engine=OPUS,
        source=decision.source,
        decision_type=dtype.name,
        why=dtype.why,
        model=decision.model,
        rationale=decision.rationale,
        cached=decision.cached,
    )


def _unavailable(ask: Ask, dtype: DecisionType, reason: str) -> Decision:
    return Decision(
        ask.id, ask.type, OPUS, "unavailable", None, 0.0, dtype.why,
        detail=f"Opus unavailable: {reason}. Left for a person; nothing is auto-applied.",
    )


def _rule(ask: Ask) -> tuple[Any, float]:
    value, confidence = ask.rule(ask)
    if value not in (ask.options or {}):
        raise ConductorError(f"rule for {ask.id!r} returned {value!r}, not one of {list(ask.options or {})}")
    return value, _clamp(confidence)


def _opus_mock(ask: Ask, value_schema: dict) -> tuple[Any, float]:
    if callable(ask.mock):
        value, confidence = ask.mock()
    elif ask.mock is not None:
        value, confidence = ask.mock, 0.5
    else:
        value, confidence = example(value_schema), 0.5
    try:
        return validate(value, value_schema), confidence
    except SchemaError as exc:
        raise ConductorError(f"mock for {ask.id!r} does not match its schema: {exc}") from exc


def _value_schema(ask: Ask) -> dict:
    if ask.schema is not None:
        return ask.schema
    return {"type": "string", "enum": list(ask.options or {})}


def _response_schema(ids: list[str], value_schema: dict) -> dict:
    return {
        "type": "object",
        "properties": {
            "decisions": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string", "enum": list(ids)},
                        "value": value_schema,
                        "confidence": {"type": "number"},
                        "rationale": {"type": "string"},
                    },
                    "required": ["id", "value", "confidence", "rationale"],
                    "additionalProperties": False,
                },
            }
        },
        "required": ["decisions"],
        "additionalProperties": False,
    }


def _check_ask(ask: Ask, seen: set[str]) -> None:
    if not _ID.match(ask.id or ""):
        raise ConductorError(f"ask id {ask.id!r} must be letters, digits, '_', '.', or '-'")
    if ask.id in seen:
        raise ConductorError(f"duplicate ask id {ask.id!r}")
    seen.add(ask.id)
    dtype = classify(ask.type)
    if dtype.engine == JEV:
        if ask.schema is not None:
            raise ConductorError(f"{ask.id}: Jev only selects; pass options, not a schema")
        if not ask.options:
            raise ConductorError(f"{ask.id}: a Jev decision needs options")
        if len(ask.options) > MAX_OPTIONS:
            raise ConductorError(
                f"{ask.id}: {len(ask.options)} options is over Jev's {MAX_OPTIONS}; chunk and re-rank"
            )
    elif not ask.options and ask.schema is None:
        raise ConductorError(f"{ask.id}: an Opus decision needs options or a schema")


def _content(state: Mapping[str, Any]) -> dict:
    """The part of a candidate that decides its answer: no id, no position."""
    return {key: value for key, value in state.items() if key not in _POSITIONAL}


def _cacheable(item: Verdict | Decision) -> dict:
    row = asdict(item)
    row.pop("id", None)
    row.pop("cached", None)
    return row


def _windows(items: list, size: int, state_of: Callable[[Any], Any]):
    window: list = []
    chars = 0
    for item in items:
        width = len(json.dumps(state_of(item), sort_keys=True, default=str))
        if window and (len(window) >= size or chars + width > MAX_STATE_CHARS):
            yield window
            window, chars = [], 0
        window.append(item)
        chars += width
    if window:
        yield window


def _receipt(engine, source, model, state, questions, answers, *, error=None, request_id=None, usage=None) -> dict:
    row = {
        "engine": engine,
        "source": source,
        "dry_run": source == "mock",
        "model": model,
        "request_id": request_id,
        "usage": usage,
        "state": state,
        "questions": questions,
        "answers": answers,
    }
    if error:
        row["error"] = error
    return row


def _jev_receipt(batch, state: dict, questions: dict) -> dict:
    row = _receipt(
        JEV,
        "mock" if batch.dry_run else "live",
        batch.model,
        state,
        questions,
        {key: answer.to_dict() for key, answer in batch.answers.items()},
        request_id=batch.request_id,
        usage=batch.usage,
    )
    row["provider"] = batch.provider
    row["endpoint"] = batch.endpoint
    return row


def _usage_numbers(usage: Mapping | None) -> tuple[int | None, int | None, float | None]:
    if not isinstance(usage, Mapping):
        return None, None, None

    def first(*names):
        for name in names:
            value = usage.get(name)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                return value
        return None

    tokens_in = first("input_tokens", "prompt_tokens")
    tokens_out = first("output_tokens", "completion_tokens")
    cost = first("cost", "total_cost", "cost_usd")
    return (
        int(tokens_in) if tokens_in is not None else None,
        int(tokens_out) if tokens_out is not None else None,
        float(cost) if cost is not None else None,
    )


def _rate(engine: str, model: str | None) -> tuple[float, float] | None:
    if engine == JEV:
        return (USD_PER_M_INPUT_TOKENS, 0.0)
    if model is None:
        return None
    return opus.USD_PER_M.get(model)


def _clamp(value: Any) -> float:
    number = float(value)
    return 0.0 if number < 0.0 else 1.0 if number > 1.0 else number


def _trim(text: str, limit: int) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[: limit - 1] + "…"
