"""Read a re-export for the style parameters the profile can learn.

Registered with :func:`conductor.feedback.register_observer`. Each row is an
``observe`` event: what the edited timeline actually did, not a judgment.
Shot length is per section. Music fade lengths and duck depth come from
volume keyframes. A timeline with neither section markers nor music
keyframes yields nothing, so a mechanical interview diff stays a candidate log.
"""

from __future__ import annotations

from .metrics import measure_root
from ..feedback import register_observer
from ..style import load_style

OBSERVER = "style"


def style_observations(proposed, edited) -> list[dict]:
    """``(proposed, edited) -> observations``. ``proposed`` is unused; the edit is the evidence."""
    del proposed
    profile = load_style("byjustinwu")
    metrics = measure_root(edited.tree.getroot(), profile, beats=[])
    rows: list[dict] = []
    if metrics.get("section_markers"):
        for kind, seconds in (metrics.get("asl_by_section") or {}).items():
            if not seconds:
                continue
            rows.append({"param": f"pacing.{kind}.asl_seconds", "section": kind, "value": seconds})
    music = metrics.get("music") or {}
    fades = music.get("fades") or []
    if fades and fades[0]["fade_in"][1] is not None:
        rows.append({"param": "music.fade_in.seconds", "section": "intro", "value": fades[0]["fade_in"][1]})
    if fades and fades[-1]["fade_out"][1] is not None:
        rows.append({"param": "music.fade_out.seconds", "section": "outro", "value": fades[-1]["fade_out"][1]})
    depth = (metrics.get("ducking") or {}).get("depth_db")
    if depth is not None:
        rows.append({"param": "music.duck.depth_db", "section": "talking", "value": depth})
    return rows


def register() -> None:
    register_observer(OBSERVER, style_observations)


register()
