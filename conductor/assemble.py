"""The assembler room-run imports: ``conductor.assemble.assemble``.

The pipeline lives in :mod:`conductor.assembly`. room-run calls this when a
drop carries music, and passes only the keywords named below. The return
value's ``fcpxml`` attribute is the file it then iterates.
"""

from pathlib import Path

from .assembly.engine import AssemblyResult, assemble as _assemble
from .router import Router


def assemble(
    *,
    out_dir: str | Path,
    media: str | Path | None = None,
    fcpxml: str | Path | None = None,
    music: str | Path | list | tuple | None = None,
    style: str | Path | None = None,
    brief: str | None = None,
    live: bool = False,
    transcript_path: str | Path | None = None,
    taste_path: str | Path | None = None,
    durations_path: str | Path | None = None,
    router: Router | None = None,
) -> AssemblyResult:
    """Build one timeline. A clip folder wins when a sibling FCPXML is also passed.

    ``style`` defaults to ``byjustinwu`` inside the engine. ``music`` may be
    one file, a folder, or the list of files room-run found.
    """
    if media and fcpxml:
        fcpxml = None
    return _assemble(
        out_dir=out_dir,
        media=media,
        fcpxml=fcpxml,
        music=music,
        style=style,
        brief=brief,
        live=live,
        transcript_path=transcript_path,
        taste_path=taste_path,
        durations_path=durations_path,
        router=router,
    )


__all__ = ["assemble"]
