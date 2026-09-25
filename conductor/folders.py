"""The Desktop drop folders the room bots read from and write to.

``~/Desktop/byjwu-in`` holds a selects folder's clips or one FCPXML export.
``~/Desktop/byjwu-out`` receives the rounds. Machines set up before the rename
still have ``jevid-in`` / ``jevid-out``; those are used only when the new
folder is missing and the legacy one exists.
"""

from __future__ import annotations

from pathlib import Path

from .errors import ConductorError

DROP_IN = "byjwu-in"
DROP_OUT = "byjwu-out"
LEGACY = {DROP_IN: "jevid-in", DROP_OUT: "jevid-out"}


def drop_folder(name: str, *, home: str | Path | None = None) -> tuple[Path, str | None]:
    """Return the folder to use and a one-line note when it is the legacy one."""
    desktop = Path(home if home is not None else Path.home()) / "Desktop"
    current = desktop / name
    legacy = desktop / LEGACY[name]
    if not current.exists() and legacy.is_dir():
        return legacy, f"using legacy {legacy}; rename it to {current.name}"
    return current, None


def drop_input(folder: Path) -> dict[str, Path]:
    """``{"fcpxml": file}`` when the folder holds one export, else ``{"media": folder}``."""
    if not folder.is_dir():
        raise ConductorError(f"no --fcpxml or --media, and {folder} does not exist")
    exports = sorted(p for p in folder.iterdir() if p.is_file() and p.suffix.lower() == ".fcpxml")
    if len(exports) > 1:
        raise ConductorError(f"{folder} holds {len(exports)} FCPXML files; leave one, or pass --fcpxml")
    if exports:
        return {"fcpxml": exports[0]}
    return {"media": folder}
