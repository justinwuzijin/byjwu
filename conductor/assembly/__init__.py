"""The jevid assembly engine: raw footage + music + brief + style → FCPXML.

Pipeline (each module owns one step):

- ``media``     inventory footage and music; per-clip signals (pluggable)
- ``beats``     beat grid per song (sidecar, WAV/ffmpeg detection, fallback)
- ``select``    usable ranges and the editorial decisions about them
- ``layout``    sections on the spine, beat-snapped cuts, split edits, music cursor
- ``dress``     subtitles, cards, text treatments, music keyframes, background
- ``render``    FCPXML 1.11
- ``metrics``   style metrics read back from any FCPXML
- ``refine``    iterate rounds v0..vN against those metrics
"""

from .engine import AssemblyResult, assemble, parse_target
from .layout import Adjustments
from .media import ClipSignals, register_signal_provider

__all__ = [
    "Adjustments",
    "AssemblyResult",
    "ClipSignals",
    "assemble",
    "parse_target",
    "register_signal_provider",
]
