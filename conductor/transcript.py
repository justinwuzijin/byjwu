"""SRT and WebVTT cues, aligned to sequence time.

Times become fractions of a second so a cue boundary can be compared with a
clip edge without float drift. Tags are stripped only for the text the
heuristics read; the cue keeps the plain words.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path

from .errors import ConductorError

_ARROW_RE = re.compile(
    r"(?P<a>(?:\d{1,2}:)?\d{2}:\d{2}[.,]\d{1,3})\s*-->\s*"
    r"(?P<b>(?:\d{1,2}:)?\d{2}:\d{2}[.,]\d{1,3})"
)
_TAG_RE = re.compile(r"<[^>]+>")


@dataclass(frozen=True)
class Cue:
    start: Fraction
    end: Fraction
    text: str

    @property
    def duration(self) -> Fraction:
        return self.end - self.start


def load_transcript(path: str | Path) -> list[Cue]:
    file = Path(path)
    if not file.is_file():
        raise ConductorError(f"no such transcript: {file}")
    try:
        text = file.read_text(encoding="utf-8-sig")
    except UnicodeError as exc:
        raise ConductorError(f"transcript is not UTF-8: {file}") from exc
    cues = parse_cues(text)
    if not cues:
        raise ConductorError(f"no cues in transcript: {file}")
    return cues


def parse_cues(text: str) -> list[Cue]:
    """Read either SRT or WebVTT. A block is a cue when it contains ``-->``."""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    cues: list[Cue] = []
    for block in re.split(r"\n\s*\n", text):
        lines = [line.strip() for line in block.split("\n") if line.strip()]
        if not lines:
            continue
        if lines[0].startswith(("WEBVTT", "NOTE", "STYLE", "REGION")):
            continue
        arrow_at = next((i for i, line in enumerate(lines) if "-->" in line), None)
        if arrow_at is None:
            continue
        match = _ARROW_RE.search(lines[arrow_at])
        if not match:
            continue
        start = _clock(match.group("a"))
        end = _clock(match.group("b"))
        body = _plain(" ".join(lines[arrow_at + 1 :]))
        if not body or end <= start:
            continue
        cues.append(Cue(start, end, body))
    cues.sort(key=lambda cue: (cue.start, cue.end, cue.text))
    return cues


def _plain(text: str) -> str:
    return _TAG_RE.sub("", text).strip()


def _clock(value: str) -> Fraction:
    raw = value.strip().replace(",", ".")
    parts = raw.split(":")
    if len(parts) == 3:
        hour, minute, sec = parts
    elif len(parts) == 2:
        hour, minute, sec = "0", parts[0], parts[1]
    else:
        raise ConductorError(f"unreadable cue time: {value!r}")
    if "." in sec:
        whole, frac = sec.split(".", 1)
        frac = (frac + "000")[:3]
    else:
        whole, frac = sec, "000"
    millis = (int(hour) * 3600 + int(minute) * 60 + int(whole)) * 1000 + int(frac)
    return Fraction(millis, 1000)
