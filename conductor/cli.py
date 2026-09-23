"""Command line. ``python -m conductor analyze timeline.fcpxml --brief ...``"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .errors import ConductorError
from .ingest import DEFAULT_BRIEF, ingest
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
    ui = sub.add_parser("ui", help="local page that runs ingest on a folder path")
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
