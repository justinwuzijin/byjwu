"""Type and graphics for a byjwu timeline: subtitles, distorted titles, rectangles.

``apply_graphics`` is the stage ``iterate``, ``room-run``, and ``assemble`` call.
It stays off unless the style profile sets ``graphics.enabled`` or the caller
passes ``enabled=True``.
"""

from .profile import GraphicsProfile, load_graphics_profile
from .stage import GraphicsResult, apply_graphics

__all__ = ["GraphicsProfile", "GraphicsResult", "apply_graphics", "load_graphics_profile"]
