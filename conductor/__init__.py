"""Cut Conductor — editorial co-pilot for a Final Cut timeline.

FCPXML in, proposal markers out, by default. ``ingest`` can build that
FCPXML from a folder of clips first. Passes (mechanical, dialogue, pacing)
can run alone or in sequence. An explicit apply writes another FCPXML with
accepted trims. The file you hand in, and the source clips, are never modified.

``analyze`` and ``ingest`` are the library boundary. The CLI is
``python -m conductor``.
"""

from __future__ import annotations

__version__ = "0.1.0"

from .errors import ConductorError
from .ingest import ingest
from .run import analyze

__all__ = ["ConductorError", "analyze", "ingest", "__version__"]
