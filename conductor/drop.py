"""The editor's drop folder.

People do not run commands. They put an FCPXML, or a folder of clips, in
``~/Desktop/jevid-in``. The Conductor bot reads that folder and writes
``~/Desktop/jevid-out``. This module only resolves paths. It does not watch
the folder and it does not talk to Final Cut.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .errors import ConductorError
from .ingest import VIDEO_EXTENSIONS

DROP_IN_NAME = "jevid-in"
DROP_OUT_NAME = "jevid-out"
DESKTOP_IN = Path.home() / "Desktop" / DROP_IN_NAME
DESKTOP_OUT = Path.home() / "Desktop" / DROP_OUT_NAME

_TRANSCRIPTS = frozenset({".srt", ".vtt"})


@dataclass(frozen=True)
class Drop:
    folder: Path
    fcpxml: Path | None
    media_dir: Path | None
    brief: str | None
    transcript: Path | None
    taste: Path | None
    durations: Path | None


def output_dir_for(folder: Path, explicit: str | Path | None = None) -> Path:
    """Where the bot writes. ``jevid-in`` maps to a sibling ``jevid-out``."""
    if explicit is not None and str(explicit).strip():
        return Path(explicit).expanduser()
    folder = Path(folder).expanduser()
    if folder.name == DROP_IN_NAME:
        return folder.parent / DROP_OUT_NAME
    return folder.parent / f"{folder.name}-out"


def resolve_drop(folder: str | Path) -> Drop:
    """Read one drop folder. An export and a pile of clips are not combined."""
    root = Path(folder).expanduser()
    if not root.is_dir():
        raise ConductorError(f"no such drop folder: {root}")
    xmls: list[Path] = []
    videos: list[Path] = []
    transcripts: list[Path] = []
    for entry in root.iterdir():
        if entry.name.startswith(".") or not entry.is_file():
            continue
        suffix = entry.suffix.lower()
        if suffix == ".fcpxml":
            xmls.append(entry)
        elif suffix in VIDEO_EXTENSIONS:
            videos.append(entry)
        elif suffix in _TRANSCRIPTS:
            transcripts.append(entry)
    if len(xmls) > 1:
        names = ", ".join(path.name for path in sorted(xmls))
        raise ConductorError(f"drop folder has more than one FCPXML ({names}). Leave one.")
    if len(transcripts) > 1:
        names = ", ".join(path.name for path in sorted(transcripts))
        raise ConductorError(f"drop folder has more than one transcript ({names}). Leave one.")
    if xmls and videos:
        raise ConductorError(
            "drop either one FCPXML or a folder of clips, not both. "
            f"Found {xmls[0].name} and {len(videos)} video file(s)."
        )
    if not xmls and not videos:
        raise ConductorError(
            f"nothing to cut in {root}. Drop one .fcpxml export, or video files "
            f"({', '.join(sorted(VIDEO_EXTENSIONS))})."
        )
    brief_path = root / "brief.txt"
    brief = None
    if brief_path.is_file():
        brief = brief_path.read_text(encoding="utf-8").strip() or None
    taste = root / "taste.json"
    durations = root / "durations.json"
    return Drop(
        folder=root.resolve(),
        fcpxml=xmls[0].resolve() if xmls else None,
        media_dir=root.resolve() if videos else None,
        brief=brief,
        transcript=transcripts[0].resolve() if transcripts else None,
        taste=taste.resolve() if taste.is_file() else None,
        durations=durations.resolve() if durations.is_file() else None,
    )
