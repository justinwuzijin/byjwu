"""Command line. ``python -m conductor analyze timeline.fcpxml --brief ...``"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .drop import output_dir_for
from .errors import ConductorError
from .ingest import DEFAULT_BRIEF, ingest
from .iterate import format_table, iterate
from .metrics import Targets
from .passes import PASSES
from .run import Report, analyze
from .taste import feedback_event, load_taste, write_taste


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="cut-conductor",
        description=(
            "Editorial co-pilot for Final Cut. Dry-run by default: "
            "proposal markers on a new FCPXML. The file you pass in, and any "
            "source clips, are never modified."
        ),
    )
    sub = parser.add_subparsers(dest="command", required=True)
    _add_analyze(sub.add_parser("analyze", help="shadow run: markers and a ranked report"))
    apply_parser = sub.add_parser(
        "apply",
        help="write a new FCPXML with accepted cuts performed",
    )
    _add_analyze(apply_parser)
    apply_parser.add_argument(
        "--accept",
        help="comma-separated candidate ids a person has accepted (raw tighten/remove)",
    )
    apply_parser.add_argument(
        "--min-confidence",
        type=float,
        help="with --pass, apply only auto-gated calls at or above this confidence",
    )
    _add_ingest(
        sub.add_parser(
            "ingest",
            help="build a starter FCPXML from a folder of clips, then shadow-mark it",
        )
    )
    _add_iterate(
        sub.add_parser(
            "iterate",
            help="repeat shadow plus mechanical auto-apply until the stop metrics clear",
        )
    )
    ui = sub.add_parser(
        "ui",
        help="developer page that runs ingest on a folder path (editors use the room)",
    )
    ui.add_argument("--port", type=int, default=8765, help="localhost port (default: 8765)")
    feedback = sub.add_parser("feedback", help="append an accept or reject to a taste log")
    feedback.add_argument("--taste", required=True, help="taste JSON to read")
    feedback.add_argument("--out", required=True, help="where to write the updated taste JSON")
    feedback.add_argument("--event", required=True, choices=("accept", "reject"))
    feedback.add_argument("--id", required=True, help="candidate id")
    feedback.add_argument("--action", required=True, help="the action the person judged")
    feedback.add_argument("--pass", dest="pass_name", required=True, help="pass that produced it")
    feedback.add_argument("--note", default="")
    args = parser.parse_args(argv)
    try:
        if args.command == "feedback":
            return _feedback(args)
        if args.command == "ui":
            return _ui(args)
        if args.command == "ingest":
            return _ingest(args)
        if args.command == "iterate":
            return _iterate(args)
        report = analyze(
            args.fcpxml,
            transcript_path=args.transcript,
            brief=args.brief,
            out_dir=args.out_dir,
            live=args.live,
            project=args.project,
            html=args.html,
            passes=args.passes,
            taste_path=args.taste,
            apply=args.command == "apply",
            accept=_accept(getattr(args, "accept", None)),
            min_confidence=getattr(args, "min_confidence", None),
        )
    except ConductorError as exc:
        print(f"cut-conductor: {exc}", file=sys.stderr)
        return 2
    _print_report(report)
    return 0


def _add_analyze(parser: argparse.ArgumentParser) -> None:
    ready = ", ".join(name for name, spec in PASSES.items() if spec.implemented)
    reserved = ", ".join(name for name, spec in PASSES.items() if not spec.implemented)
    parser.add_argument("fcpxml", help="FCPXML exported from Final Cut Pro")
    parser.add_argument("--transcript", help="optional SRT or WebVTT aligned to the sequence")
    parser.add_argument("--brief", required=True, help="what this cut is for")
    parser.add_argument("--out-dir", default="out", help="directory for new files (default: out)")
    parser.add_argument("--project", help="project name, when the XML holds more than one")
    parser.add_argument(
        "--pass",
        dest="passes",
        action="append",
        help=f"run one pass (repeatable). Ready: {ready}. Reserved: {reserved}.",
    )
    parser.add_argument("--taste", help="taste JSON (prefs, gates, accept/reject log)")
    parser.add_argument(
        "--live",
        action="store_true",
        help="call Jev. Requires OPENROUTER_API_KEY or TYPESAFE_API_KEY. Off by default.",
    )
    parser.add_argument("--html", action="store_true", help="also write a single-file HTML report")


def _add_ingest(parser: argparse.ArgumentParser) -> None:
    ready = ", ".join(name for name, spec in PASSES.items() if spec.implemented)
    parser.add_argument("--media", required=True, help="folder of video clips (not searched recursively)")
    parser.add_argument(
        "--brief",
        help=f"what this cut is for (default: {DEFAULT_BRIEF})",
    )
    parser.add_argument("--transcript", help="optional SRT or WebVTT aligned to the sequence")
    parser.add_argument("--out-dir", default="out", help="directory for new files (default: out)")
    parser.add_argument("--sequence", help="project name (default: the folder name)")
    parser.add_argument(
        "--durations",
        help="JSON object of file name to length (8, 8s, or 1/8s). Wins over ffprobe.",
    )
    parser.add_argument(
        "--pass",
        dest="passes",
        action="append",
        help=f"run one pass (repeatable). Ready: {ready}.",
    )
    parser.add_argument("--taste", help="taste JSON (prefs, gates, accept/reject log)")
    parser.add_argument(
        "--live",
        action="store_true",
        help="call Jev. Requires OPENROUTER_API_KEY or TYPESAFE_API_KEY. Off by default.",
    )
    parser.add_argument("--html", action="store_true", help="also write a single-file HTML report")
    parser.add_argument(
        "--apply",
        action="store_true",
        help="also write an applied FCPXML, with the same gates as the apply command",
    )
    parser.add_argument(
        "--accept",
        help="with --apply, comma-separated candidate ids a person has accepted",
    )
    parser.add_argument(
        "--min-confidence",
        type=float,
        help="with --apply and --pass, apply only auto-gated calls at or above this confidence",
    )


def _add_iterate(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--drop",
        help="folder the editor dropped. One FCPXML, or video files in that folder.",
    )
    parser.add_argument("--fcpxml", help="an export, when there is no drop folder")
    parser.add_argument("--media", help="a folder of clips, when there is no drop folder")
    parser.add_argument("--brief", help="overrides brief.txt in a drop folder")
    parser.add_argument("--transcript", help="optional SRT or WebVTT aligned to the sequence")
    parser.add_argument("--taste", help="taste JSON (prefs, gates, accept/reject log)")
    parser.add_argument(
        "--durations",
        help="JSON object of file name to length. Used when the drop is a folder of clips.",
    )
    parser.add_argument(
        "--out-dir",
        help="where to write. Default for a jevid-in drop is the sibling jevid-out; otherwise out.",
    )
    parser.add_argument("--max-rounds", type=int, default=5, help="stop after this many rounds (default: 5)")
    parser.add_argument("--target-seconds", type=float, help="duration the handback should land near")
    parser.add_argument(
        "--duration-tolerance",
        type=float,
        default=2.0,
        help="seconds either side of --target-seconds that still count (default: 2)",
    )
    parser.add_argument(
        "--max-silence",
        type=float,
        default=1.25,
        help="stop when total gap time is at or under this many seconds (default: 1.25)",
    )
    parser.add_argument(
        "--max-escalations",
        type=int,
        default=0,
        help="stop when open escalations are at or under this count (default: 0)",
    )
    parser.add_argument(
        "--max-reviews",
        type=int,
        help="when set, stop only if open reviews are at or under this count",
    )
    parser.add_argument("--min-shot", type=float, help="when set, average shot length must be at least this")
    parser.add_argument("--max-shot", type=float, help="when set, average shot length must be at most this")
    parser.add_argument("--min-cpm", type=float, help="when set, cuts per minute must be at least this")
    parser.add_argument("--max-cpm", type=float, help="when set, cuts per minute must be at most this")
    parser.add_argument(
        "--min-confidence",
        type=float,
        help="mechanical auto-apply floor (default: the taste gate, 0.80)",
    )
    parser.add_argument(
        "--live",
        action="store_true",
        help="call Jev. Requires OPENROUTER_API_KEY or TYPESAFE_API_KEY. Off by default.",
    )


def _iterate(args) -> int:
    if not args.drop and not args.fcpxml and not args.media:
        raise ConductorError("iterate needs --drop, --fcpxml, or --media")
    if args.drop and not args.out_dir:
        out_dir = output_dir_for(Path(args.drop))
    else:
        out_dir = args.out_dir or "out"
    targets = Targets(
        target_seconds=args.target_seconds,
        duration_tolerance=args.duration_tolerance,
        max_silence=args.max_silence,
        max_escalations=args.max_escalations,
        max_reviews=args.max_reviews,
        min_shot=args.min_shot,
        max_shot=args.max_shot,
        min_cuts_per_minute=args.min_cpm,
        max_cuts_per_minute=args.max_cpm,
    )
    result = iterate(
        out_dir=out_dir,
        brief=args.brief,
        fcpxml=args.fcpxml,
        media_dir=args.media,
        drop=args.drop,
        transcript_path=args.transcript,
        taste_path=args.taste,
        durations_path=args.durations,
        max_rounds=args.max_rounds,
        live=args.live,
        targets=targets,
        min_confidence=args.min_confidence,
    )
    print(format_table(result))
    if result.summary_json:
        print(f"  summary {result.summary_json}")
    if result.reason == "error":
        print(f"cut-conductor: {result.error}", file=sys.stderr)
        return 2
    return 0


def _accept(value: str | None) -> list[str] | None:
    if not value:
        return None
    ids = [part.strip() for part in value.split(",") if part.strip()]
    if not ids:
        raise ConductorError("--accept did not contain any candidate ids")
    return ids


def _ingest(args) -> int:
    accept = _accept(args.accept)
    if not args.apply and (accept or args.min_confidence is not None):
        raise ConductorError(
            "--accept and --min-confidence require --apply. The default is shadow markers."
        )
    result = ingest(
        args.media,
        brief=args.brief,
        transcript_path=args.transcript,
        taste_path=args.taste,
        durations_path=args.durations,
        out_dir=args.out_dir,
        name=args.sequence,
        live=args.live,
        html=args.html,
        passes=args.passes,
        apply=args.apply,
        accept=accept,
        min_confidence=args.min_confidence,
    )
    _print_report(result.report, starter=result.starter)
    for warning in result.warnings:
        print(f"  warning {warning}", file=sys.stderr)
    return 0


def _ui(args) -> int:
    from .ui import serve

    serve(args.port)
    return 0


def _print_report(report: Report, starter: Path | None = None) -> None:
    changes = len(report.changes)
    print(
        f"cut-conductor: {report.mode}, {len(report.candidates)} candidates, "
        f"{report.marker_count} markers added, {changes} ranked changes, "
        f"{report.cuts_applied} cuts written"
    )
    if starter is not None:
        print(f"  starter {starter}")
    if report.out_fcpxml:
        print(f"  shadow  {report.out_fcpxml}")
    if report.out_applied:
        print(f"  applied {report.out_applied}")
    if report.out_markdown:
        print(f"  report  {report.out_markdown}")
    if report.out_json:
        print(f"  json    {report.out_json}")
    if report.out_taste:
        print(f"  taste   {report.out_taste}")
    if report.out_html:
        print(f"  html    {report.out_html}")


def _feedback(args) -> int:
    taste = load_taste(args.taste)
    taste.append(
        feedback_event(
            event=args.event,
            candidate_id=args.id,
            action=args.action,
            pass_name=args.pass_name,
            note=args.note,
        )
    )
    destination = Path(args.out)
    if destination.resolve() == Path(args.taste).resolve():
        raise ConductorError("refusing to overwrite the taste file; pass a different --out")
    write_taste(taste, destination)
    print(f"cut-conductor: wrote {args.event} for {args.id} to {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
