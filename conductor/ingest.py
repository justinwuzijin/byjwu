"""Build a starter FCPXML from a folder of clips, then run the shadow pass.

The folder is the input. The outputs are XML and a report. Source media is
read, hashed, and never written. Final Cut is not launched.

Duration for each clip comes from ``--durations`` when that file names it,
otherwise from ffprobe, otherwise from a 10-second placeholder. A placeholder
is not the picture's length. Final Cut uses the duration written into the
XML. ``media-rep`` paths are absolute ``file://`` URLs; if they do not
resolve on the machine that opens the XML, Final Cut needs Relink Files.

Order is the file name, case-insensitive. Subfolders and dotfiles are
ignored. The brief does not reorder clips.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import xml.etree.ElementTree as ET
from collections.abc import Callable
from dataclasses import dataclass, field
from fractions import Fraction
from pathlib import Path
from urllib.parse import quote

from .errors import ConductorError
from .fcpxml import write_document
from .report import dumps
from .run import Report, analyze
from .timeutil import format_time, parse_time, seconds

VIDEO_EXTENSIONS = frozenset(
    {".mov", ".mp4", ".m4v", ".mxf", ".avi", ".mkv", ".mts", ".m2ts"}
)
FALLBACK_DURATION = Fraction(10)
DEFAULT_FRAME = Fraction(1, 24)
DEFAULT_WIDTH = 1920
DEFAULT_HEIGHT = 1080
DEFAULT_BRIEF = (
    "Assemble these clips in filename order. Keep what serves the story, lose dead air."
)
RELINK = (
    "media-rep src values are absolute file:// URLs from this machine. "
    "Final Cut finds the files only when those paths resolve. Otherwise use "
    "Relink Files. jevid does not copy media and does not edit the originals."
)

ProbeFn = Callable[[Path], "Probe | Skip | None"]


@dataclass(frozen=True)
class Probe:
    duration: Fraction
    width: int | None = None
    height: int | None = None
    frame_duration: Fraction | None = None
    has_video: bool = True
    has_audio: bool = True


@dataclass(frozen=True)
class Skip:
    """ffprobe read the file and it has no video stream."""

    reason: str


@dataclass
class MediaClip:
    path: Path
    name: str
    stem: str
    src: str
    uid: str
    duration: Fraction
    duration_source: str
    frame_duration: Fraction
    width: int
    height: int
    has_video: bool
    has_audio: bool
    blake2b: str
    warning: str | None = None

    def to_state(self) -> dict:
        return {
            "name": self.name,
            "path": str(self.path),
            "src": self.src,
            "duration": format_time(self.duration),
            "duration_seconds": seconds(self.duration),
            "duration_source": self.duration_source,
            "frame_duration": format_time(self.frame_duration),
            "width": self.width,
            "height": self.height,
            "has_video": self.has_video,
            "has_audio": self.has_audio,
            "blake2b": self.blake2b,
            "warning": self.warning,
        }


@dataclass
class Inventory:
    media_dir: Path
    clips: list[MediaClip]
    skipped: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


@dataclass
class IngestResult:
    starter: Path
    inventory: Inventory
    report: Report
    warnings: list[str]


def file_url(path: Path) -> str:
    """Absolute ``file://`` URL. Spaces and other reserved characters are escaped."""
    resolved = path.resolve()
    return "file://" + quote(resolved.as_posix(), safe="/")


def load_duration_overrides(path: str | Path) -> dict[str, Fraction]:
    """Map of file name (not a path) to a positive duration."""
    file = Path(path)
    if not file.is_file():
        raise ConductorError(f"no such durations file: {file}")
    try:
        data = json.loads(file.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ConductorError(f"durations file is not JSON: {file}: {exc}") from exc
    if not isinstance(data, dict):
        raise ConductorError("durations file must be an object of file name to length")
    found: dict[str, Fraction] = {}
    for key, value in data.items():
        if not isinstance(key, str) or not key or "/" in key or "\\" in key:
            raise ConductorError(f"durations keys must be file names, not paths: {key!r}")
        found[key] = _duration_value(value, key)
    return found


def ffprobe_probe(path: Path) -> Probe | Skip | None:
    """Probe one file. None means ffprobe is missing or the file was unreadable."""
    binary = shutil.which("ffprobe")
    if binary is None:
        return None
    command = [
        binary,
        "-v",
        "error",
        "-print_format",
        "json",
        "-show_format",
        "-show_streams",
        str(path),
    ]
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if completed.returncode != 0 or not completed.stdout.strip():
        return None
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError:
        return None
    return parse_ffprobe(payload)


def parse_ffprobe(payload: object) -> Probe | Skip | None:
    """Turn ffprobe's JSON into a probe. A file with streams but no video is a skip."""
    if not isinstance(payload, dict):
        return None
    streams = payload.get("streams")
    if not isinstance(streams, list):
        return None
    video = next(
        (item for item in streams if isinstance(item, dict) and item.get("codec_type") == "video"),
        None,
    )
    if video is None:
        if streams:
            return Skip("no video stream")
        return None
    has_audio = any(
        isinstance(item, dict) and item.get("codec_type") == "audio" for item in streams
    )
    container = payload.get("format") if isinstance(payload.get("format"), dict) else {}
    raw_duration = container.get("duration")
    if raw_duration in (None, "", "N/A"):
        raw_duration = video.get("duration")
    try:
        duration = Fraction(str(raw_duration))
    except (TypeError, ValueError, ZeroDivisionError):
        return None
    if duration <= 0:
        return None
    frame = _frame_from_rate(video.get("avg_frame_rate")) or _frame_from_rate(
        video.get("r_frame_rate")
    )
    width = video.get("width") if isinstance(video.get("width"), int) and video.get("width") > 0 else None
    height = (
        video.get("height") if isinstance(video.get("height"), int) and video.get("height") > 0 else None
    )
    return Probe(duration, width, height, frame, True, has_audio)


def inventory(
    media_dir: str | Path,
    *,
    probe: ProbeFn | None = None,
    overrides: dict[str, Fraction] | None = None,
) -> Inventory:
    """List video files in ``media_dir`` itself, in filename order."""
    root = Path(media_dir)
    if root.is_file():
        raise ConductorError(f"{root} is a file. Pass the folder that contains the clips.")
    if not root.is_dir():
        raise ConductorError(f"no such media folder: {root}")
    root = root.resolve()
    overrides = overrides or {}
    probe_fn = ffprobe_probe if probe is None else probe
    paths, skipped = _video_files(root)
    if not paths:
        listed = ", ".join(sorted(VIDEO_EXTENSIONS))
        raise ConductorError(
            f"no video files in {root}. Looked for {listed} in this folder only, not subfolders."
        )
    warnings: list[str] = []
    known = {path.name for path in paths}
    for key in sorted(overrides):
        if key not in known:
            warnings.append(
                f"durations entry {key!r} does not match a video file in the folder; ignored."
            )
    ffprobe_missing = probe_fn is ffprobe_probe and shutil.which("ffprobe") is None
    if ffprobe_missing and any(path.name not in overrides for path in paths):
        warnings.append(
            "ffprobe is not on PATH. Clips without a --durations entry use a "
            f"{int(FALLBACK_DURATION)}s placeholder. Install ffmpeg for real lengths."
        )
    clips: list[MediaClip] = []
    stems = _stem_counts(paths)
    for path in paths:
        if path.name in overrides:
            built = _clip_from_probe(
                path,
                Probe(duration=overrides[path.name]),
                source="override",
                stem=_display_stem(path, stems),
            )
            clips.append(built)
            continue
        probed = probe_fn(path)
        if isinstance(probed, Skip):
            warnings.append(f"{path.name}: {probed.reason}; skipped.")
            skipped.append(path.name)
            continue
        if probed is None:
            warning = _fallback_warning(path.name, ffprobe_missing)
            if not ffprobe_missing:
                warnings.append(warning)
            clips.append(
                _clip_from_probe(
                    path,
                    Probe(duration=FALLBACK_DURATION),
                    source="fallback",
                    stem=_display_stem(path, stems),
                    warning=warning,
                )
            )
            continue
        source = "ffprobe" if probe_fn is ffprobe_probe else "probe"
        clips.append(
            _clip_from_probe(
                path,
                probed,
                source=source,
                stem=_display_stem(path, stems),
            )
        )
    if not clips:
        raise ConductorError(f"no video clips to sequence in {root}")
    return Inventory(root, clips, skipped, warnings)


def ingest(
    media_dir: str | Path,
    *,
    out_dir: str | Path,
    brief: str | None = None,
    transcript_path: str | Path | None = None,
    taste_path: str | Path | None = None,
    durations_path: str | Path | None = None,
    name: str | None = None,
    live: bool = False,
    html: bool = False,
    passes: list[str] | None = None,
    apply: bool = False,
    accept: list[str] | None = None,
    min_confidence: float | None = None,
    probe: ProbeFn | None = None,
) -> IngestResult:
    """Inventory ``media_dir``, write a starter FCPXML, and run ``analyze`` on it.

    ``apply=True`` writes a second FCPXML through the same gates as
    ``conductor.analyze(..., apply=True)``. Shadow markers are the default.
    """
    overrides = load_duration_overrides(durations_path) if durations_path else None
    found = inventory(media_dir, probe=probe, overrides=overrides)
    sequence_name = (name or found.media_dir.name).strip() or "Selects"
    destination = Path(out_dir)
    try:
        destination.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise ConductorError(f"cannot create {destination}: {exc}") from exc
    starter = destination / f"{slug(sequence_name)}.fcpxml"
    _refuse_media_overwrite(found, starter)
    before = {clip.path: clip.blake2b for clip in found.clips}
    tree = render_starter(found.clips, name=sequence_name)
    write_document(tree, starter)
    text = brief.strip() if brief and brief.strip() else DEFAULT_BRIEF
    report = analyze(
        starter,
        transcript_path=transcript_path,
        brief=text,
        out_dir=destination,
        live=live,
        html=html,
        passes=passes,
        taste_path=taste_path,
        apply=apply,
        accept=accept,
        min_confidence=min_confidence,
    )
    info = _payload(found, starter, sequence_name)
    _attach(report, info)
    _assert_media_unchanged(before)
    return IngestResult(starter, found, report, list(found.warnings))


def render_starter(clips: list[MediaClip], *, name: str) -> ET.ElementTree:
    """A library/event/project sequence. One spine, butt-joined, no transitions."""
    if not clips:
        raise ConductorError("refusing to write an empty sequence")
    root = ET.Element("fcpxml", {"version": "1.11"})
    resources = ET.SubElement(root, "resources")
    formats: dict[tuple[Fraction, int, int], str] = {}
    next_id = 1
    format_of: list[str] = []
    for clip in clips:
        key = (clip.frame_duration, clip.width, clip.height)
        if key not in formats:
            ident = f"r{next_id}"
            next_id += 1
            formats[key] = ident
            ET.SubElement(
                resources,
                "format",
                {
                    "id": ident,
                    "name": _format_name(clip),
                    "frameDuration": format_time(clip.frame_duration),
                    "width": str(clip.width),
                    "height": str(clip.height),
                },
            )
        format_of.append(formats[key])

    asset_ids: list[str] = []
    for clip, format_ref in zip(clips, format_of):
        ident = f"r{next_id}"
        next_id += 1
        asset_ids.append(ident)
        attrs = {
            "id": ident,
            "name": clip.stem,
            "uid": clip.uid,
            "start": "0s",
            "duration": format_time(clip.duration),
            "hasVideo": "1" if clip.has_video else "0",
            "hasAudio": "1" if clip.has_audio else "0",
            "format": format_ref,
        }
        if clip.has_audio:
            attrs["audioSources"] = "1"
            attrs["audioChannels"] = "2"
            attrs["audioRate"] = "48000"
        asset = ET.SubElement(resources, "asset", attrs)
        ET.SubElement(
            asset,
            "media-rep",
            {"kind": "original-media", "src": clip.src},
        )

    library = ET.SubElement(root, "library")
    event = ET.SubElement(library, "event", {"name": name, "uid": _uid("event", name)})
    project = ET.SubElement(event, "project", {"name": name, "uid": _uid("project", name)})
    total = sum((clip.duration for clip in clips), Fraction(0))
    first = clips[0]
    sequence = ET.SubElement(
        project,
        "sequence",
        {
            "format": formats[(first.frame_duration, first.width, first.height)],
            "duration": format_time(total),
            "tcStart": "0s",
            "tcFormat": "NDF",
        },
    )
    spine = ET.SubElement(sequence, "spine")
    offset = Fraction(0)
    for clip, ident in zip(clips, asset_ids):
        attrs = {
            "ref": ident,
            "offset": format_time(offset),
            "name": clip.stem,
            "start": "0s",
            "duration": format_time(clip.duration),
            "tcFormat": "NDF",
        }
        if clip.has_audio:
            attrs["audioRole"] = "dialogue"
        ET.SubElement(spine, "asset-clip", attrs)
        offset += clip.duration
    return ET.ElementTree(root)


def slug(name: str) -> str:
    cleaned = []
    for char in name:
        if char.isascii() and (char.isalnum() or char in "._-"):
            cleaned.append(char)
        else:
            cleaned.append("-")
    text = "".join(cleaned).strip("-.")
    while "--" in text:
        text = text.replace("--", "-")
    return text or "selects"


def quantize(duration: Fraction, frame: Fraction) -> Fraction:
    """Nearest whole frame, at least one. Probe noise should not drop a frame."""
    duration = Fraction(duration)
    frame = Fraction(frame)
    if frame <= 0:
        frame = DEFAULT_FRAME
    if duration <= 0:
        return frame
    count = int((duration / frame) + Fraction(1, 2))
    if count < 1:
        count = 1
    return count * frame


def file_hash(path: Path) -> str:
    digest = hashlib.blake2b(digest_size=16)
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _video_files(root: Path) -> tuple[list[Path], list[str]]:
    paths: list[Path] = []
    skipped: list[str] = []
    for entry in root.iterdir():
        if entry.name.startswith("."):
            continue
        if entry.is_dir() or not entry.is_file():
            continue
        if entry.suffix.lower() not in VIDEO_EXTENSIONS:
            skipped.append(entry.name)
            continue
        paths.append(entry)
    paths.sort(key=lambda item: (item.name.casefold(), item.name))
    skipped.sort()
    return paths, skipped


def _stem_counts(paths: list[Path]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for path in paths:
        counts[path.stem] = counts.get(path.stem, 0) + 1
    return counts


def _display_stem(path: Path, counts: dict[str, int]) -> str:
    if counts.get(path.stem, 0) > 1:
        return path.name
    return path.stem


def _clip_from_probe(
    path: Path,
    probed: Probe,
    *,
    source: str,
    stem: str,
    warning: str | None = None,
) -> MediaClip:
    frame = probed.frame_duration if probed.frame_duration and probed.frame_duration > 0 else DEFAULT_FRAME
    width = probed.width if probed.width and probed.width > 0 else DEFAULT_WIDTH
    height = probed.height if probed.height and probed.height > 0 else DEFAULT_HEIGHT
    duration = quantize(probed.duration, frame)
    resolved = path.resolve()
    return MediaClip(
        path=resolved,
        name=path.name,
        stem=stem,
        src=file_url(resolved),
        uid=_uid("asset", str(resolved)),
        duration=duration,
        duration_source=source,
        frame_duration=frame,
        width=width,
        height=height,
        has_video=probed.has_video,
        has_audio=probed.has_audio,
        blake2b=file_hash(resolved),
        warning=warning,
    )


def _fallback_warning(name: str, ffprobe_missing: bool) -> str:
    if ffprobe_missing:
        why = "ffprobe is not installed"
    else:
        why = "ffprobe could not read the file"
    return (
        f"{name}: {why}; duration is a {int(FALLBACK_DURATION)}s placeholder, "
        "not the picture's length."
    )


def _payload(found: Inventory, starter: Path, sequence_name: str) -> dict:
    return {
        "media_dir": str(found.media_dir),
        "sequence_name": sequence_name,
        "order": "filename",
        "fallback_seconds": int(FALLBACK_DURATION),
        "extensions": sorted(VIDEO_EXTENSIONS),
        "starter_fcpxml": str(starter.resolve()),
        "relink": RELINK,
        "clips": [clip.to_state() for clip in found.clips],
        "skipped": list(found.skipped),
        "warnings": list(found.warnings),
    }


def _attach(report: Report, info: dict) -> None:
    report.payload = dict(report.payload)
    report.payload["ingest"] = info
    if report.out_json is not None:
        report.out_json.write_text(dumps(report.payload), encoding="utf-8")
    if report.out_markdown is not None:
        report.out_markdown.write_text(
            report.out_markdown.read_text(encoding="utf-8") + _markdown(info),
            encoding="utf-8",
        )


def _markdown(info: dict) -> str:
    lines = [
        "",
        "## Ingest",
        "",
        f"- Folder: `{info['media_dir']}`",
        f"- Order: `{info['order']}`",
        f"- Starter: `{info['starter_fcpxml']}`",
        f"- {info['relink']}",
        "",
        "| clip | duration | source | path |",
        "|---|---|---|---|",
    ]
    for clip in info["clips"]:
        lines.append(
            f"| {clip['name']} | {clip['duration']} | {clip['duration_source']} | `{clip['path']}` |"
        )
    if info["warnings"]:
        lines.extend(["", "### Duration limits", ""])
        for warning in info["warnings"]:
            lines.append(f"- {warning}")
    if info["skipped"]:
        lines.extend(["", "Ignored (not a video container, or no video stream):", ""])
        for name in info["skipped"]:
            lines.append(f"- `{name}`")
    lines.append("")
    return "\n".join(lines)


def _assert_media_unchanged(before: dict[Path, str]) -> None:
    for path, digest in before.items():
        if not path.is_file() or file_hash(path) != digest:
            raise ConductorError(f"refusing to finish: source media changed: {path}")


def _refuse_media_overwrite(found: Inventory, starter: Path) -> None:
    starter_resolved = starter.resolve()
    for clip in found.clips:
        if starter_resolved == clip.path:
            raise ConductorError(
                "refusing to write the starter FCPXML over a source clip; "
                "choose a different --out-dir"
            )


def _duration_value(value: object, key: str) -> Fraction:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise ConductorError(f"duration for {key!r} must be seconds or an FCP time such as 8s")
    if isinstance(value, str):
        try:
            parsed = parse_time(value.strip())
        except ValueError as exc:
            raise ConductorError(f"duration for {key!r} is not an FCP time: {value!r}") from exc
    else:
        try:
            parsed = Fraction(str(value))
        except (ValueError, ZeroDivisionError) as exc:
            raise ConductorError(f"duration for {key!r} is not a number") from exc
    if parsed <= 0:
        raise ConductorError(f"duration for {key!r} must be positive")
    return parsed


def _frame_from_rate(rate: object) -> Fraction | None:
    if not isinstance(rate, str) or "/" not in rate:
        return None
    num, den = rate.split("/", 1)
    if not num.lstrip("-").isdigit() or not den.lstrip("-").isdigit():
        return None
    numerator, denominator = int(num), int(den)
    if numerator <= 0 or denominator <= 0:
        return None
    return Fraction(denominator, numerator)


def _format_name(clip: MediaClip) -> str:
    if clip.width == 1920 and clip.height == 1080 and clip.frame_duration == Fraction(1, 24):
        return "FFVideoFormat1080p24"
    return f"FFVideoFormat{clip.height}p"


def _uid(kind: str, text: str) -> str:
    digest = hashlib.blake2b(f"{kind}\0{text}".encode(), digest_size=8).hexdigest()
    return digest.upper()
