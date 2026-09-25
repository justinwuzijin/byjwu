"""One bot-facing run: detect a drop, call ``iterate``, write a chat summary.

``python -m conductor room-run <path>`` accepts a ``.fcpxml``, a ``.fcpxmld``
bundle, a ``.zip`` of either, or a folder of clips. Timelines go through
``iterate``. A clip folder goes through the same loop, which writes the
starter FCPXML and then iterates. The drop is only read.

A drop with music files (in the folder, or named by the timeline's assets)
goes to an assembler first when one is available (``register_assembler``, or
``conductor.assemble.assemble``), with ``--style`` (default ``byjustinwu``).
The FCPXML it returns is iterated. Without an assembler the run falls back to
the flow above and says so in the summary.

One :class:`conductor.router.Router` serves the whole run. The assembler gets
it as ``router`` when its signature takes that keyword, so its linear calls
reach Jev and its creative calls reach Opus through the same cache and
fallbacks as the passes. ``room.json`` has the combined ``decision_usage``.

Each run writes a new timestamped folder under ``--out-root`` (default
``~/Desktop/byjwu-out``). ``room.md`` is the chat text. ``room.json`` is the
same summary. The shadow FCPXML from the last round is always on disk; the
summary names the file to import in Final Cut.

``--watch`` polls a drop folder, waits until a new item's size stops
changing, skips digests it has already recorded, and appends ``room-run.log``.
"""

from __future__ import annotations

import hashlib
import importlib
import inspect
import json
import os
import re
import stat
import time
import zipfile
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from fractions import Fraction
from pathlib import Path
from urllib.parse import unquote, urlparse

from . import __version__
from .errors import ConductorError
from .fcpxml import Document, parse_fcpxml
from .folders import DROP_OUT, drop_folder
from .ingest import DEFAULT_BRIEF, VIDEO_EXTENSIONS
from .iterate import IterateResult, iterate
from .jev import dry_run_forced
from .metrics import measure
from .report import dumps
from .router import Ledger, Router, format_usage
from .rules import format_rules
from .timeutil import clock

PROTOCOL = "cut-conductor.room-run"
PROTOCOL_VERSION = 1
SUPPORTED_FCPXML = ("1.8", "1.9", "1.10", "1.11", "1.12", "1.13", "1.14")
LEDGER_NAME = ".room-run.json"
LOG_NAME = "room-run.log"

MUSIC_EXTENSIONS = frozenset({".mp3", ".wav", ".aif", ".aiff", ".m4a", ".aac", ".flac", ".caf"})
DEFAULT_STYLE = "byjustinwu"

_STEM_RE = re.compile(r"[^A-Za-z0-9._-]+")
_RESULT_KEYS = ("fcpxml", "out_fcpxml", "timeline", "path")
_ASSEMBLER: Callable[..., object] | None = None


@dataclass
class Prepared:
    kind: str
    contained: str | None
    given: Path
    origin: Path
    fcpxml: Path | None = None
    media: Path | None = None
    transcript: Path | None = None
    durations: Path | None = None
    local_root: Path | None = None
    warnings: list[str] = field(default_factory=list)
    media_present: bool = False
    media_note: str | None = None
    music: list[Path] = field(default_factory=list)
    flow: str = "iterate"
    style: str | None = None
    assembled: Path | None = None
    timeline_hint: Path | None = None


@dataclass
class RoomRun:
    ok: bool
    out_dir: Path
    markdown: str
    payload: dict
    open_in_final_cut: Path | None = None


@dataclass(frozen=True)
class Drop:
    """One inbox item. Loose clips in the inbox share a single media drop."""

    path: Path
    force_media: bool = False
    members: tuple[Path, ...] = ()


@dataclass
class WatchEvent:
    path: Path
    status: str
    message: str
    out_dir: Path | None = None


@dataclass
class WatchState:
    pending: dict[str, tuple] = field(default_factory=dict)
    announced: set[str] = field(default_factory=set)
    seen: dict = field(default_factory=dict)

    @classmethod
    def load(cls, out_root: Path) -> WatchState:
        file = Path(out_root) / LEDGER_NAME
        if not file.is_file():
            return cls()
        try:
            payload = json.loads(file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return cls()
        seen = payload.get("seen") if isinstance(payload, dict) else None
        if not isinstance(seen, dict):
            return cls()
        return cls(seen=seen)

    def save(self, out_root: Path) -> None:
        root = Path(out_root)
        root.mkdir(parents=True, exist_ok=True)
        payload = {"version": 1, "seen": self.seen}
        (root / LEDGER_NAME).write_text(dumps(payload), encoding="utf-8")


def room_run(
    path: str | Path,
    *,
    out_root: str | Path | None = None,
    brief: str | None = None,
    live: bool = False,
    transcript: str | Path | None = None,
    taste: str | Path | None = None,
    durations: str | Path | None = None,
    max_rounds: int = 5,
    force_media: bool = False,
    style: str | None = None,
    graphics: bool | None = None,
    beats: str | Path | None = None,
    now: datetime | None = None,
) -> RoomRun:
    """Detect ``path``, run the iterate loop, and write ``room.md`` plus ``room.json``.

    Dry-run unless ``live`` is set. The path that was passed in is not modified.
    A second call writes a new folder. When the drop carries music and an
    assembler is available, the assembled timeline is what gets iterated.
    """
    given = Path(path).expanduser()
    if not given.exists():
        raise ConductorError(_missing_path(given))
    source = given.resolve()
    root = _out_root(out_root)
    if source.is_dir():
        _refuse_inside(source, root)
    fingerprint = _fingerprint(source)
    dest = _allocate(root, _stem(given), now or _now())
    router: Router | None = None
    try:
        router = Router(live=bool(live) and not dry_run_forced())
        prepared = _prepare(
            given,
            source,
            dest,
            transcript=transcript,
            durations=durations,
            force_media=force_media,
        )
        if prepared.fcpxml is not None:
            _inspect_timeline(prepared)
        _route_music(
            prepared, dest, brief=brief, style=style, live=router.live, taste=taste, router=router
        )
        result = _run_iterate(
            prepared,
            dest,
            brief=brief,
            live=router.live,
            taste=taste,
            max_rounds=max_rounds,
            router=router,
            style=style,
            graphics=graphics,
            beats=beats,
        )
        if _fingerprint(source) != fingerprint:
            raise ConductorError(
                "The drop changed while byjwu was reading it. "
                "The original was not written by byjwu. Drop it again once the copy has finished."
            )
        summary = _summarize(prepared, result, dest, router)
        _write_success(dest, summary)
        return summary
    except ConductorError as exc:
        friendly = _chat(exc)
        _write_failure(dest, given, friendly)
        if friendly is exc:
            raise
        raise friendly from exc
    except Exception as exc:  # noqa: BLE001 - the bot needs a sentence, not a traceback
        friendly = ConductorError(_unexpected(exc))
        _write_failure(dest, given, friendly)
        raise friendly from exc
    finally:
        if router is not None:
            router.close()


def watch(
    inbox: str | Path,
    *,
    out_root: str | Path | None = None,
    brief: str | None = None,
    live: bool = False,
    transcript: str | Path | None = None,
    taste: str | Path | None = None,
    durations: str | Path | None = None,
    max_rounds: int = 5,
    style: str | None = None,
    graphics: bool | None = None,
    beats: str | Path | None = None,
    stable_seconds: float = 2.0,
    poll_seconds: float = 1.0,
    max_scans: int | None = None,
    sleeper=time.sleep,
    clock_fn=time.monotonic,
    on_event=None,
) -> WatchState:
    """Poll ``inbox`` until interrupted. ``max_scans`` stops a test loop."""
    folder = Path(inbox).expanduser()
    if not folder.is_dir():
        raise ConductorError(
            f"The drop folder {folder} is not there. "
            "Create ~/Desktop/byjwu-in and drop a Final Cut XML or a folder of clips into it."
        )
    if stable_seconds < 0 or poll_seconds < 0:
        raise ConductorError("--stable-seconds and --poll-seconds must be >= 0")
    root = _out_root(out_root)
    state = WatchState.load(root)
    scans = 0
    while True:
        events = scan_once(
            folder,
            root,
            state,
            now=clock_fn(),
            stable_seconds=stable_seconds,
            brief=brief,
            live=live,
            transcript=transcript,
            taste=taste,
            durations=durations,
            max_rounds=max_rounds,
            style=style,
            graphics=graphics,
            beats=beats,
        )
        if on_event is not None:
            for event in events:
                on_event(event)
        scans += 1
        if max_scans is not None and scans >= max_scans:
            return state
        sleeper(poll_seconds)


def scan_once(
    inbox: str | Path,
    out_root: str | Path,
    state: WatchState,
    *,
    now: float | None = None,
    stable_seconds: float = 2.0,
    brief: str | None = None,
    live: bool = False,
    transcript: str | Path | None = None,
    taste: str | Path | None = None,
    durations: str | Path | None = None,
    max_rounds: int = 5,
    style: str | None = None,
    graphics: bool | None = None,
    beats: str | Path | None = None,
) -> list[WatchEvent]:
    """Process inbox items whose size has stayed the same for ``stable_seconds``.

    The first time an item is seen it is only recorded. A later scan processes
    it once the signature still matches and the wait has elapsed. Digests
    already in the ledger are skipped.
    """
    folder = Path(inbox).expanduser()
    if not folder.is_dir():
        raise ConductorError(
            f"The drop folder {folder} is not there. "
            "Create ~/Desktop/byjwu-in and drop a Final Cut XML or a folder of clips into it."
        )
    if stable_seconds < 0:
        raise ConductorError("--stable-seconds must be >= 0")
    moment = time.monotonic() if now is None else now
    root = Path(out_root).expanduser()
    events: list[WatchEvent] = []
    for drop in list_drops(folder):
        key = _drop_key(drop)
        signature = _signature(drop)
        previous = state.pending.get(key)
        if previous is None or previous[0] != signature:
            state.pending[key] = (signature, moment)
            continue
        if moment - previous[1] < stable_seconds:
            continue
        try:
            digest = _digest(drop)
        except (ConductorError, OSError) as exc:
            state.pending.pop(key, None)
            message = f"error {drop.path} {exc}"
            _log(root, message)
            events.append(WatchEvent(drop.path, "error", str(exc)))
            continue
        remembered = state.seen.get(digest)
        if remembered is not None:
            if digest not in state.announced:
                state.announced.add(digest)
                message = f"skip {drop.path} already processed"
                _log(root, message)
                events.append(WatchEvent(drop.path, "skipped", message, _as_path(remembered.get("out_dir"))))
            continue
        try:
            result = room_run(
                drop.path,
                out_root=root,
                brief=brief,
                live=live,
                transcript=transcript,
                taste=taste,
                durations=durations,
                max_rounds=max_rounds,
                force_media=drop.force_media,
                style=style,
                graphics=graphics,
                beats=beats,
            )
        except Exception as exc:  # noqa: BLE001 - one bad drop must not stop the watcher
            text = str(exc) if isinstance(exc, ConductorError) else _unexpected(exc)
            if _still(drop, digest):
                state.seen[digest] = _seen_row(drop.path, ok=False, error=text)
                state.save(root)
            message = f"error {drop.path} {text}"
            _log(root, message)
            events.append(WatchEvent(drop.path, "error", text))
            continue
        state.seen[digest] = _seen_row(
            drop.path,
            ok=True,
            out_dir=str(result.out_dir),
            open_path=str(result.open_in_final_cut) if result.open_in_final_cut else None,
        )
        state.save(root)
        message = (
            f"ok {result.payload['input']['kind']} {drop.path} → {result.out_dir} "
            f"stop={result.payload['stop_reason']} open={result.open_in_final_cut}"
        )
        _log(root, message)
        events.append(WatchEvent(drop.path, "ran", result.markdown, result.out_dir))
    return events


def list_drops(inbox: Path) -> list[Drop]:
    """Top-level drops. Loose video files in the inbox are one media drop.

    Loose transcripts, ``durations.json``, and music travel with loose clips.
    Without loose clips they are not a drop on their own.
    """
    children = [
        child
        for child in inbox.iterdir()
        if not child.name.startswith(".") and child.name != "__MACOSX"
    ]
    loose = [child for child in children if child.is_file() and child.suffix.lower() in VIDEO_EXTENSIONS]
    sidecars = [
        child
        for child in children
        if child.is_file()
        and (
            child.suffix.lower() in {".srt", ".vtt"}
            or child.suffix.lower() in MUSIC_EXTENSIONS
            or child.name.lower() == "durations.json"
        )
    ]
    drops: list[Drop] = []
    if loose:
        members = tuple(sorted([*loose, *sidecars], key=lambda item: item.name.lower()))
        drops.append(Drop(inbox, force_media=True, members=members))
    for child in sorted(children, key=lambda item: item.name.lower()):
        if child in loose or child in sidecars:
            continue
        if child.is_file() and child.suffix.lower() in {".txt", ".md"}:
            continue
        drops.append(Drop(child))
    return drops


def _prepare(
    given: Path,
    source: Path,
    dest: Path,
    *,
    transcript: str | Path | None,
    durations: str | Path | None,
    force_media: bool,
) -> Prepared:
    explicit_transcript = _existing_transcript(transcript) if transcript else None
    explicit_durations = _existing_file(durations, "durations") if durations else None
    if force_media:
        if not source.is_dir():
            raise ConductorError(
                f"{given} is not a folder of clips. Drop the folder, or drop a Final Cut XML export."
            )
        return _media_prepared(
            given,
            source,
            transcript=explicit_transcript,
            durations=explicit_durations,
        )
    if source.is_file():
        suffix = source.suffix.lower()
        if suffix == ".fcpxml":
            return _timeline_prepared(
                given,
                source,
                kind="fcpxml",
                contained=None,
                fcpxml=source,
                local_root=source.parent,
                transcript=explicit_transcript,
            )
        if suffix == ".zip":
            return _from_zip(given, source, dest, explicit_transcript)
        raise ConductorError(
            f"{source.name} is not a Final Cut timeline. "
            "Drop a .fcpxml export, a .fcpxmld bundle, a zip of either, or a folder of clips."
        )
    if source.is_dir():
        return _from_directory(
            given,
            source,
            dest,
            transcript=explicit_transcript,
            durations=explicit_durations,
        )
    raise ConductorError(
        f"{given} is not a Final Cut timeline or a folder of clips. "
        "Drop a .fcpxml export, a .fcpxmld bundle, a zip of either, or a folder of clips."
    )


def _from_directory(
    given: Path,
    source: Path,
    dest: Path,
    *,
    transcript: Path | None,
    durations: Path | None,
    depth: int = 0,
) -> Prepared:
    if depth > 3:
        raise ConductorError(
            "That folder is nested too deeply. Drop the XML file or the clips folder itself."
        )
    if _is_bundle(source):
        info = _info_fcpxml(source)
        if info is None:
            raise ConductorError(
                "That .fcpxmld bundle has no Info.fcpxml, so there is no timeline to read. "
                "Export the project again with File → Export XML… and drop the new bundle or the .fcpxml file."
            )
        return _timeline_prepared(
            given,
            source,
            kind="fcpxmld",
            contained=None,
            fcpxml=info,
            local_root=source,
            transcript=transcript,
        )
    top = _visible(source)
    videos = [item for item in top if item.is_file() and item.suffix.lower() in VIDEO_EXTENSIONS]
    if videos:
        return _media_prepared(given, source, transcript=transcript, durations=durations)
    timelines = [item for item in top if _is_timeline_item(item)]
    if len(timelines) == 1:
        chosen = timelines[0]
        if chosen.is_file() and chosen.suffix.lower() == ".zip":
            return _from_zip(given, chosen, dest, transcript, origin=source)
        if chosen.is_dir():
            return _from_directory(
                given,
                chosen,
                dest,
                transcript=transcript,
                durations=durations,
                depth=depth + 1,
            )
        return _timeline_prepared(
            given,
            source,
            kind="fcpxml",
            contained=None,
            fcpxml=chosen,
            local_root=chosen.parent,
            transcript=transcript,
        )
    if len(timelines) > 1:
        names = ", ".join(item.name for item in timelines)
        raise ConductorError(
            f"That folder has more than one Final Cut drop ({names}). "
            "Pass one file, or run room-run --watch on the folder so each drop is handled on its own."
        )
    subdirs = [item for item in top if item.is_dir()]
    if len(subdirs) == 1 and not any(item.is_file() for item in top):
        return _from_directory(
            given,
            subdirs[0],
            dest,
            transcript=transcript,
            durations=durations,
            depth=depth + 1,
        )
    raise ConductorError(
        "No video clips in that folder, so the media is missing. "
        "Drop .mov, .mp4, .m4v, .mxf, .avi, .mkv, .mts, or .m2ts files into the folder itself, "
        "not a subfolder. Or drop a Final Cut XML export instead."
    )


def _from_zip(
    given: Path,
    zip_path: Path,
    dest: Path,
    transcript: Path | None,
    *,
    origin: Path | None = None,
) -> Prepared:
    unpacked = dest / "unpacked"
    unpacked.mkdir(parents=True, exist_ok=True)
    _safe_extract(zip_path, unpacked)
    found = _find_timelines(unpacked)
    if not found:
        raise ConductorError(
            "That zip has no Final Cut XML in it. "
            "Zip a .fcpxml file or a .fcpxmld bundle (the one that contains Info.fcpxml) and drop it again. "
            "A folder of clips does not need to be zipped — drop the folder itself."
        )
    if len(found) > 1:
        names = ", ".join(path.name for _, path in found)
        raise ConductorError(
            f"That zip contains more than one Final Cut timeline ({names}). "
            "Zip a single .fcpxml or .fcpxmld and drop it again."
        )
    contained, chosen = found[0]
    if contained == "fcpxmld":
        info = _info_fcpxml(chosen)
        if info is None:
            raise ConductorError(
                "That zip's .fcpxmld bundle has no Info.fcpxml. "
                "Export again with File → Export XML… and drop the new bundle."
            )
        fcpxml = info
        local_root = chosen
    else:
        fcpxml = chosen
        local_root = chosen.parent
    return _timeline_prepared(
        given,
        origin or zip_path,
        kind="zip",
        contained=contained,
        fcpxml=fcpxml,
        local_root=local_root,
        transcript=transcript,
    )


def _timeline_prepared(
    given: Path,
    origin: Path,
    *,
    kind: str,
    contained: str | None,
    fcpxml: Path,
    local_root: Path,
    transcript: Path | None,
) -> Prepared:
    found, warnings = _discover_transcript(fcpxml.parent, fcpxml.stem, reserved=set())
    return Prepared(
        kind=kind,
        contained=contained,
        given=given,
        origin=origin,
        fcpxml=fcpxml,
        transcript=transcript or found,
        local_root=local_root,
        warnings=warnings,
    )


def _media_prepared(
    given: Path,
    source: Path,
    *,
    transcript: Path | None,
    durations: Path | None,
) -> Prepared:
    reserved = {
        item.stem
        for item in _visible(source)
        if item.is_file() and item.suffix.lower() == ".fcpxml"
    }
    found, warnings = _discover_transcript(source, None, reserved=reserved)
    durations_path = durations
    if durations_path is None:
        candidate = source / "durations.json"
        if candidate.is_file():
            durations_path = candidate
    music = sorted(
        (item for item in _visible(source) if item.is_file() and item.suffix.lower() in MUSIC_EXTENSIONS),
        key=lambda item: item.name.lower(),
    )
    timelines = [item for item in _visible(source) if item.is_file() and item.suffix.lower() == ".fcpxml"]
    return Prepared(
        kind="media",
        contained=None,
        given=given,
        origin=source,
        media=source,
        transcript=transcript or found,
        durations=durations_path,
        local_root=source,
        warnings=warnings,
        media_present=True,
        music=music,
        flow="ingest+iterate",
        timeline_hint=timelines[0] if len(timelines) == 1 else None,
    )


def _inspect_timeline(prepared: Prepared) -> None:
    assert prepared.fcpxml is not None
    try:
        document = parse_fcpxml(prepared.fcpxml)
    except ConductorError as exc:
        raise ConductorError(
            "That file is not a Final Cut XML export I can read. "
            f"{exc} In Final Cut, choose File → Export XML… and drop the new file."
        ) from exc
    version = (document.version or "").strip()
    if version not in SUPPORTED_FCPXML:
        supported = ", ".join(SUPPORTED_FCPXML)
        shown = version or "missing"
        raise ConductorError(
            f"This Final Cut XML is version {shown}, which byjwu cannot read. "
            f"Supported versions are {supported}. "
            "In Final Cut, choose File → Export XML… and export a current XML, then drop that file again."
        )
    root = prepared.local_root or prepared.fcpxml.parent
    missing = _missing_local_media(document, root)
    if missing:
        names = ", ".join(missing)
        raise ConductorError(
            f"The media for this drop is missing ({names}). "
            "The XML points at those files inside the drop, and they are not there. "
            "Add the clips and drop it again."
        )
    present, note = _external_media(document, root)
    prepared.media_present = present
    prepared.media_note = note
    prepared.music = _timeline_music(document, root)


def _run_iterate(
    prepared: Prepared,
    dest: Path,
    *,
    brief: str | None,
    live: bool,
    taste: str | Path | None,
    max_rounds: int,
    router: Router | None = None,
    style: str | None = None,
    graphics: bool | None = None,
    beats: str | Path | None = None,
) -> IterateResult:
    text = (brief if brief is not None else DEFAULT_BRIEF).strip() or DEFAULT_BRIEF
    shared = dict(
        brief=text,
        out_dir=dest,
        transcript_path=prepared.transcript,
        taste_path=taste,
        live=live,
        max_rounds=max_rounds,
        router=router,
        style=style,
        graphics=graphics,
        beats=beats,
    )
    try:
        if prepared.assembled is not None:
            return iterate(fcpxml=prepared.assembled, **shared)
        if prepared.media is not None:
            return iterate(media=prepared.media, durations_path=prepared.durations, **shared)
        return iterate(fcpxml=prepared.fcpxml, **shared)
    except ConductorError as exc:
        raise _chat(exc) from exc


def register_assembler(fn: Callable[..., object] | None) -> None:
    """Route music drops to ``fn``. ``None`` goes back to discovery.

    Without a registration, ``conductor.assemble.assemble`` is used when that
    module exists. The callable receives the keywords from ``_assemble_kwargs``
    that its signature accepts, and returns the FCPXML it wrote: a path, a
    mapping, or an object with ``fcpxml`` / ``out_fcpxml`` / ``timeline`` /
    ``path``. room-run then iterates that file like any other timeline.
    """
    global _ASSEMBLER
    _ASSEMBLER = fn


def find_assembler() -> Callable[..., object] | None:
    if _ASSEMBLER is not None:
        return _ASSEMBLER
    try:
        module = importlib.import_module("conductor.assemble")
    except ModuleNotFoundError as exc:
        if exc.name == "conductor.assemble":
            return None
        raise
    fn = getattr(module, "assemble", None)
    return fn if callable(fn) else None


def _route_music(
    prepared: Prepared,
    dest: Path,
    *,
    brief: str | None,
    style: str | None,
    live: bool,
    taste: str | Path | None,
    router: Router | None = None,
) -> None:
    if not prepared.music:
        return
    names = ", ".join(path.name for path in prepared.music)
    assembler = find_assembler()
    if assembler is None:
        what = (
            "the clips were sequenced in filename order and the music was not placed"
            if prepared.media is not None
            else "the timeline was iterated as it stands"
        )
        prepared.warnings.append(
            f"Music found ({names}), but no assemble step is installed here, so {what}."
        )
        return
    chosen = (style or DEFAULT_STYLE).strip() or DEFAULT_STYLE
    out_dir = dest / "assemble"
    out_dir.mkdir(parents=True, exist_ok=True)
    kwargs = _assemble_kwargs(
        prepared,
        out_dir=out_dir,
        brief=brief,
        style=chosen,
        live=live,
        taste=taste,
        router=router,
    )
    try:
        returned = assembler(**_accepted(assembler, kwargs))
    except ConductorError as exc:
        raise ConductorError(f"The {chosen} assembly did not finish. {exc}") from exc
    except TypeError as exc:
        raise ConductorError(
            f"The assemble step does not take the arguments room-run passes ({exc}). "
            "Update the assembler or room-run so they agree."
        ) from exc
    timeline = _assembled_path(returned)
    if timeline is None:
        raise ConductorError(
            f"The {chosen} assembly finished without an FCPXML, so there is nothing to open in Final Cut."
        )
    prepared.assembled = timeline
    prepared.flow = "assemble+iterate"
    prepared.style = chosen


def _assemble_kwargs(
    prepared: Prepared,
    *,
    out_dir: Path,
    brief: str | None,
    style: str,
    live: bool,
    taste: str | Path | None,
    router: Router | None = None,
) -> dict:
    return {
        "media": prepared.media,
        "fcpxml": prepared.fcpxml or prepared.timeline_hint,
        "music": list(prepared.music),
        "style": style,
        "brief": brief,
        "out_dir": out_dir,
        "live": live,
        "transcript_path": prepared.transcript,
        "taste_path": taste,
        "durations_path": prepared.durations,
        "router": router,
    }


def _accepted(fn: Callable[..., object], kwargs: dict) -> dict:
    try:
        params = inspect.signature(fn).parameters
    except (TypeError, ValueError):
        return kwargs
    if any(param.kind is inspect.Parameter.VAR_KEYWORD for param in params.values()):
        return kwargs
    return {key: value for key, value in kwargs.items() if key in params}


def _assembled_path(returned: object) -> Path | None:
    value: object = returned
    if isinstance(returned, dict):
        value = next((returned[key] for key in _RESULT_KEYS if returned.get(key)), None)
    elif not isinstance(returned, (str, os.PathLike)):
        value = next(
            (getattr(returned, key) for key in _RESULT_KEYS if getattr(returned, key, None)),
            None,
        )
    if not isinstance(value, (str, os.PathLike)):
        return None
    path = Path(value)
    if path.is_dir():
        path = _info_fcpxml(path) or path
    if not path.is_file() or path.suffix.lower() != ".fcpxml":
        return None
    return path.resolve()


def _timeline_music(document: Document, root: Path) -> list[Path]:
    found: list[Path] = []
    for asset in sorted(document.assets.values(), key=lambda item: item.id):
        if not asset.src:
            continue
        path = _src_path(asset.src, root)
        if path.suffix.lower() in MUSIC_EXTENSIONS and path not in found:
            found.append(path)
    return found


def _summarize(
    prepared: Prepared, result: IterateResult, dest: Path, router: Router | None = None
) -> RoomRun:
    if not result.rounds:
        raise ConductorError("The run finished without a round, so there is no file to open in Final Cut.")
    last = result.rounds[-1]
    shadow = Path(last["shadow"]).resolve() if last.get("shadow") else None
    if shadow is None or not shadow.is_file():
        raise ConductorError(
            "The run finished without a shadow FCPXML, so there is no file to open in Final Cut."
        )
    open_path = Path(last["next"]).resolve()
    if not open_path.is_file():
        raise ConductorError("The run finished without a timeline to open in Final Cut.")
    before = measure(parse_fcpxml(result.rounds[0]["source"]).sequences)["duration_seconds"]
    after = measure(parse_fcpxml(open_path).sequences)["duration_seconds"]
    rules_fired, cuts_applied = _across_rounds(result.rounds)
    cuts = [_cut_row(cut) for row in result.rounds for cut in (row.get("cuts") or [])]
    flagged = _flagged(last.get("json"))
    media_signals = last.get("signals") or {}
    signals = _signals(prepared, media_signals)
    payload = {
        "protocol": PROTOCOL,
        "protocol_version": PROTOCOL_VERSION,
        "conductor_version": __version__,
        "ok": True,
        "mode": result.rounds[-1].get("mode"),
        "input": {
            "path": str(prepared.given),
            "resolved": str(prepared.origin),
            "kind": prepared.kind,
            "contained": prepared.contained,
            "name": prepared.given.name,
        },
        "signals": signals,
        "signals_label": ", ".join(signals) if signals else "none",
        "media_signals": media_signals,
        "words": last.get("words"),
        "duration": {
            "before_seconds": before,
            "after_seconds": after,
            "before": _clock_seconds(before),
            "after": _clock_seconds(after),
        },
        "cuts": cuts,
        "cuts_applied": cuts_applied,
        "rules_fired": rules_fired,
        "flagged": flagged,
        "stop_reason": result.stop_reason,
        "needs_human": result.needs_human,
        "human_reasons": list(result.human_reasons),
        "rounds": len(result.rounds),
        "open_in_final_cut": str(open_path),
        "shadow": str(shadow),
        "out_dir": str(dest.resolve()),
        "iterate_json": str(result.out_json) if result.out_json else None,
        "starter": str(result.starter) if result.starter else None,
        "brief": _iterate_brief(result),
        "flow": prepared.flow,
        "style": prepared.style,
        "assembled_fcpxml": str(prepared.assembled) if prepared.assembled else None,
        "music": [str(path) for path in prepared.music],
        "media_note": prepared.media_note,
        "decision_usage": _usage(result, router),
        "rules": _rules_from_round(last),
        "lint": last.get("lint"),
        "warnings": list(
            dict.fromkeys(
                [*prepared.warnings, *result.warnings, *(router.ledger.warnings if router else [])]
            )
        ),
    }
    markdown = _markdown(payload)
    return RoomRun(True, dest.resolve(), markdown, payload, open_path)


def _rules_from_round(last: dict) -> dict:
    path = last.get("json")
    if not path:
        return {}
    file = Path(path)
    if not file.is_file():
        return {}
    try:
        payload = json.loads(file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    rules = payload.get("rules") or {}
    return rules if isinstance(rules, dict) else {}


def _markdown(payload: dict) -> str:
    duration = payload["duration"]
    kind = payload["input"]["kind"]
    if payload["input"].get("contained"):
        kind = f"{kind}, {payload['input']['contained']} inside"
    lines = [
        "# byjwu",
        "",
        f"Input: {payload['input']['name']} ({kind})",
        f"Flow: {payload['flow']}" + (f" (style {payload['style']})" if payload.get("style") else ""),
        f"Duration: {duration['before']} → {duration['after']}",
        f"Stop: {payload['stop_reason']}",
        f"Cuts applied: {payload['cuts_applied']}",
        f"Rules fired: {payload['rules_fired']}",
        f"Signals: {payload['signals_label']}",
        f"Decisions: {format_usage(payload['decision_usage']) or 'none'}",
        *format_rules(payload.get("rules") or {}),
    ]
    if (payload.get("media_signals") or {}).get("summary"):
        lines.append(f"Audio and words: {payload['media_signals']['summary']}")
    lines += [
        "",
        "## Cuts applied",
        "",
    ]
    if not payload["cuts"]:
        lines.append("None.")
    else:
        for cut in payload["cuts"]:
            clip = f" — {cut['clip_name']}" if cut.get("clip_name") else ""
            lines.append(f"- {cut['timecode']} {cut['action']} ({cut['pass']}){clip}")
    lines.extend(_room_lint(payload))
    lines.extend(["", "## Flagged for the editor", ""])
    if not payload["flagged"]:
        lines.append("None.")
    else:
        for item in payload["flagged"]:
            lines.append(_flag_line(item))
    opened = _display_path(payload["open_in_final_cut"], payload["out_dir"])
    lines.extend(["", f"Open in Final Cut: {opened}"])
    if payload["shadow"] != payload["open_in_final_cut"]:
        lines.append(f"Shadow (markers): {_display_path(payload['shadow'], payload['out_dir'])}")
    if payload.get("media_note"):
        lines.extend(["", payload["media_note"]])
    if payload["needs_human"]:
        why = ", ".join(payload["human_reasons"]) or "a person is needed"
        lines.extend(["", f"Ask a person to open that file ({why})."])
    if payload["warnings"]:
        lines.extend(["", "## Warnings", ""])
        for warning in payload["warnings"]:
            lines.append(f"- {warning}")
    lines.append("")
    return "\n".join(lines)


def _usage(result: IterateResult, router: Router | None) -> dict:
    """Iterate's rounds plus anything the assembler asked the router directly."""
    total = Ledger()
    if router is not None:
        total.merge(router.ledger)
    if result.ledger is not None:
        total.merge(result.ledger)
    return total.to_dict()


def _room_lint(payload: dict) -> list[str]:
    lint = payload.get("lint")
    if not lint:
        return []
    lines = ["", "## Lint", ""]
    if lint.get("blocked"):
        lines.append(f"Export blocked. {lint.get('blocked_reason') or ''}".rstrip())
    else:
        lines.append("No hard lint blocked the export.")
    body = lint.get("lint") or {}
    hard = body.get("hard") or []
    soft = body.get("soft") or []
    if hard:
        lines.append("Hard:")
        for finding in hard:
            lines.append(f"- {finding['code']}: {finding['message']}")
    if soft:
        lines.append("Warnings:")
        for finding in soft:
            lines.append(f"- {finding['code']}: {finding['message']}")
    critic = lint.get("critic") or []
    if not critic or critic[0].get("skipped"):
        lines.append("Critic skipped (no taste model available).")
    else:
        lines.append(f"Critic: {critic[-1].get('action')}.")
    return lines


def _flag_line(item: dict) -> str:
    when = item.get("timecode") or ""
    if item.get("length"):
        when = f"{when} ({item['length']})".strip()
    elif isinstance(item.get("range"), list) and len(item["range"]) == 2:
        when = f"{item['range'][0]}–{item['range'][1]}"
    ident = item.get("candidate_id") or ""
    clip = f" — {item['clip_name']}" if item.get("clip_name") else ""
    return f"- {item['section']} {ident} {when} {item['action']} ({item['pass']}){clip}"


def _write_success(dest: Path, summary: RoomRun) -> None:
    (dest / "room.json").write_text(dumps(summary.payload), encoding="utf-8")
    (dest / "room.md").write_text(summary.markdown, encoding="utf-8")


def _write_failure(dest: Path, given: Path, exc: ConductorError) -> None:
    payload = {
        "protocol": PROTOCOL,
        "protocol_version": PROTOCOL_VERSION,
        "conductor_version": __version__,
        "ok": False,
        "error": str(exc),
        "input": {"path": str(given), "name": given.name},
        "out_dir": str(dest),
    }
    try:
        dest.mkdir(parents=True, exist_ok=True)
        (dest / "room.json").write_text(dumps(payload), encoding="utf-8")
        (dest / "room.md").write_text(str(exc) + "\n", encoding="utf-8")
    except OSError:
        return


def _chat(exc: ConductorError) -> ConductorError:
    text = str(exc)
    if text.startswith("no video files") or text.startswith("no video clips"):
        return ConductorError(
            "No video clips in that folder, so the media is missing. "
            "Drop .mov, .mp4, .m4v, .mxf, .avi, .mkv, .mts, or .m2ts files into the folder itself, "
            "not a subfolder. Or drop a Final Cut XML export instead."
        )
    if text.startswith("no such media folder") or text.startswith("no such FCPXML"):
        return ConductorError(
            f"That path is missing. {text} Drop the file again, or check the path the bot was given."
        )
    return exc


def _iterate_brief(result: IterateResult) -> str | None:
    if result.out_json and result.out_json.is_file():
        payload = json.loads(result.out_json.read_text(encoding="utf-8"))
        brief = payload.get("brief")
        if isinstance(brief, str):
            return brief
    return None


def _signals(prepared: Prepared, media_signals: dict) -> list[str]:
    found: list[str] = []
    if prepared.transcript is not None:
        found.append("transcript")
    if prepared.kind == "media" or prepared.media_present:
        found.append("media")
    if prepared.music:
        found.append("music")
    if media_signals.get("audio") == "used":
        found.append("audio")
    if media_signals.get("transcript") == "whisper":
        found.append("words")
    return found


def _cut_row(cut: dict) -> dict:
    return {
        "round": cut["round"],
        "candidate_id": cut["candidate_id"],
        "action": cut["action"],
        "pass": cut["pass"],
        "clip_name": cut.get("clip_name"),
        "start_seconds": cut["start_seconds"],
        "end_seconds": cut["end_seconds"],
        "timecode": _span(cut["start_seconds"], cut["end_seconds"]),
    }


def _across_rounds(rounds: list[dict]) -> tuple[int, int]:
    """Rules fired and cuts applied, summed over every round."""
    rules = 0
    cuts = 0
    for row in rounds:
        fired, applied = _rule_counts(row)
        rules += fired
        cuts += applied
    return rules, cuts


def _rule_counts(row: dict) -> tuple[int, int]:
    """``(rules fired, cuts applied)`` for one round.

    Rules are changes the deterministic fallback answered (``engine_source``
    ``rules``). Cuts are the ones that round wrote. The last round alone is
    not the run: an earlier round can cut and then stop.
    """
    cuts = len(row.get("cuts") or [])
    path = row.get("json")
    if not path:
        return 0, cuts
    file = Path(path)
    if not file.is_file():
        return 0, cuts
    payload = json.loads(file.read_text(encoding="utf-8"))
    rules = sum(
        1 for change in payload.get("changes") or [] if change.get("engine_source") == "rules"
    )
    return rules, cuts


def _display_path(path: str, out_dir: str) -> str:
    """Path the editor opens, when ``BYJWU_OUT_DISPLAY_ROOT`` is set.

    The root is the folder that corresponds to this run's output parent, for
    example ``~/Desktop/byjwu-out``. Unset, the path is the one on the machine
    that ran conductor. ``~`` is kept as written. No username is added.
    """
    root = os.environ.get("BYJWU_OUT_DISPLAY_ROOT", "").strip()
    if not root or not path:
        return path
    resolved = Path(path).resolve()
    parent = Path(out_dir).resolve().parent
    try:
        relative = resolved.relative_to(parent)
    except ValueError:
        return path
    return f"{root.rstrip('/')}/{relative.as_posix()}"


def _flagged(json_path: str | None) -> list[dict]:
    if not json_path:
        return []
    file = Path(json_path)
    if not file.is_file():
        return []
    payload = json.loads(file.read_text(encoding="utf-8"))
    durations = {
        item.get("id"): item.get("duration_seconds") for item in payload.get("candidates") or []
    }
    rows = []
    for row in payload.get("changes") or []:
        if row.get("section") not in {"review", "escalate"} and row.get("kind") != "long_static":
            continue
        rows.append(
            {
                "candidate_id": row.get("candidate_id"),
                "section": row.get("section"),
                "action": row.get("action"),
                "pass": row.get("pass"),
                "kind": row.get("kind"),
                "clip_name": row.get("clip_name"),
                "timecode": row.get("timecode"),
                "length": _length_label(durations.get(row.get("candidate_id"))),
                "range": row.get("range"),
                "reason": row.get("reason"),
                "confidence": row.get("confidence"),
            }
        )
    return rows


def _length_label(duration: float | None) -> str | None:
    if duration is None:
        return None
    if abs(duration - round(duration)) < 1e-3:
        return f"{int(round(duration))}s"
    text = f"{duration:.3f}".rstrip("0").rstrip(".")
    return f"{text}s"


def _missing_local_media(document: Document, root: Path) -> list[str]:
    missing: list[str] = []
    for asset in sorted(document.assets.values(), key=lambda item: item.id):
        if not asset.src:
            continue
        path = _src_path(asset.src, root)
        if _is_local_ref(asset.src, path, root) and not path.is_file():
            missing.append(asset.name)
    return missing


def _external_media(document: Document, root: Path) -> tuple[bool, str | None]:
    referenced: list[tuple[str, Path, bool]] = []
    for asset in sorted(document.assets.values(), key=lambda item: item.id):
        if not asset.src:
            continue
        path = _src_path(asset.src, root)
        referenced.append((asset.name, path, path.is_file()))
    if not referenced:
        return False, None
    if any(exists for _, _, exists in referenced):
        return True, None
    names = ", ".join(name for name, _, _ in referenced)
    return False, (
        f"Media files named in the XML are not on this machine ({names}). "
        "The run used the timeline XML only. Open the FCPXML in Final Cut and use "
        "Relink Files if the pictures are offline."
    )


def _src_path(src: str, base: Path) -> Path:
    if src.lower().startswith("file:"):
        parsed = urlparse(src)
        raw = unquote(parsed.path)
        if parsed.netloc not in ("", "localhost"):
            return Path(f"//{parsed.netloc}{raw}")
        return Path(raw)
    return (base / src).resolve()


def _is_local_ref(src: str, path: Path, root: Path) -> bool:
    if not src.lower().startswith("file:"):
        return True
    return _is_under(path, root)


def _is_under(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
    except (OSError, ValueError):
        return False
    return True


def _discover_transcript(
    directory: Path,
    stem: str | None,
    *,
    reserved: set[str],
) -> tuple[Path | None, list[str]]:
    if not directory.is_dir():
        return None, []
    files = [
        child
        for child in directory.iterdir()
        if child.is_file() and child.suffix.lower() in {".srt", ".vtt"}
    ]
    if stem:
        for child in files:
            if child.stem == stem:
                return child, []
    available = [child for child in files if child.stem not in reserved]
    if len(available) == 1:
        return available[0], []
    if len(available) > 1:
        names = ", ".join(child.name for child in sorted(available, key=lambda item: item.name.lower()))
        return None, [
            f"More than one transcript sits next to this timeline ({names}). "
            "None was used. Pass --transcript with the one that matches the sequence clock."
        ]
    return None, []


def _find_timelines(root: Path) -> list[tuple[str, Path]]:
    bundles: list[Path] = []
    xmls: list[Path] = []
    if not root.exists():
        return []
    for path in root.rglob("*"):
        if _skipped(path):
            continue
        if path.is_dir() and path.suffix.lower() == ".fcpxmld":
            bundles.append(path)
        elif path.is_file() and path.suffix.lower() == ".fcpxml":
            xmls.append(path)
    bundle_dirs = {bundle.resolve() for bundle in bundles}
    plain: list[Path] = []
    extra: list[Path] = []
    for xml in xmls:
        parents = {parent.resolve() for parent in xml.parents}
        if parents & bundle_dirs:
            continue
        if xml.name.lower() == "info.fcpxml":
            extra.append(xml.parent)
            continue
        plain.append(xml)
    found = [("fcpxmld", bundle) for bundle in [*bundles, *extra]]
    found.extend(("fcpxml", xml) for xml in plain)
    return found


def _safe_extract(zip_path: Path, dest: Path) -> None:
    dest = dest.resolve()
    try:
        archive = zipfile.ZipFile(zip_path)
    except zipfile.BadZipFile as exc:
        raise ConductorError(
            "That file is not a zip archive I can open. "
            "Drop a .zip of the Final Cut XML, or drop the .fcpxml itself."
        ) from exc
    with archive:
        for info in archive.infolist():
            name = info.filename
            parts = Path(name).parts
            if name.startswith(("/", "\\")) or ".." in parts:
                raise ConductorError(
                    "That zip contains a path that escapes the archive, so it was not opened. "
                    "Re-zip the Final Cut XML and drop it again."
                )
            mode = info.external_attr >> 16
            if stat.S_ISLNK(mode):
                raise ConductorError(
                    "That zip contains a link, so it was not opened. "
                    "Re-zip the Final Cut XML as real files and drop it again."
                )
            target = (dest / name).resolve()
            if target != dest and dest not in target.parents:
                raise ConductorError(
                    "That zip contains a path that escapes the archive, so it was not opened. "
                    "Re-zip the Final Cut XML and drop it again."
                )
        archive.extractall(dest)


def _is_bundle(path: Path) -> bool:
    return path.suffix.lower() == ".fcpxmld" or _info_fcpxml(path) is not None


def _is_timeline_item(path: Path) -> bool:
    if path.is_file() and path.suffix.lower() in {".fcpxml", ".zip"}:
        return True
    return path.is_dir() and _is_bundle(path)


def _info_fcpxml(bundle: Path) -> Path | None:
    if not bundle.is_dir():
        return None
    for name in ("Info.fcpxml", "info.fcpxml"):
        candidate = bundle / name
        if candidate.is_file():
            return candidate
    for child in bundle.iterdir():
        if child.is_file() and child.name.lower() == "info.fcpxml":
            return child
    return None


def _visible(directory: Path) -> list[Path]:
    return [
        child
        for child in directory.iterdir()
        if not child.name.startswith(".") and child.name != "__MACOSX"
    ]


def _skipped(path: Path) -> bool:
    return any(part == "__MACOSX" or part.startswith(".") for part in path.parts)


def _existing_transcript(path: str | Path) -> Path:
    file = Path(path).expanduser()
    if not file.is_file():
        raise ConductorError(
            f"No transcript at {file}. Pass the SRT or WebVTT, or leave --transcript off "
            "to use one sitting next to the timeline."
        )
    if file.suffix.lower() not in {".srt", ".vtt"}:
        raise ConductorError(
            f"{file.name} is not an SRT or WebVTT transcript. Pass a .srt or .vtt, or leave --transcript off."
        )
    return file


def _existing_file(path: str | Path, label: str) -> Path:
    file = Path(path).expanduser()
    if not file.is_file():
        raise ConductorError(f"No {label} file at {file}. Check the path the bot was given.")
    return file


def _out_root(path: str | Path | None) -> Path:
    if path is None:
        return drop_folder(DROP_OUT)[0].resolve()
    return Path(path).expanduser().resolve()


def _refuse_inside(source: Path, out_root: Path) -> None:
    src = source.resolve()
    root = out_root.resolve()
    if root == src or _is_under(root, src):
        raise ConductorError(
            "Refusing to write results inside the drop. "
            "Pass --out-root pointing at a different folder, usually ~/Desktop/byjwu-out."
        )


def _allocate(out_root: Path, stem: str, now: datetime) -> Path:
    out_root.mkdir(parents=True, exist_ok=True)
    stamp = now.strftime("%Y%m%d-%H%M%S")
    base = f"{stem}-{stamp}"
    number = 2
    candidate = out_root / base
    while True:
        try:
            candidate.mkdir()
            return candidate
        except FileExistsError:
            candidate = out_root / f"{base}-{number}"
            number += 1


def _stem(path: Path) -> str:
    name = path.name
    if name.lower().endswith(".fcpxmld"):
        name = name[: -len(".fcpxmld")]
    elif path.suffix:
        name = path.stem
    cleaned = _STEM_RE.sub("-", name).strip("-.")
    return cleaned or "drop"


def _now() -> datetime:
    return datetime.now().astimezone()


def _fingerprint(path: Path) -> tuple:
    """Size and mtime of every file. Ingest byte-hashes the clips it reads."""
    files = [path] if path.is_file() else _files_under(path)
    rows = []
    for file in files:
        info = file.stat()
        rows.append((file.as_posix(), info.st_size, info.st_mtime_ns))
    return tuple(rows)


def _files_under(path: Path) -> list[Path]:
    files = [
        file
        for file in path.rglob("*")
        if file.is_file() and not _skipped(file.relative_to(path))
    ]
    return sorted(files, key=lambda item: item.as_posix())


def _drop_key(drop: Drop) -> str:
    suffix = "|media" if drop.force_media else ""
    return str(drop.path.resolve()) + suffix


def _signature(drop: Drop) -> tuple:
    files = _drop_files(drop)
    rows = []
    for file in files:
        try:
            stat_result = file.stat()
        except OSError:
            rows.append((str(file), None, None))
            continue
        rows.append((str(file.resolve()), stat_result.st_size, stat_result.st_mtime_ns))
    return tuple(rows)


def _digest(drop: Drop) -> str:
    digest = hashlib.blake2b(digest_size=16)
    root = drop.path if drop.path.is_dir() else drop.path.parent
    for file in _drop_files(drop):
        try:
            rel = os.path.relpath(file.resolve(), root.resolve())
        except OSError:
            rel = file.name
        digest.update(rel.encode())
        digest.update(b"\0")
        try:
            with file.open("rb") as handle:
                for chunk in iter(lambda: handle.read(1 << 20), b""):
                    digest.update(chunk)
        except OSError as exc:
            raise ConductorError(
                f"Could not read {file}: {exc}. "
                "Check that the bot's user can read the drop and write the output folder."
            ) from exc
        digest.update(b"\0")
    return digest.hexdigest()


def _drop_files(drop: Drop) -> list[Path]:
    if drop.members:
        return list(drop.members)
    if drop.path.is_file():
        return [drop.path]
    return _files_under(drop.path)


def _seen_row(
    path: Path,
    *,
    ok: bool,
    out_dir: str | None = None,
    open_path: str | None = None,
    error: str | None = None,
) -> dict:
    return {
        "path": str(path),
        "ok": ok,
        "out_dir": out_dir,
        "open_in_final_cut": open_path,
        "error": error,
        "at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


def _as_path(value: str | None) -> Path | None:
    if not value:
        return None
    return Path(value)


def _log(out_root: Path, message: str) -> None:
    root = Path(out_root)
    root.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    with (root / LOG_NAME).open("a", encoding="utf-8") as handle:
        handle.write(f"{stamp} {message}\n")


def _span(start: float, end: float) -> str:
    return f"{clock(_fraction(start))}–{clock(_fraction(end))}"


def _clock_seconds(value: float) -> str:
    return clock(_fraction(value))


def _fraction(value: float) -> Fraction:
    return Fraction(str(value))


def _unexpected(exc: Exception) -> str:
    return (
        f"byjwu stopped on this drop ({type(exc).__name__}: {exc}). "
        "The original was not written by byjwu. Check the drop can be read and the output folder written, "
        "then drop it again."
    )


def _still(drop: Drop, digest: str) -> bool:
    try:
        return _digest(drop) == digest
    except (ConductorError, OSError):
        return False


def _missing_path(path: Path) -> str:
    return (
        f"Nothing at {path}. Drop a .fcpxml, a .fcpxmld bundle, a zip of either, "
        "or a folder of clips, and pass that path."
    )
