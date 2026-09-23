"""FCPXML rational times and display timecode.

Final Cut stores time as seconds: ``8s`` or ``1001/24000s``. Arithmetic stays
in :class:`fractions.Fraction` so a marker lands on the same frame we measured.
"""

from __future__ import annotations

import math
import re
from fractions import Fraction

_TIME_RE = re.compile(
    r"^\s*(?:(?P<dec>-?\d+(?:\.\d+)?)s|(?P<num>-?\d+)\s*/\s*(?P<den>-?\d+)s)\s*$"
)


def parse_time(value: str | None, default: Fraction | None = None) -> Fraction:
    """Parse an FCPXML time attribute. Missing values return `default`."""
    if value is None or str(value).strip() == "":
        if default is None:
            raise ValueError("missing FCP time")
        return default
    match = _TIME_RE.match(str(value).strip())
    if not match:
        raise ValueError(f"unreadable FCP time: {value!r}")
    if match.group("dec") is not None:
        return Fraction(match.group("dec"))
    den = int(match.group("den"))
    if den == 0:
        raise ValueError(f"FCP time has a zero denominator: {value!r}")
    return Fraction(int(match.group("num")), den)


def format_time(value: Fraction) -> str:
    """Write a Fraction back out as FCPXML time."""
    value = Fraction(value)
    if value.denominator == 1:
        return f"{value.numerator}s"
    return f"{value.numerator}/{value.denominator}s"


def seconds(value: Fraction) -> float:
    """A JSON-stable float. Six decimals is finer than a frame and exact for our fixtures."""
    return round(float(value), 6)


def clock(pos: Fraction) -> str:
    """``HH:MM:SS.mmm`` from a timeline position."""
    ms_total = int((Fraction(pos) * 1000).to_integral_value())
    if ms_total < 0:
        ms_total = 0
    ms = ms_total % 1000
    sec_total = ms_total // 1000
    sec = sec_total % 60
    minute = (sec_total // 60) % 60
    hour = sec_total // 3600
    return f"{hour:02d}:{minute:02d}:{sec:02d}.{ms:03d}"


def short_clock(pos: Fraction) -> str:
    """``MM:SS`` (or ``H:MM:SS``) for a marker name. Floors to the whole second."""
    whole = int(Fraction(pos))
    if whole < 0:
        whole = 0
    hour, rem = divmod(whole, 3600)
    minute, sec = divmod(rem, 60)
    if hour:
        return f"{hour}:{minute:02d}:{sec:02d}"
    return f"{minute:02d}:{sec:02d}"


def smpte(pos: Fraction, frame: Fraction, origin: Fraction = Fraction(0)) -> str:
    """Non-drop ``HH:MM:SS:FF`` when the frame duration is an integer fps.

    Fractional rates (24000/1001 and friends) would need drop-frame rules this
    writer does not implement, so those fall back to a milliseconds clock.
    """
    frame = Fraction(frame)
    if frame <= 0:
        frame = Fraction(1, 24)
    total = Fraction(pos) + Fraction(origin)
    fps = Fraction(1, 1) / frame
    if fps.denominator != 1:
        return clock(total)
    rate = fps.numerator
    index = math.floor(total / frame)
    if index < 0:
        index = 0
    frames = index % rate
    sec_total = index // rate
    sec = sec_total % 60
    minute = (sec_total // 60) % 60
    hour = sec_total // 3600
    return f"{hour:02d}:{minute:02d}:{sec:02d}:{frames:02d}"
