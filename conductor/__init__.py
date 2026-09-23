"""Cut Conductor — editorial co-pilot for a Final Cut timeline.

FCPXML in, proposal markers out, by default. Passes (mechanical, dialogue,
pacing) can run alone or in sequence. An explicit apply writes a second
FCPXML with accepted trims. The file you hand in is never modified.

``analyze`` is the library boundary. The CLI is ``python -m conductor``.
"""

from __future__ import annotations

__version__ = "0.1.0"

from .errors import ConductorError
from .run import analyze

__all__ = ["ConductorError", "analyze", "__version__"]
