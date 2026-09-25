"""Repeat analyze, then auto-apply only mechanical cuts, until the cut holds.

Each round writes ``<out>/vN/``. The next round reads the applied FCPXML when
this round cut something, and the shadow FCPXML when it did not. Taste is the
file the round just wrote, so accepted ids travel with the log.

Stop on the first of:

- ``metrics`` — every configured target holds (duration ± tolerance, escalate
  cap, review cap, silence, average shot length, cuts per minute).
- ``no-progress`` — the round applied no mechanical cut.
- ``max-rounds`` — the cap was hit after a round that still cut something.

A person is needed when the last round still has an escalate, or the stop
reason is ``max-rounds``. Review rows stay marked. They are not applied here.
Colour is judged with the other passes and is never on the auto-apply path.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from . import __version__
from .errors import ConductorError
from .fcpxml import parse_fcpxml, write_document
from .ingest import (
    file_hash,
    inventory,
    load_duration_overrides,
    render_starter,
)
from .metrics import Targets, measure
from .report import dumps
from .run import Report, analyze

PROTOCOL = "cut-conductor.iterate"
PROTOCOL_VERSION = 1
MECHANICAL = ["mechanical"]


@dataclass
class IterateResult:
    stop_reason: str
    rounds: list[dict]
    applied: list[dict]
    needs_human: bool
    human_reasons: list[str]
    cleared: list[str]
    warnings: list[str] = field(default_factory=list)
    starter: Path | None = None
    out_json: Path | None = None
    source: Path | None = None
    signals_summary: str = ""


def iterate(
    *,
    brief: str,
    out_dir: str | Path,
    fcpxml: str | Path | None = None,
    media: str | Path | None = None,
    transcript_path: str | Path | None = None,
    taste_path: str | Path | None = None,
    durations_path: str | Path | None = None,
    sequence: str | None = None,
    project: str | None = None,
    passes: list[str] | None = None,
    live: bool = False,
    html: bool = False,
    max_rounds: int = 5,
    min_confidence: float = 0.8,
    target_seconds: float | None = None,
    tolerance: float = 1.0,
    max_escalate: int | None = None,
    max_review: int | None = None,
    max_silence_seconds: float | None = None,
    min_shot_seconds: float | None = None,
    max_cuts_per_minute: float | None = None,
    signals: str = "auto",
    transcribe: str = "auto",
    signal_cache: str | Path | None = None,
    global_taste_path: str | Path | None = None,
    feedback_path: str | Path | None = None,
    learn_from: str | Path | None = None,
) -> IterateResult:
    """Run the unattended mechanical loop. Dry-run unless ``live`` is set."""
    if bool(fcpxml) == bool(media):
        raise ConductorError("iterate needs exactly one of --fcpxml or --media")
    if media is None and (durations_path or sequence):
        raise ConductorError("--durations and --sequence are for --media")
    if not brief or not str(brief).strip():
        raise ConductorError("iterate needs a --brief")
    if max_rounds < 1:
        raise ConductorError("--max-rounds must be at least 1")
    if tolerance < 0:
        raise ConductorError("--tolerance must be >= 0")
    if not 0.0 <= float(min_confidence) <= 1.0:
        raise ConductorError("--min-confidence must be between 0 and 1")
    for name, value in (
        ("--max-escalate", max_escalate),
        ("--max-review", max_review),
    ):
        if value is not None and value < 0:
            raise ConductorError(f"{name} must be >= 0")
    for name, value in (
        ("--target-seconds", target_seconds),
        ("--max-silence-seconds", max_silence_seconds),
        ("--min-shot-seconds", min_shot_seconds),
        ("--max-cuts-per-minute", max_cuts_per_minute),
    ):
        if value is not None and float(value) < 0:
            raise ConductorError(f"{name} must be >= 0")

    targets = Targets(
        target_seconds=target_seconds,
        tolerance=tolerance,
        max_escalate=max_escalate,
        max_review=max_review,
        max_silence_seconds=max_silence_seconds,
        min_shot_seconds=min_shot_seconds,
        max_cuts_per_minute=max_cuts_per_minute,
    )
    destination = Path(out_dir)
    destination.mkdir(parents=True, exist_ok=True)
    warnings: list[str] = []
    starter: Path | None = None
    if media is not None:
        starter, warnings = _starter(
            media,
            destination,
            durations_path=durations_path,
            sequence=sequence,
        )
        source = starter
    else:
        source = Path(fcpxml or "")
        if not source.is_file():
            raise ConductorError(f"no such FCPXML: {source}")

    taste = taste_path
    rounds: list[dict] = []
    applied: list[dict] = []
    reason = "max-rounds"
    cleared: list[str] = []
    for number in range(1, max_rounds + 1):
        staged = _stage(source, destination / f"v{number}")
        report = analyze(
            staged,
            transcript_path=transcript_path,
            brief=brief.strip(),
            out_dir=staged.parent,
            live=live,
            project=project,
            html=html,
            passes=passes,
            taste_path=taste,
            apply=True,
            min_confidence=min_confidence,
            apply_passes=list(MECHANICAL),
            allow_empty_apply=True,
            skip_apply=(targets.clear if targets.configured() else None),
            signals=signals,
            transcribe=transcribe,
            signal_cache=signal_cache,
            global_taste_path=global_taste_path,
            feedback_path=feedback_path if number == 1 else None,
            learn_from=learn_from if number == 1 else None,
        )
        metrics = _metrics(report)
        cuts = _cuts(report, number)
        row = {
            "round": number,
            "source": str(staged),
            "shadow": str(report.out_fcpxml) if report.out_fcpxml else None,
            "applied_fcpxml": str(report.out_applied) if report.out_applied else None,
            "json": str(report.out_json) if report.out_json else None,
            "markdown": str(report.out_markdown) if report.out_markdown else None,
            "taste": str(report.out_taste) if report.out_taste else None,
            "applied_ids": [item["candidate_id"] for item in cuts],
            "cuts": cuts,
            "metrics": metrics,
            "mode": report.mode,
            "learned": report.payload.get("learned") or [],
            "next": str(report.out_applied or report.out_fcpxml or staged),
            "words": report.payload["files"].get("applied_words") or report.payload["files"].get("words"),
            "signals": _signal_summary(report),
        }
        rounds.append(row)
        applied.extend(cuts)
        warnings.extend(report.warnings)
        if targets.clear(metrics):
            reason = "metrics"
            cleared = targets.configured()
            break
        if report.cuts_applied == 0:
            reason = "no-progress"
            break
        if number == max_rounds:
            reason = "max-rounds"
            break
        source = Path(row["next"])
        if report.out_taste is not None:
            taste = report.out_taste

    human = _human(reason, rounds)
    result = IterateResult(
        stop_reason=reason,
        rounds=rounds,
        applied=applied,
        needs_human=bool(human),
        human_reasons=human,
        cleared=cleared,
        warnings=warnings,
        starter=starter,
        source=Path(fcpxml) if fcpxml else starter,
        signals_summary=(rounds[-1].get("signals") or {}).get("summary", "") if rounds else "",
    )
    payload = {
        "protocol": PROTOCOL,
        "protocol_version": PROTOCOL_VERSION,
        "conductor_version": __version__,
        "stop_reason": result.stop_reason,
        "needs_human": result.needs_human,
        "human_reasons": result.human_reasons,
        "cleared": result.cleared,
        "brief": brief.strip(),
        "max_rounds": max_rounds,
        "min_confidence": min_confidence,
        "passes": passes,
        "targets": targets.to_dict(),
        "source": str(result.source) if result.source else None,
        "starter": str(starter) if starter else None,
        "applied": applied,
        "rounds": rounds,
        "warnings": warnings,
        "signals": rounds[-1].get("signals") if rounds else None,
        "words": rounds[-1].get("words") if rounds else None,
    }
    out_json = destination / "iterate.json"
    out_json.write_text(dumps(payload), encoding="utf-8")
    result.out_json = out_json
    return result


def format_report(result: IterateResult) -> str:
    """The per-round table and the stop line."""
    lines = [
        f"cut-conductor: iterate, {len(result.rounds)} rounds, stop {result.stop_reason}",
        "round  duration  silence  review  escalate  avg_shot  cuts/min  applied",
    ]
    for row in result.rounds:
        metrics = row["metrics"]
        applied = ",".join(row["applied_ids"]) if row["applied_ids"] else "none"
        lines.append(
            f"{row['round']:<5}  "
            f"{metrics['duration_seconds']:>8.2f}  "
            f"{metrics['silence_seconds']:>7.2f}  "
            f"{metrics['review_count']:>6}  "
            f"{metrics['escalate_count']:>8}  "
            f"{metrics['average_shot_seconds']:>8.2f}  "
            f"{metrics['cuts_per_minute']:>8.2f}  "
            f"{applied}"
        )
    if result.signals_summary:
        lines.append(f"signals  {result.signals_summary}")
    if result.cleared:
        lines.append("clear  " + ", ".join(result.cleared))
    lines.append("human  " + (", ".join(result.human_reasons) if result.human_reasons else "none"))
    if result.starter is not None:
        lines.append(f"starter  {result.starter}")
    if result.out_json is not None:
        lines.append(f"json  {result.out_json}")
    return "\n".join(lines)


def _signal_summary(report: Report) -> dict:
    raw = report.payload.get("signals") or {}
    return {
        "audio": raw.get("audio"),
        "transcript": raw.get("transcript"),
        "whisper_tool": raw.get("whisper_tool"),
        "summary": raw.get("summary") or "",
        "word_count": raw.get("word_count", 0),
        "reasons": list(raw.get("reasons") or []),
        "unreachable": list(raw.get("unreachable") or []),
        "cache_hits": dict(raw.get("cache_hits") or {}),
    }


def _human(reason: str, rounds: list[dict]) -> list[str]:
    reasons: list[str] = []
    if rounds and int(rounds[-1]["metrics"]["escalate_count"]) > 0:
        reasons.append("escalate")
    if reason == "max-rounds":
        reasons.append("max-rounds")
    return reasons


def _metrics(report: Report) -> dict:
    path = report.out_applied or report.out_fcpxml
    if path is None:
        raise ConductorError("iterate round wrote no FCPXML to measure")
    document = parse_fcpxml(path)
    return measure(document.sequences, changes=report.changes)


def _cuts(report: Report, number: int) -> list[dict]:
    by_id = {item["id"]: item for item in report.candidates}
    rows: list[dict] = []
    for cut in report.payload.get("cuts") or []:
        candidate = by_id.get(cut["candidate_id"], {})
        rows.append(
            {
                "round": number,
                "candidate_id": cut["candidate_id"],
                "action": cut["action"],
                "pass": cut["pass"],
                "clip_name": candidate.get("clip_name"),
                "kind": candidate.get("kind"),
                "start_seconds": cut["start_seconds"],
                "end_seconds": cut["end_seconds"],
            }
        )
    return rows


def _stage(source: Path, round_dir: Path) -> Path:
    round_dir.mkdir(parents=True, exist_ok=True)
    dest = round_dir / "timeline.fcpxml"
    if dest.resolve() == source.resolve():
        raise ConductorError("refusing to stage a round onto its own input")
    dest.write_bytes(source.read_bytes())
    return dest


def _starter(
    media: str | Path,
    out_dir: Path,
    *,
    durations_path: str | Path | None,
    sequence: str | None,
) -> tuple[Path, list[str]]:
    """Same starter FCPXML ``ingest`` writes, without a second analyze."""
    overrides = load_duration_overrides(durations_path) if durations_path else None
    found = inventory(media, overrides=overrides)
    name = (sequence or found.media_dir.name).strip() or "Selects"
    starter = out_dir / "starter.fcpxml"
    starter_resolved = starter.resolve()
    for clip in found.clips:
        if starter_resolved == clip.path:
            raise ConductorError(
                "refusing to write the starter FCPXML over a source clip; "
                "choose a different --out-dir"
            )
    before = {clip.path: clip.blake2b for clip in found.clips}
    write_document(render_starter(found.clips, name=name), starter)
    for path, digest in before.items():
        if not path.is_file() or file_hash(path) != digest:
            raise ConductorError(f"refusing to finish: source media changed: {path}")
    return starter, list(found.warnings)
