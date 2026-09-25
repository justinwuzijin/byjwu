"""Word timings on a sequence clock, for subtitles and anything else that needs them.

This is the stable way to read what the media-signal stage heard::

    from conductor.words import timeline_words, words_payload

    words = timeline_words("cut.fcpxml")              # cache first, then a local tool
    words = timeline_words("cut.fcpxml", transcribe="cached")   # cache only, never a tool

Each item is a ``TimelineWord``. ``start`` and ``end`` are exact ``Fraction``
seconds on the sequence clock of that FCPXML, the same clock as spine
``offset`` values (so a sequence with ``tcStart="3600s"`` starts at 3600).
``file_start`` / ``file_end`` are seconds into the source file, which is what
the cache stores. Mapping from the file onto a timeline happens on every
call, so the same cached words line up with v0, an applied vN, or any other
FCPXML that uses the same media. A word cut part way by an edit is kept with
the heard part only and ``partial`` set.

Only words from a local transcription exist here. An SRT or WebVTT has cue
timings, not word timings, and is not converted. Nothing here contacts the
network or downloads a model.

``words_payload`` is the JSON the room writes beside a report as
``<stem>.words.json``. Its shape is ``cut-conductor.words`` version 1:

``protocol``, ``protocol_version``, ``source`` (the FCPXML path), ``tool``
(``whisper.cpp`` / ``faster-whisper`` / null), ``sequences`` (name,
``tc_start``, ``frame_duration`` as FCPXML time strings), and ``words``, each
with ``text``, ``start`` / ``end`` (FCPXML rational time strings),
``start_seconds`` / ``end_seconds`` (floats rounded to microseconds),
``sequence``, ``clip_id``, ``clip_name``, ``connected``, ``src``,
``file_start_seconds``, ``file_end_seconds``, ``partial``, and
``confidence`` (0..1 from faster-whisper, null from whisper.cpp).
"""

from __future__ import annotations

import json
from pathlib import Path

from .fcpxml import Document, parse_fcpxml
from .signals import SignalReport, TimelineWord, gather
from .timeutil import format_time

PROTOCOL = "cut-conductor.words"
PROTOCOL_VERSION = 1

__all__ = [
    "PROTOCOL",
    "PROTOCOL_VERSION",
    "TimelineWord",
    "timeline_words",
    "words_for_document",
    "words_payload",
    "write_words",
]


def timeline_words(
    fcpxml: str | Path,
    *,
    project: str | None = None,
    transcribe: str = "auto",
    cache_dir: str | Path | None = None,
) -> list[TimelineWord]:
    """Every heard word in ``fcpxml``, sorted by sequence then time.

    ``transcribe`` is ``auto`` (cache, then a local tool if one is set up),
    ``cached`` (cache only), or ``off`` (always empty).
    """
    document = parse_fcpxml(fcpxml)
    return words_for_document(document, project=project, transcribe=transcribe, cache_dir=cache_dir).words


def words_for_document(
    document: Document,
    *,
    project: str | None = None,
    transcribe: str = "auto",
    cache_dir: str | Path | None = None,
) -> SignalReport:
    """The signal report for words only. ``report.words`` is the list; ``report.reasons`` says why it is empty."""
    sequences = [sequence for sequence in document.sequences if project is None or sequence.name == project]
    return gather(document, sequences, signals="off", transcribe=transcribe, cache_dir=cache_dir)


def words_payload(document: Document, report: SignalReport) -> dict:
    names = {word.sequence for word in report.words}
    return {
        "protocol": PROTOCOL,
        "protocol_version": PROTOCOL_VERSION,
        "source": str(document.source) if document.source is not None else None,
        "tool": report.whisper_tool,
        "sequences": [
            {
                "name": sequence.name,
                "tc_start": format_time(sequence.tc_start),
                "frame_duration": format_time(sequence.frame_duration),
            }
            for sequence in document.sequences
            if sequence.name in names
        ],
        "words": [word.to_dict() for word in report.words],
    }


def write_words(path: str | Path, document: Document, report: SignalReport) -> Path:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(words_payload(document, report), indent=2) + "\n", encoding="utf-8")
    return target
