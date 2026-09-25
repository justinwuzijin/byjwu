"""Cut Conductor — editorial co-pilot for a Final Cut timeline.

FCPXML in, proposal markers out, by default. ``ingest`` can build that
FCPXML from a folder of clips first. Passes (mechanical, dialogue, pacing,
colour) can run alone or in sequence. ``iterate`` repeats analyze and
auto-applies only mechanical cuts. An explicit apply writes another FCPXML
with accepted trims. The file you hand in, and the source clips, are never
modified.

``analyze``, ``ingest``, and ``iterate`` are the library boundary. ``room_run``
is the bot entry point: it detects a drop and calls ``iterate``. The CLI is
``python -m conductor``.
"""

from __future__ import annotations

__version__ = "0.1.0"

from .errors import ConductorError
# The callable shares the submodule name. ``import conductor.ingest`` is this
# function; the module object remains ``sys.modules["conductor.ingest"]``.
from .ingest import ingest
from .iterate import iterate
from .room import room_run
from .run import analyze

__all__ = ["ConductorError", "analyze", "ingest", "iterate", "room_run", "__version__"]
