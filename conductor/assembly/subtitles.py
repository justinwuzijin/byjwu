"""Turn transcript cues into subtitle cards that fit the profile's line rules.

Input cues are already on the timeline clock. A long cue is split at word
boundaries so each card is at most ``max_lines`` lines of at most
``max_chars_per_line`` characters; its time is shared by character count.
Cards shorter than ``min_seconds`` borrow from the gap after them (never
overlapping the next card), and cards longer than ``max_seconds`` split.
"""

from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction

from cutmcp.extract import is_trivial_filler

from ..transcript import Cue


@dataclass(frozen=True)
class Card:
    start: Fraction
    end: Fraction
    text: str
    lines: tuple[str, ...]

    @property
    def duration(self) -> Fraction:
        return self.end - self.start


def apply_case(text: str, case: str) -> str:
    if case == "upper":
        return text.upper()
    if case == "lower":
        return text.lower()
    if case == "sentence":
        stripped = text.strip()
        return stripped[:1].upper() + stripped[1:].lower() if stripped else stripped
    if case == "title":
        return " ".join(word[:1].upper() + word[1:].lower() for word in text.split())
    return text


def cards_from_cues(
    cues: list[Cue],
    *,
    max_chars: int,
    max_lines: int,
    min_seconds: Fraction,
    max_seconds: Fraction,
    frame: Fraction,
    case: str = "as_is",
) -> list[Card]:
    pieces: list[tuple[Fraction, Fraction, list[str]]] = []
    for cue in sorted(cues, key=lambda c: (c.start, c.end)):
        text = " ".join(cue.text.split())
        if not text or is_trivial_filler(text) or cue.end <= cue.start:
            continue
        lines = wrap(text, max_chars)
        groups = [lines[i : i + max_lines] for i in range(0, len(lines), max_lines)]
        total = sum(len(" ".join(group)) for group in groups) or 1
        cursor = cue.start
        for index, group in enumerate(groups):
            share = Fraction(len(" ".join(group)), total)
            end = cue.end if index == len(groups) - 1 else cursor + (cue.end - cue.start) * share
            pieces.extend(_split_long(cursor, end, group, max_seconds, max_chars))
            cursor = end
    cards: list[Card] = []
    for index, (start, end, lines) in enumerate(pieces):
        next_start = pieces[index + 1][0] if index + 1 < len(pieces) else None
        if end - start < min_seconds:
            wanted = start + min_seconds
            end = min(wanted, next_start) if next_start is not None else wanted
        start_q = _snap(start, frame)
        end_q = max(start_q + frame, _snap(end, frame))
        if cards and start_q < cards[-1].end:
            start_q = cards[-1].end
            if end_q <= start_q:
                continue
        cased = tuple(apply_case(line, case) for line in lines)
        cards.append(Card(start_q, end_q, "\n".join(cased), cased))
    return cards


def break_choices(text: str, width: int) -> tuple[str, dict[str, str]]:
    """Legal first-line breaks, and the greedy one ``wrap`` would pick.

    A line with only one legal break is not a decision. Two or more are a
    linear ``subtitle_break`` call.
    """
    words = " ".join(text.replace("\n", " ").split()).split()
    if width < 1 or len(words) < 2:
        return "", {}
    options: dict[str, str] = {}
    greedy = ""
    for count in range(1, len(words)):
        first = " ".join(words[:count])
        if len(first) > width:
            break
        key = f"after-{count}"
        options[key] = f"“{first}” / {' '.join(words[count:])}"
        greedy = key
    if len(options) < 2:
        return "", {}
    return greedy, options


def apply_break(text: str, choice: str, width: int) -> list[str] | None:
    """Lines for a ``after-N`` choice. None when the choice is not legal."""
    words = " ".join(text.replace("\n", " ").split()).split()
    if not choice.startswith("after-"):
        return None
    try:
        count = int(choice.split("-", 1)[1])
    except ValueError:
        return None
    if count < 1 or count >= len(words):
        return None
    first = " ".join(words[:count])
    if len(first) > width:
        return None
    return [first, *wrap(" ".join(words[count:]), width)]


def wrap(text: str, width: int) -> list[str]:
    lines: list[str] = []
    current = ""
    for word in text.split():
        candidate = f"{current} {word}".strip()
        if len(candidate) <= width or not current:
            current = candidate
        else:
            lines.append(current)
            current = word
    if current:
        lines.append(current)
    return lines


def _split_long(
    start: Fraction, end: Fraction, lines: list[str], max_seconds: Fraction, max_chars: int
) -> list[tuple[Fraction, Fraction, list[str]]]:
    if end - start <= max_seconds or len(lines) > 1 or len(lines[0].split()) < 2:
        return [(start, end, lines)]
    words = lines[0].split()
    half = len(words) // 2
    first, second = " ".join(words[:half]), " ".join(words[half:])
    total = len(first) + len(second)
    middle = start + (end - start) * Fraction(len(first), total)
    return _split_long(start, middle, [first], max_seconds, max_chars) + _split_long(
        middle, end, [second], max_seconds, max_chars
    )


def _snap(value: Fraction, frame: Fraction) -> Fraction:
    return Fraction(round(value / frame)) * frame
