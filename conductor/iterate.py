"""Iterate a timeline until the stop metrics clear, or the loop should halt.

The Conductor bot runs this after a room drop. Each round analyzes every
pass, then auto-applies only mechanical calls that clear the confidence
gate. Dialogue, pacing, and any other creative pass stay in review.

Versioned output lives in ``out/v1``, ``out/v2``, …. The file handed back
to the room is the applied FCPXML when that round cut, otherwise the shadow
file. The editor's original is never written.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from .drop import Drop, output_dir_for, resolve_drop
from .errors import ConductorError
from .ingest import DEFAULT_BRIEF, ingest
from .metrics import Targets, measure, metric_key
from .report import dumps
from .run import Report, analyze
from .taste import load_taste, write_taste

MECHANICAL = ["mechanical"]


@dataclass
class RoundRecord:
    number: int
    report: Report
    metrics: dict
    failures: list[str]
    applied: int
    handback: Path

    def to_state(self) -> dict:
        return {
            "round": self.number,
            "applied": self.applied,
            "failures": list(self.failures),
            "metrics": dict(self.metrics),
            "handback": str(self.handback),
            "shadow": str(self.report.out_fcpxml) if self.report.out_fcpxml else None,
            "applied_fcpxml": str(self.report.out_applied) if self.report.out_applied else None,
            "json": str(self.report.out_json) if self.report.out_json else None,
            "markdown": str(self.report.out_markdown) if self.report.out_markdown else None,
        }


@dataclass
class IterateResult:
    rounds: list[RoundRecord]
    reason: str
    handback: Path | None
    summary_json: Path | None = None
    summary_markdown: Path | None = None
    starter: Path | None = None
    error: str | None = None
    warnings: list[str] = field(default_factory=list)

    def to_state(self) -> dict:
        return {
            "protocol": "cut-conductor.iterate",
            "reason": self.reason,
            "handback": str(self.handback) if self.handback else None,
            "starter": str(self.starter) if self.starter else None,
            "error": self.error,
            "warnings": list(self.warnings),
            "rounds": [item.to_state() for item in self.rounds],
        }


def iterate(
    *,
    out_dir: str | Path,
    brief: str | None = None,
    fcpxml: str | Path | None = None,
    media_dir: str | Path | None = None,
    drop: str | Path | None = None,
    transcript_path: str | Path | None = None,
    taste_path: str | Path | None = None,
    durations_path: str | Path | None = None,
    max_rounds: int = 5,
    live: bool = False,
    targets: Targets | None = None,
    min_confidence: float | None = None,
) -> IterateResult:
    """Run up to ``max_rounds`` analyze/apply cycles.

    Provide ``fcpxml``, ``media_dir``, or ``drop``. A drop folder supplies
    the rest when those arguments are omitted. ``--brief`` wins over
    ``brief.txt``.
    """
    if max_rounds < 1:
        raise ConductorError("--max-rounds must be at least 1")
    warnings: list[str] = []
    resolved: Drop | None = None
    if drop is not None:
        if fcpxml or media_dir:
            raise ConductorError("pass --drop, or pass --fcpxml/--media, not both")
        resolved = resolve_drop(drop)
        fcpxml = fcpxml or resolved.fcpxml
        media_dir = media_dir or resolved.media_dir
        if transcript_path is None:
            transcript_path = resolved.transcript
        if taste_path is None:
            taste_path = resolved.taste
        if durations_path is None:
            durations_path = resolved.durations
        if not brief and resolved.brief:
            brief = resolved.brief
    destination = Path(out_dir)
    if resolved is not None and str(out_dir) == str(output_dir_for(resolved.folder)):
        destination = output_dir_for(resolved.folder)
    destination.mkdir(parents=True, exist_ok=True)
    text = brief.strip() if brief and brief.strip() else DEFAULT_BRIEF
    targets = targets or Targets()
    current, starter = _starting_timeline(
        destination,
        fcpxml=fcpxml,
        media_dir=media_dir,
        brief=text,
        transcript_path=transcript_path,
        taste_path=taste_path,
        durations_path=durations_path,
        live=live,
        warnings=warnings,
    )
    original = Path(fcpxml).resolve() if fcpxml else None
    taste_in = Path(taste_path) if taste_path else None
    floor = min_confidence
    if floor is None:
        floor = load_taste(taste_in).gates.auto_confidence

    records: list[RoundRecord] = []
    reason = "max-rounds"
    error: str | None = None
    previous: tuple | None = None
    try:
        for number in range(1, max_rounds + 1):
            record = _round(
                number,
                current,
                destination / f"v{number}",
                brief=text,
                transcript_path=transcript_path,
                taste_path=taste_in,
                live=live,
                targets=targets,
                min_confidence=floor,
                original=original,
            )
            records.append(record)
            taste_in = record.report.out_taste
            if not record.failures:
                reason = "metrics"
                break
            if record.applied == 0 and previous is not None and metric_key(record.metrics) == previous:
                reason = "no-progress"
                break
            previous = metric_key(record.metrics)
            current = record.handback
            if number == max_rounds:
                reason = "max-rounds"
    except ConductorError as exc:
        reason = "error"
        error = str(exc)
        result = IterateResult(records, reason, _handback(records), starter=starter, error=error, warnings=warnings)
        _write_summary(result, destination)
        return result

    result = IterateResult(
        records,
        reason,
        _handback(records),
        starter=starter,
        warnings=warnings,
    )
    _write_summary(result, destination)
    return result


def format_table(result: IterateResult) -> str:
    header = (
        f"{'round':>5}  {'duration':>8}  {'silence':>7}  {'reviews':>7}  "
        f"{'escalations':>11}  {'avg_shot':>8}  {'cpm':>6}  {'applied':>7}"
    )
    lines = [header]
    for record in result.rounds:
        metrics = record.metrics
        lines.append(
            f"{record.number:5d}  "
            f"{metrics['duration_seconds']:8.2f}  "
            f"{metrics['silence_seconds']:7.2f}  "
            f"{metrics['reviews']:7d}  "
            f"{metrics['escalations']:11d}  "
            f"{metrics['avg_shot_seconds']:8.2f}  "
            f"{metrics['cuts_per_minute']:6.2f}  "
            f"{record.applied:7d}"
        )
    lines.append(f"stopped because {result.reason}")
    if result.handback is not None:
        lines.append(f"handback {result.handback}")
    if result.starter is not None:
        lines.append(f"starter {result.starter}")
    for warning in result.warnings:
        lines.append(f"warning {warning}")
    return "\n".join(lines)


def _round(
    number: int,
    source: Path,
    round_dir: Path,
    *,
    brief: str,
    transcript_path: str | Path | None,
    taste_path: Path | None,
    live: bool,
    targets: Targets,
    min_confidence: float,
    original: Path | None,
) -> RoundRecord:
    round_dir.mkdir(parents=True, exist_ok=True)
    copied = round_dir / "input.fcpxml"
    if original is not None and copied.resolve() == original:
        raise ConductorError("refusing to copy the iterate input onto itself")
    copied.write_bytes(source.read_bytes())
    if original is not None and source.resolve() == original:
        if source.read_bytes() != copied.read_bytes():
            raise ConductorError("refusing to finish: the source FCPXML changed while copying")
    report = analyze(
        copied,
        transcript_path=transcript_path,
        brief=brief,
        out_dir=round_dir,
        live=live,
        taste_path=taste_path,
        apply=True,
        min_confidence=min_confidence,
        apply_passes=MECHANICAL,
        allow_empty=True,
    )
    _refuse_original(original, report)
    if report.out_taste is not None:
        taste = load_taste(report.out_taste)
        applied_ids = {cut["candidate_id"] for cut in report.payload.get("cuts") or []}
        taste.remember(report.changes, round_n=number, applied_ids=applied_ids)
        write_taste(taste, report.out_taste)
    handback = report.out_applied or report.out_fcpxml
    if handback is None:
        raise ConductorError(f"round {number} did not write an FCPXML")
    reviews, escalations = _open_counts(report)
    metrics = measure(handback, reviews=reviews, escalations=escalations)
    failures = targets.failures(metrics)
    if original is not None and original.exists() and source.resolve() == original:
        pass
    return RoundRecord(number, report, metrics, failures, report.cuts_applied, handback)


def _starting_timeline(
    destination: Path,
    *,
    fcpxml: str | Path | None,
    media_dir: str | Path | None,
    brief: str,
    transcript_path: str | Path | None,
    taste_path: str | Path | None,
    durations_path: str | Path | None,
    live: bool,
    warnings: list[str],
) -> tuple[Path, Path | None]:
    if fcpxml is None and media_dir is None:
        raise ConductorError("iterate needs an FCPXML, a media folder, or a drop folder")
    if fcpxml is not None and media_dir is not None:
        raise ConductorError("iterate takes an FCPXML or a media folder, not both")
    if fcpxml is not None:
        path = Path(fcpxml)
        if not path.is_file():
            raise ConductorError(f"no such FCPXML: {path}")
        return path.resolve(), None
    result = ingest(
        media_dir,
        brief=brief,
        transcript_path=transcript_path,
        taste_path=taste_path,
        durations_path=durations_path,
        out_dir=destination / "starter",
        live=live,
        apply=False,
    )
    warnings.extend(result.warnings)
    return result.starter, result.starter


def _open_counts(report: Report) -> tuple[int, int]:
    applied_ids = {cut["candidate_id"] for cut in report.payload.get("cuts") or []}
    reviews = 0
    escalations = 0
    for row in report.changes:
        if row.get("candidate_id") in applied_ids:
            continue
        if row.get("section") == "review":
            reviews += 1
        elif row.get("section") == "escalate":
            escalations += 1
    return reviews, escalations


def _handback(records: list[RoundRecord]) -> Path | None:
    if not records:
        return None
    return records[-1].handback


def _refuse_original(original: Path | None, report: Report) -> None:
    if original is None:
        return
    for path in (report.out_fcpxml, report.out_applied, report.out_json):
        if path is not None and path.resolve() == original:
            raise ConductorError("refusing to overwrite the iterate input FCPXML")


def _write_summary(result: IterateResult, destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    payload = result.to_state()
    json_path = destination / "iterate.json"
    md_path = destination / "iterate.md"
    json_path.write_text(dumps(payload), encoding="utf-8")
    md_path.write_text(_markdown(result), encoding="utf-8")
    result.summary_json = json_path
    result.summary_markdown = md_path


def _markdown(result: IterateResult) -> str:
    lines = [
        "# Cut Conductor iterate",
        "",
        f"Stopped because **{result.reason}**.",
        "",
        "The editor opens the handback in Final Cut. This file is what the bot posts.",
        "",
        format_table(result),
        "",
    ]
    if result.rounds:
        last = result.rounds[-1]
        if last.failures:
            lines.append("Still open: " + ", ".join(last.failures) + ".")
            lines.append("")
    lines.append(
        "Mechanical auto-apply uses the same gate as "
        "`apply --min-confidence --pass mechanical`. "
        "Creative passes are not cut by this loop."
    )
    lines.append("")
    return "\n".join(lines)
