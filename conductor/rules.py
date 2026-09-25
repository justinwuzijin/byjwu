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
import re
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field, replace
from difflib import SequenceMatcher
from fractions import Fraction
from pathlib import Path

from .errors import ConductorError
from .style import StyleProfile

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
        f"Rules fired: {len(fired)}. Rule cuts written: {applied}. Vetoed: {len(vetoed)}.",
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


# --- retake selection and filler proposals ---

_FILLERS = frozenset({"um", "uh", "erm", "hmm"})
_AMBIGUOUS = ("you know", "basically", "like", "so")
_TOKEN = re.compile(r"[a-z0-9']+")
_SENTENCE_END = frozenset(".!?")


@dataclass(frozen=True)
class TakeSignal:
    """Optional per-take measurements. Missing values stay neutral."""

    filler_count: int | None = None
    loudness_variance: float | None = None
    clipping: bool | None = None
    speech_rate: float | None = None


@dataclass
class Utterance:
    clip_id: str
    start: Fraction
    end: Fraction
    text: str
    tokens: tuple[str, ...]
    words: tuple
    cutoff: bool = False

    @property
    def duration(self) -> Fraction:
        return self.end - self.start


@dataclass
class TakeGroup:
    clip_id: str
    takes: list[Utterance]
    complete: list[int]
    keep: int
    close: bool
    scores: list[float]
    cuts: list[dict] = field(default_factory=list)


@dataclass
class RulePlan:
    groups: list[TakeGroup]
    cuts: list[dict]
    markers: list[dict]
    filler_proposals: list[dict]


def plan_retakes(
    words: list,
    *,
    profile: StyleProfile | None = None,
    signals: dict | None = None,
    vetoes: dict | None = None,
    picks: dict | None = None,
) -> RulePlan:
    """Group retakes and mark the cuts. ``vetoes`` and ``picks`` are fixtures.

    ``vetoes`` maps a cut id to a reason. A veto drops that cut and records a
    review marker. ``picks`` maps a group index to a take index and is used
    only when that group is close. Both default to the logic answer.
    """
    profile = profile or StyleProfile()
    signals = signals or {}
    vetoes = vetoes or {}
    picks = picks or {}
    if not words or not _timed(words):
        return RulePlan([], [], [], propose_fillers([], profile=profile))
    groups: list[TakeGroup] = []
    for clip_id, clip_words in _by_clip(words).items():
        if not _timed(clip_words):
            continue
        utterances = _utterances(clip_words, profile)
        groups.extend(_groups(clip_id, utterances, profile, signals))
    cuts: list[dict] = []
    markers: list[dict] = []
    for index, group in enumerate(groups):
        if group.close and index in picks:
            chosen = picks[index]
            if chosen in group.complete:
                group.keep = chosen
        _mark_cuts(group, profile)
        kept_cuts = []
        for cut in group.cuts:
            reason = vetoes.get(cut["id"])
            if reason:
                markers.append(
                    {
                        "id": cut["id"],
                        "kind": "retake_veto",
                        "sequence": cut.get("sequence", ""),
                        "clip_id": cut["clip_id"],
                        "start": cut["start"],
                        "end": cut["end"],
                        "value": "Retake kept",
                        "note": f"Both takes kept. {reason}",
                    }
                )
                continue
            kept_cuts.append(cut)
            markers.append(
                {
                    "id": cut["id"],
                    "kind": "retake",
                    "sequence": cut.get("sequence", ""),
                    "clip_id": cut["clip_id"],
                    "start": cut["start"],
                    "end": cut["end"],
                    "value": "Retake",
                    "note": "Earlier take removed. The last complete take was kept.",
                }
            )
        group.cuts = kept_cuts
        cuts.extend(kept_cuts)
    return RulePlan(groups, cuts, markers, propose_fillers(words, profile=profile))


def propose_fillers(words: list, *, profile: StyleProfile | None = None) -> list[dict]:
    """Auto-cut um/uh/erm beside silence. Ambiguous fillers stay proposals."""
    profile = profile or StyleProfile()
    ordered = sorted(_by_clip_flat(words), key=lambda item: (item[0], item[1].start))
    proposals: list[dict] = []
    for index, (clip_id, word) in enumerate(ordered):
        token = _token(word.text)
        phrase = _phrase_at(ordered, index)
        kind = None
        auto = False
        if token in _FILLERS or token == "uh":
            kind = "filler_auto"
            auto = _silence_beside(ordered, index, profile.filler_silence)
        elif phrase:
            kind = "filler_proposal"
            auto = False
        if kind is None:
            continue
        if kind == "filler_auto" and not auto:
            continue
        start, end = _span(ordered, index, phrase)
        proposals.append(
            {
                "id": f"filler-{clip_id}-{index}",
                "kind": kind,
                "clip_id": clip_id,
                "start": start,
                "end": end,
                "text": phrase or token,
                "auto": auto if kind == "filler_auto" else False,
                "sequence": getattr(word, "sequence", ""),
            }
        )
    return proposals


def retake_asks(plan: RulePlan):
    """Router asks. Taste picks a close group. Each cut may be vetoed.

    The dry-run answer is the logic choice: last complete take, and no veto.
    """
    from .router import Ask

    asks = []
    for index, group in enumerate(plan.groups):
        if not group.close:
            continue
        options = {
            f"t{take_index}": " ".join(group.takes[take_index].tokens)
            for take_index in group.complete
        }
        keep = f"t{group.keep}"

        def _rule(ask, keep=keep):
            return keep, 0.9

        asks.append(
            Ask(
                id=f"retake-pick-{index}",
                type="retake_pick",
                subject={
                    "group": index,
                    "takes": [
                        {
                            "id": f"t{take_index}",
                            "text": " ".join(group.takes[take_index].tokens),
                            "score": group.scores[take_index],
                        }
                        for take_index in group.complete
                    ],
                },
                options=options,
                question="Pick one existing complete take. Do not invent a span.",
                rule=_rule,
                mock=lambda keep=keep: (keep, 0.9),
            )
        )
    for cut in plan.cuts:
        def _allow(ask):
            return "allow", 0.9

        asks.append(
            Ask(
                id=f"veto-{cut['id']}",
                type="retake_veto",
                subject={"cut_id": cut["id"], "text": cut.get("text", "")},
                options={
                    "allow": "The earlier take is a retake or false start and should be cut.",
                    "veto": "The repeat is intentional. Keep both takes.",
                },
                question="Veto this retake cut, or allow it.",
                rule=_allow,
                mock=lambda: ("allow", 0.9),
            )
        )
    for proposal in plan.filler_proposals:
        if proposal["auto"]:
            continue

        def _keep(ask):
            return "keep", 0.9

        asks.append(
            Ask(
                id=f"approve-{proposal['id']}",
                type="filler_approve",
                subject={"text": proposal["text"], "start": str(proposal["start"])},
                options={
                    "keep": "Leave the word. It may carry meaning.",
                    "cut": "It is disposable filler.",
                },
                question="Approve cutting this filler, or keep it.",
                rule=_keep,
                mock=lambda: ("keep", 0.9),
            )
        )
    return asks


def apply_router(plan: RulePlan, decisions: list) -> RulePlan:
    """Fold router answers into the plan. A veto becomes a review marker."""
    by_id = {item.id: item for item in decisions}
    for index, group in enumerate(plan.groups):
        decision = by_id.get(f"retake-pick-{index}")
        if decision is None or decision.value is None or not group.close:
            continue
        chosen = str(decision.value)
        for take_index in group.complete:
            if f"t{take_index}" == chosen:
                group.keep = take_index
        _mark_cuts(group, StyleProfile())
    cuts = []
    markers = []
    for cut in plan.cuts:
        decision = by_id.get(f"veto-{cut['id']}")
        if decision is not None and decision.value == "veto":
            note = decision.rationale or "The repeat looks intentional."
            markers.append(
                {
                    "id": cut["id"],
                    "kind": "retake_veto",
                    "clip_id": cut["clip_id"],
                    "start": cut["start"],
                    "end": cut["end"],
                    "value": "Retake kept",
                    "note": f"Both takes kept. {note}",
                    "sequence": cut.get("sequence", ""),
                }
            )
            continue
        cuts.append(cut)
        markers.append(
            {
                "id": cut["id"],
                "kind": "retake",
                "clip_id": cut["clip_id"],
                "start": cut["start"],
                "end": cut["end"],
                "value": "Retake",
                "note": "Earlier take removed. The last complete take was kept.",
                "sequence": cut.get("sequence", ""),
            }
        )
    approved = []
    for proposal in plan.filler_proposals:
        if proposal["auto"]:
            approved.append(proposal)
            continue
        decision = by_id.get(f"approve-{proposal['id']}")
        if decision is not None and decision.value == "cut":
            approved.append({**proposal, "auto": True})
    plan.cuts = cuts
    plan.markers = markers
    plan.filler_proposals = approved
    for group in plan.groups:
        group.cuts = [cut for cut in group.cuts if any(cut["id"] == kept["id"] for kept in cuts)]
    return plan


def _mark_cuts(group: TakeGroup, profile: StyleProfile) -> None:
    """Each discarded take, plus the gap up to the next kept word."""
    keep_take = group.takes[group.keep]
    ordered = sorted(range(len(group.takes)), key=lambda index: group.takes[index].start)
    cuts = []
    for position, index in enumerate(ordered):
        if index == group.keep:
            continue
        take = group.takes[index]
        end = take.end
        for later in ordered[position + 1 :]:
            nxt = group.takes[later]
            if later == group.keep:
                end = nxt.start
            else:
                end = nxt.start
            break
        else:
            end = take.end
        if end < take.end:
            end = take.end
        cuts.append(
            {
                "id": f"retake-{group.clip_id}-{_stamp(take.start)}",
                "kind": "retake",
                "reason": "retake",
                "clip_id": group.clip_id,
                "sequence": getattr(take.words[0], "sequence", "") if take.words else "",
                "start": take.start,
                "end": end,
                "text": take.text,
                "explicit_trim": True,
            }
        )
    group.cuts = cuts
    # profile is accepted so callers can pass it; spans are word-bounded.
    del profile
    del keep_take


def _groups(clip_id: str, utterances: list[Utterance], profile: StyleProfile, signals: dict) -> list[TakeGroup]:
    parent = list(range(len(utterances)))

    def find(index: int) -> int:
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    for left in range(len(utterances)):
        for right in range(left + 1, len(utterances)):
            if utterances[right].start > utterances[left].end + profile.group_window:
                break
            if _linked(utterances[left], utterances[right], profile):
                parent[find(right)] = find(left)
    buckets: dict[int, list[int]] = {}
    for index in range(len(utterances)):
        buckets.setdefault(find(index), []).append(index)
    groups = []
    for indexes in buckets.values():
        if len(indexes) < 2:
            continue
        takes = [utterances[index] for index in indexes]
        _flag_incomplete(takes, profile)
        complete = [index for index, take in enumerate(takes) if not getattr(take, "incomplete", False)]
        if not complete:
            keep = max(range(len(takes)), key=lambda index: takes[index].start)
            complete_for_score = list(range(len(takes)))
        else:
            keep = max(complete, key=lambda index: takes[index].start)
            complete_for_score = complete
        scored = [_score(take, signals.get(_key(take)), profile) for take in takes]
        close_pool = [scored[index] for index in complete_for_score]
        close = (
            len(complete) >= 2
            and max(close_pool) - min(close_pool) < profile.take_score_margin
        )
        groups.append(
            TakeGroup(clip_id, takes, complete, keep, close, scored)
        )
    return groups


def _flag_incomplete(takes: list[Utterance], profile: StyleProfile) -> None:
    longest = max(takes, key=lambda take: (len(take.tokens), take.start))
    long_tokens = longest.tokens
    for index, take in enumerate(takes):
        later = takes[index + 1 :]
        prefix = any(
            take.tokens
            and len(take.tokens) < len(other.tokens)
            and other.tokens[: len(take.tokens)] == take.tokens
            for other in later
        )
        coverage = 1.0
        if long_tokens:
            have = list(take.tokens)
            matched = 0
            for token in long_tokens:
                if token in have:
                    have.remove(token)
                    matched += 1
            coverage = matched / len(long_tokens)
        short = coverage < profile.incomplete_coverage and take is not longest
        take.incomplete = bool(prefix or take.cutoff or short)  # type: ignore[attr-defined]


def _score(take: Utterance, signal: TakeSignal | None, profile: StyleProfile) -> float:
    signal = signal or TakeSignal()
    tokens = max(len(take.tokens), 1)
    fillers = signal.filler_count
    if fillers is None:
        fillers = sum(1 for token in take.tokens if token in _FILLERS)
    filler_score = 1.0 - min(1.0, fillers / tokens)
    if signal.loudness_variance is None:
        level_score = 0.5
    else:
        level_score = 1.0 / (1.0 + max(0.0, signal.loudness_variance))
    if signal.clipping:
        clip_score = 0.0
    else:
        clip_score = 1.0
    rate = signal.speech_rate
    if rate is None and take.duration > 0:
        rate = len(take.tokens) / float(take.duration)
    median = profile.speech_rate_median or 1.0
    if rate is None:
        rate_score = 0.5
    else:
        rate_score = 1.0 - min(1.0, abs(rate - median) / median)
    return round(0.35 * filler_score + 0.25 * level_score + 0.20 * clip_score + 0.20 * rate_score, 4)


def _linked(left: Utterance, right: Utterance, profile: StyleProfile) -> bool:
    if (
        len(left.tokens) >= profile.prefix_tokens
        and right.tokens[: len(left.tokens)] == left.tokens
    ):
        return True
    if not left.tokens or not right.tokens:
        return False
    return SequenceMatcher(a=left.tokens, b=right.tokens).ratio() >= profile.similarity


def _utterances(words: list, profile: StyleProfile) -> list[Utterance]:
    ordered = sorted(words, key=lambda word: (word.start, word.end))
    groups: list[list] = []
    bucket: list = []
    for word in ordered:
        if not str(word.text).strip():
            continue
        if bucket:
            gap = word.start - bucket[-1].end
            if gap >= profile.utterance_pause or _ends_sentence(bucket[-1].text):
                groups.append(bucket)
                bucket = []
        bucket.append(word)
    if bucket:
        groups.append(bucket)
    utterances = []
    for group in groups:
        text = " ".join(str(word.text).strip() for word in group)
        tokens = tuple(_normalize(text))
        cutoff = _ends_on_cutoff(group, profile)
        clip_id = str(getattr(group[0], "clip_id", "") or "")
        utterances.append(
            Utterance(clip_id, group[0].start, group[-1].end, text, tokens, tuple(group), cutoff)
        )
    return utterances


def _ends_on_cutoff(words: list, profile: StyleProfile) -> bool:
    end = words[-1].end
    for word in words:
        if end - word.end > profile.cutoff_window:
            continue
        if _is_cutoff(word, profile):
            return True
    return False


def _is_cutoff(word, profile: StyleProfile) -> bool:
    if getattr(word, "partial", False):
        return True
    text = str(word.text).strip()
    if text.endswith("-") or text.endswith("—"):
        return True
    confidence = getattr(word, "confidence", None)
    return confidence is not None and float(confidence) < profile.cutoff_confidence


def _normalize(text: str) -> list[str]:
    tokens = _TOKEN.findall(text.lower())
    return [token for token in tokens if token not in _FILLERS]


def _ends_sentence(text: str) -> bool:
    stripped = str(text).rstrip()
    return bool(stripped) and stripped[-1] in _SENTENCE_END


def _by_clip(words: list) -> dict[str, list]:
    grouped: dict[str, list] = {}
    for word in words:
        clip_id = str(getattr(word, "clip_id", "") or "")
        grouped.setdefault(clip_id, []).append(word)
    return grouped


def _timed(words: list) -> bool:
    return any(getattr(word, "start", None) is not None and getattr(word, "end", None) is not None for word in words)


def _by_clip_flat(words: list) -> list[tuple[str, object]]:
    return [(str(getattr(word, "clip_id", "") or ""), word) for word in words if str(getattr(word, "text", "")).strip()]


def _token(text: str) -> str:
    tokens = _TOKEN.findall(str(text).lower())
    return tokens[0] if tokens else ""


def _phrase_at(ordered, index: int) -> str | None:
    clip_id, word = ordered[index]
    token = _token(word.text)
    if token == "you" and index + 1 < len(ordered) and ordered[index + 1][0] == clip_id:
        nxt = _token(ordered[index + 1][1].text)
        if nxt == "know":
            return "you know"
    if token in {"like", "so", "basically"}:
        return token
    return None


def _silence_beside(ordered, index: int, minimum: Fraction) -> bool:
    _clip, word = ordered[index]
    before = Fraction(0)
    after = Fraction(0)
    if index > 0 and ordered[index - 1][0] == ordered[index][0]:
        before = word.start - ordered[index - 1][1].end
    if index + 1 < len(ordered) and ordered[index + 1][0] == ordered[index][0]:
        after = ordered[index + 1][1].start - word.end
    else:
        after = minimum
    if index == 0:
        before = minimum
    return before >= minimum or after >= minimum


def _span(ordered, index: int, phrase: str | None) -> tuple[Fraction, Fraction]:
    word = ordered[index][1]
    end = word.end
    if phrase == "you know" and index + 1 < len(ordered):
        end = ordered[index + 1][1].end
    return word.start, end


def _key(take: Utterance) -> tuple:
    return (take.clip_id, take.start, take.end)


def _stamp(moment: Fraction) -> str:
    return str(moment).replace("/", "_").replace(" ", "")

