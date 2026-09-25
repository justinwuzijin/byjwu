"""Command line. ``python -m conductor analyze timeline.fcpxml --brief ...``"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .errors import ConductorError
from .feedback import apply_note_items, bind_pending, diff_fcpxml, load_notes
from .fcpxml import parse_fcpxml
from .ingest import DEFAULT_BRIEF, ingest
from .iterate import format_report, iterate
from .passes import PASSES, collect
from .room import room_run, watch
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
            help="repeat analyze and auto-apply mechanical cuts until the cut holds",
        )
    )
    _add_room(
        sub.add_parser(
            "room-run",
            help="bot entry: detect a drop, iterate, and write a chat summary",
        )
    )
    ui = sub.add_parser("ui", help="local page that runs ingest on a folder path")
    ui.add_argument("--port", type=int, default=8765, help="localhost port (default: 8765)")
    feedback = sub.add_parser(
        "feedback",
        help="record an accept/reject, a notes file, or a diff against an editor re-export",
    )
    feedback.add_argument("--taste", required=True, help="project taste JSON to read")
    feedback.add_argument("--out", required=True, help="where to write the updated project taste JSON")
    feedback.add_argument("--global-taste", help="optional global profile to read")
    feedback.add_argument(
        "--global-out",
        help="where to write global-scoped notes; required when a note says scope=global",
    )
    feedback.add_argument("--event", choices=("accept", "reject", "modify", "extra"))
    feedback.add_argument("--id", help="candidate id, with --event")
    feedback.add_argument("--action", help="the action the person judged, with --event")
    feedback.add_argument("--pass", dest="pass_name", help="pass that produced it, with --event")
    feedback.add_argument("--note", default="")
    feedback.add_argument("--kind", help="candidate kind, with --event")
    feedback.add_argument("--notes", help="structured feedback JSON from a room bot")
    feedback.add_argument("--fcpxml", help="timeline used to bind at= timecodes in --notes")
    feedback.add_argument("--proposed", help="conductor shadow FCPXML, with --edited")
    feedback.add_argument("--edited", help="editor re-export, with --proposed")
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
        if args.command == "room-run":
            return _room(args)
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
            global_taste_path=args.global_taste,
            feedback_path=args.feedback,
            learn_from=args.learn_from,
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
    parser.add_argument("--taste", help="project taste JSON (prefs, gates, feedback log)")
    parser.add_argument(
        "--global-taste",
        help="optional global taste profile, read-only; project keys win",
    )
    parser.add_argument(
        "--feedback",
        help="notes JSON a room bot wrote from chat (timecodes and standing rules)",
    )
    parser.add_argument(
        "--learn-from",
        help="previous conductor shadow FCPXML; the main file is the editor re-export",
    )
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
    ready = ", ".join(name for name, spec in PASSES.items() if spec.implemented)
    parser.add_argument("--fcpxml", help="FCPXML to iterate. Not with --media.")
    parser.add_argument(
        "--media",
        help="folder of clips. Writes a starter FCPXML, then iterates. Not with --fcpxml.",
    )
    parser.add_argument("--brief", required=True, help="what this cut is for")
    parser.add_argument("--transcript", help="optional SRT or WebVTT aligned to the sequence")
    parser.add_argument("--out-dir", default="out", help="directory for vN/ rounds (default: out)")
    parser.add_argument("--project", help="project name, when the XML holds more than one")
    parser.add_argument("--sequence", help="with --media, project name (default: the folder name)")
    parser.add_argument(
        "--durations",
        help="with --media, JSON object of file name to length (8, 8s, or 1/8s)",
    )
    parser.add_argument(
        "--pass",
        dest="passes",
        action="append",
        help=f"passes to judge (repeatable). Default includes colour. Ready: {ready}.",
    )
    parser.add_argument("--taste", help="project taste JSON carried into round 1; later rounds use the written log")
    parser.add_argument("--global-taste", help="optional global taste profile, read on every round")
    parser.add_argument("--feedback", help="notes JSON folded into round 1 before judging")
    parser.add_argument(
        "--learn-from",
        help="previous conductor shadow FCPXML; --fcpxml is the editor re-export. Round 1 only.",
    )
    parser.add_argument(
        "--live",
        action="store_true",
        help="call Jev. Requires OPENROUTER_API_KEY or TYPESAFE_API_KEY. Off by default.",
    )
    parser.add_argument("--html", action="store_true", help="also write a single-file HTML report each round")
    parser.add_argument("--max-rounds", type=int, default=5, help="stop after this many rounds (default: 5)")
    parser.add_argument(
        "--min-confidence",
        type=float,
        default=0.8,
        help="auto-apply mechanical calls at or above this confidence (default: 0.8)",
    )
    parser.add_argument("--target-seconds", type=float, help="stop when duration is within --tolerance of this")
    parser.add_argument(
        "--tolerance",
        type=float,
        default=1.0,
        help="seconds of slack around --target-seconds (default: 1)",
    )
    parser.add_argument("--max-escalate", type=int, help="stop when escalate rows are at or under this")
    parser.add_argument("--max-review", type=int, help="stop when review rows are at or under this")
    parser.add_argument(
        "--max-silence-seconds",
        type=float,
        help="stop when silence-gap candidates sum to at most this many seconds",
    )
    parser.add_argument(
        "--min-shot-seconds",
        type=float,
        help="stop when the average non-gap spine clip is at least this long",
    )
    parser.add_argument(
        "--max-cuts-per-minute",
        type=float,
        help="stop when spine joins per minute are at or under this",
    )


def _add_room(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "path",
        help="a .fcpxml, .fcpxmld, .zip of either, or a folder of clips. With --watch, the drop folder.",
    )
    parser.add_argument(
        "--watch",
        action="store_true",
        help="keep running, and process new drops in this folder once each file has finished copying",
    )
    parser.add_argument("--brief", help="what this cut is for (default: a filename-order assembly line)")
    parser.add_argument(
        "--out-root",
        default=str(Path.home() / "Desktop" / "jevid-out"),
        help="folder for timestamped results (default: ~/Desktop/jevid-out)",
    )
    parser.add_argument("--transcript", help="SRT or WebVTT. Default: one sitting next to the timeline")
    parser.add_argument("--taste", help="taste JSON carried into round 1")
    parser.add_argument(
        "--durations",
        help="for a clip folder, JSON object of file name to length. Default: durations.json in the folder",
    )
    parser.add_argument("--max-rounds", type=int, default=5, help="stop after this many rounds (default: 5)")
    parser.add_argument(
        "--style",
        help="assembly style when the drop has music and an assembler is installed (default: byjustinwu)",
    )
    parser.add_argument(
        "--live",
        action="store_true",
        help="call Jev. Requires OPENROUTER_API_KEY or TYPESAFE_API_KEY. Off by default.",
    )
    parser.add_argument(
        "--stable-seconds",
        type=float,
        default=2.0,
        help="with --watch, wait until a drop's size is unchanged for this long (default: 2)",
    )
    parser.add_argument(
        "--poll-seconds",
        type=float,
        default=1.0,
        help="with --watch, seconds between scans (default: 1)",
    )


def _room(args) -> int:
    if args.stable_seconds < 0 or args.poll_seconds < 0:
        raise ConductorError("--stable-seconds and --poll-seconds must be >= 0")
    if args.watch:
        return _watch_room(args)
    result = room_run(
        args.path,
        out_root=args.out_root,
        brief=args.brief,
        live=args.live,
        transcript=args.transcript,
        taste=args.taste,
        durations=args.durations,
        max_rounds=args.max_rounds,
        style=args.style,
    )
    print(result.markdown, end="" if result.markdown.endswith("\n") else "\n")
    return 0


def _watch_room(args) -> int:
    def _show(event) -> None:
        if event.status == "ran":
            print(event.message, end="" if str(event.message).endswith("\n") else "\n")
        elif event.status == "error":
            print(f"cut-conductor: {event.message}", file=sys.stderr)

    try:
        watch(
            args.path,
            out_root=args.out_root,
            brief=args.brief,
            live=args.live,
            transcript=args.transcript,
            taste=args.taste,
            durations=args.durations,
            max_rounds=args.max_rounds,
            style=args.style,
            stable_seconds=args.stable_seconds,
            poll_seconds=args.poll_seconds,
            on_event=_show,
        )
    except KeyboardInterrupt:
        print("cut-conductor: room-run watch stopped", file=sys.stderr)
        return 0
    return 0


def _iterate(args) -> int:
    result = iterate(
        fcpxml=args.fcpxml,
        media=args.media,
        brief=args.brief,
        transcript_path=args.transcript,
        taste_path=args.taste,
        durations_path=args.durations,
        sequence=args.sequence,
        project=args.project,
        passes=args.passes,
        out_dir=args.out_dir,
        live=args.live,
        html=args.html,
        max_rounds=args.max_rounds,
        min_confidence=args.min_confidence,
        target_seconds=args.target_seconds,
        tolerance=args.tolerance,
        max_escalate=args.max_escalate,
        max_review=args.max_review,
        max_silence_seconds=args.max_silence_seconds,
        min_shot_seconds=args.min_shot_seconds,
        max_cuts_per_minute=args.max_cuts_per_minute,
        global_taste_path=args.global_taste,
        feedback_path=args.feedback,
        learn_from=args.learn_from,
    )
    print(format_report(result))
    for warning in result.warnings:
        print(f"  warning {warning}", file=sys.stderr)
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
    learned = report.payload.get("learned") or []
    if learned:
        print(f"  learned {len(learned)} feedback event(s)")


def _feedback(args) -> int:
    if bool(args.proposed) != bool(args.edited):
        raise ConductorError("--proposed and --edited are used together")
    modes = [bool(args.event), bool(args.notes), bool(args.proposed)]
    if sum(modes) != 1:
        raise ConductorError("feedback needs one of --event, --notes, or --proposed with --edited")
    destination = Path(args.out)
    _refuse_same(destination, args.taste, "taste file")
    taste = load_taste(args.taste, global_path=args.global_taste)
    candidates = _feedback_candidates(args.fcpxml) if args.fcpxml else None
    warnings: list[str] = []
    if args.event:
        if not args.id or not args.action or not args.pass_name:
            raise ConductorError("--event needs --id, --action, and --pass")
        fields = {"source": "feedback"}
        if args.kind:
            fields["kind"] = args.kind
        row = feedback_event(
            event=args.event,
            candidate_id=args.id,
            action=args.action,
            pass_name=args.pass_name,
            note=args.note,
            fields=fields,
        )
        taste.append(row)
        summary = f"wrote {args.event} for {args.id}"
    elif args.proposed:
        events, warnings = diff_fcpxml(args.proposed, args.edited)
        added = 0
        for event in events:
            if taste.append(event):
                added += 1
        summary = f"learned {added} event(s) from the re-export"
    else:
        project_items, global_items = _split_scope(load_notes(args.notes))
        if global_items:
            _write_global_notes(args, global_items, candidates)
        noted, warnings = apply_note_items(taste, project_items)
        if candidates is not None:
            bound, bind_warnings = bind_pending(taste, candidates)
            noted = [*noted, *bound]
            warnings = [*warnings, *bind_warnings]
        summary = f"learned {len(noted)} note(s)"
        if taste.pending:
            summary += f", {len(taste.pending)} still pending"
    write_taste(taste, destination)
    print(f"cut-conductor: {summary} to {args.out}")
    for warning in warnings:
        print(f"  warning {warning}", file=sys.stderr)
    return 0


def _feedback_candidates(path: str):
    document = parse_fcpxml(path)
    return collect(document.sequences, [], transcript_present=False)


def _split_scope(items: list[dict]) -> tuple[list[dict], list[dict]]:
    project, glob = [], []
    for item in items:
        if item.get("scope") == "global":
            glob.append(item)
        else:
            project.append(item)
    return project, glob


def _write_global_notes(args, items: list[dict], candidates) -> None:
    if not args.global_out:
        raise ConductorError("a note has scope global; pass --global-out")
    if args.global_taste:
        _refuse_same(Path(args.global_out), args.global_taste, "global taste file")
    cleaned = [{key: value for key, value in item.items() if key != "scope"} for item in items]
    glob = load_taste(args.global_taste) if args.global_taste else load_taste(None)
    apply_note_items(glob, cleaned)
    if candidates is not None:
        bind_pending(glob, candidates)
    write_taste(glob, Path(args.global_out))
    print(f"cut-conductor: wrote {len(cleaned)} global note(s) to {args.global_out}")


def _refuse_same(destination: Path, source: str, label: str) -> None:
    if destination.resolve() == Path(source).resolve():
        raise ConductorError(f"refusing to overwrite the {label}; pass a different output path")


if __name__ == "__main__":
    raise SystemExit(main())
