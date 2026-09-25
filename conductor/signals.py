"""Optional silence, loudness, and a local transcript from reachable media.

Nothing here contacts the network. ffmpeg measures silence and loudness when
it is on ``PATH`` and a ``media-rep`` ``file://`` path exists on this machine.
A transcript is read from faster-whisper or whisper.cpp only when that tool
and a model are already on disk. A missing tool, a missing model, or a media
path that does not resolve is a skip recorded on the report, not a failed run.

Every range here is seconds into the media file (the asset's own ``start`` is
already subtracted by ``timing``). Results are cached per file, keyed by path,
size, and mtime, as a list of covered intervals. Only the parts of a file a
timeline uses are decoded, and a later ``iterate`` round reads the cache.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import shutil
import subprocess
import tempfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from fractions import Fraction
from pathlib import Path

from cutmcp.extract import is_trivial_filler

from .candidates import SILENCE_GAP, AudioSilence
from .errors import ConductorError
from .fcpxml import Document
from .fcpxml import Sequence as TimelineSequence
from .timeutil import format_time, seconds
from .timing import AudibleSpan, audible_spans, media_at, timeline_intervals
from .transcript import Cue

NOISE_DB = -40
MIN_SILENCE = Fraction(3, 10)
CLIP_PEAK_DB = -0.1
UTTERANCE_GAP = Fraction("0.35")
#: Used ranges closer than this are decoded in one pass.
MERGE_GAP = Fraction(5)
#: Context either side of a transcribed range, so edge words are whole.
WORD_PAD = Fraction(1)
CACHE_VERSION = 2
_MODES = frozenset({"auto", "on", "off"})
_TRANSCRIBE_MODES = frozenset({"auto", "on", "off", "cached"})
_NON_SPEECH_ROLES = ("music", "effects", "sfx")

Probe = Callable[[Path, Fraction, Fraction], "ProbeResult"]
Transcriber = Callable[[Path, Fraction, Fraction], list["Word"]]
Interval = tuple[Fraction, Fraction]


@dataclass(frozen=True)
class Word:
    """One recognised word, in seconds into its media file."""

    start: Fraction
    end: Fraction
    text: str
    confidence: float | None = None


@dataclass(frozen=True)
class TimelineWord:
    """One heard word on a sequence clock. See ``conductor.words``."""

    text: str
    start: Fraction
    end: Fraction
    sequence: str
    clip_id: str
    clip_name: str
    connected: bool
    src: str | None
    file_start: Fraction
    file_end: Fraction
    partial: bool = False
    confidence: float | None = None

    def to_dict(self) -> dict:
        return {
            "text": self.text,
            "start": format_time(self.start),
            "end": format_time(self.end),
            "start_seconds": seconds(self.start),
            "end_seconds": seconds(self.end),
            "sequence": self.sequence,
            "clip_id": self.clip_id,
            "clip_name": self.clip_name,
            "connected": self.connected,
            "src": self.src,
            "file_start_seconds": seconds(self.file_start),
            "file_end_seconds": seconds(self.file_end),
            "partial": self.partial,
            "confidence": self.confidence,
        }


@dataclass(frozen=True)
class Loudness:
    integrated_lufs: float | None
    true_peak_db: float | None
    clipping: bool


@dataclass(frozen=True)
class ProbeResult:
    silence: list[Interval]
    loudness: Loudness | None
    error: str | None = None


@dataclass
class SignalReport:
    audio: str
    transcript: str
    mode: str
    transcribe_mode: str
    ffmpeg: bool
    whisper_tool: str | None
    cache: str
    reasons: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    summary: str = ""
    clips: list[dict] = field(default_factory=list)
    unreachable: list[str] = field(default_factory=list)
    cues: list[Cue] = field(default_factory=list)
    words: list[TimelineWord] = field(default_factory=list)
    silences: list[AudioSilence] = field(default_factory=list)
    cache_hits: dict = field(default_factory=dict)

    def to_state(self) -> dict:
        return {
            "mode": self.mode,
            "transcribe": self.transcribe_mode,
            "audio": self.audio,
            "transcript": self.transcript,
            "ffmpeg": self.ffmpeg,
            "whisper_tool": self.whisper_tool,
            "cache": self.cache,
            "cache_hits": dict(self.cache_hits),
            "reasons": list(self.reasons),
            "summary": self.summary,
            "unreachable": list(self.unreachable),
            "word_count": len(self.words),
            "clips": self.clips,
        }


def default_cache_dir() -> Path:
    env = os.environ.get("CONDUCTOR_CACHE")
    if env:
        return Path(env)
    xdg = os.environ.get("XDG_CACHE_HOME")
    base = Path(xdg) if xdg else Path.home() / ".cache"
    return base / "conductor"


def gather(
    document: Document,
    sequences: Sequence[TimelineSequence],
    *,
    signals: str = "auto",
    transcribe: str = "auto",
    cache_dir: str | Path | None = None,
    transcript_supplied: bool = False,
    probe: Probe | None = None,
    transcriber: Transcriber | None = None,
    whisper_tool: str | None = None,
) -> SignalReport:
    """Read local media when the flags and the tools allow it.

    ``transcribe="cached"`` maps words already in the cache and never runs a
    tool. ``probe`` and ``transcriber`` are test seams; production leaves them
    unset. A transcriber that raises is recorded as a skip for that file.
    """
    mode = _mode(signals, "--signals", _MODES)
    transcribe_mode = _mode(transcribe, "--transcribe", _TRANSCRIBE_MODES)
    cache = Path(cache_dir) if cache_dir is not None else default_cache_dir()
    ffmpeg = shutil.which("ffmpeg") is not None
    spans = audible_spans(document, list(sequences))
    report = SignalReport(
        audio="skipped",
        transcript="skipped",
        mode=mode,
        transcribe_mode=transcribe_mode,
        ffmpeg=ffmpeg,
        whisper_tool=None,
        cache=str(cache),
        cache_hits={"silence": 0, "loudness": 0, "transcript": 0},
    )
    frames = {sequence.name: sequence.frame_duration for sequence in sequences}
    reachable = [span for span in spans if span.reachable and span.path is not None]
    report.unreachable = _unreachable(spans)

    measure = probe or probe_range
    silence_of: dict[Path, list[Interval]] = {}
    failed: set[Path] = set()
    if mode == "off":
        report.reasons.append("audio signals are off")
    elif not ffmpeg:
        report.reasons.append("ffmpeg is not on PATH")
    elif not reachable:
        report.reasons.append("no referenced media file is on this machine")
    else:
        report.audio = "used"
        silence_of, failed = _measure(report, reachable, cache, measure)
    for span in spans:
        report.clips.append(
            _clip_row(span, silence_of, failed, cache, measure, report)
        )
    if report.audio == "used":
        readable = [row for row in report.clips if row.get("reachable")]
        if readable and all(row.get("error") for row in readable):
            report.audio = "skipped"
            if not any("could not read audio" in reason for reason in report.reasons):
                report.reasons.append("ffmpeg could not read audio from the reachable files")
        else:
            report.silences = _spine_silences(spans, silence_of, failed, frames, report.clips)

    _transcribe(
        report,
        spans,
        reachable,
        cache,
        ffmpeg=ffmpeg,
        transcript_supplied=transcript_supplied,
        transcriber=transcriber,
        whisper_tool=whisper_tool,
    )
    report.summary = _summary(report)
    forced = (mode == "on" and report.audio == "skipped") or (
        transcribe_mode == "on" and report.transcript == "skipped" and not transcript_supplied
    )
    if forced:
        report.warnings.append(report.summary)
    return report


def words_to_cues(words: Sequence[Word] | Sequence[TimelineWord]) -> list[Cue]:
    """Group word timings into the cues the dialogue and pacing passes already read.

    A filler token is its own cue. Any other gap of at least 0.35s starts a
    new cue, so a pause of 0.80s or more is visible to the existing heuristic.
    """
    ordered = [word for word in words if word.text.strip() and word.end > word.start]
    ordered.sort(key=lambda word: (word.start, word.end, word.text))
    unique: list = []
    for word in ordered:
        if unique and (unique[-1].start, unique[-1].end, unique[-1].text) == (word.start, word.end, word.text):
            continue
        unique.append(word)
    if not unique:
        return []
    groups: list[list] = [[unique[0]]]
    for previous, word in zip(unique, unique[1:]):
        gap = word.start - previous.end
        split = (
            gap >= UTTERANCE_GAP
            or is_trivial_filler(previous.text)
            or is_trivial_filler(word.text)
        )
        if split:
            groups.append([word])
        else:
            groups[-1].append(word)
    cues: list[Cue] = []
    for group in groups:
        text = " ".join(word.text.strip() for word in group).strip()
        if not text or group[-1].end <= group[0].start:
            continue
        cues.append(Cue(group[0].start, group[-1].end, text))
    return cues


def probe_range(path: Path, start: Fraction, end: Fraction) -> ProbeResult:
    """One ffmpeg pass over seconds ``start``..``end`` of the file. No network."""
    binary = shutil.which("ffmpeg")
    if binary is None:
        return ProbeResult([], None, "ffmpeg is not on PATH")
    duration = end - start
    if duration <= 0:
        return ProbeResult([], None, "empty media range")
    silence = f"silencedetect=noise={NOISE_DB}dB:d={float(MIN_SILENCE):.2f}"
    text = ""
    for graph in (f"{silence},ebur128=peak=true:framelog=quiet", f"{silence},ebur128=peak=true", silence):
        completed = _ffmpeg(binary, path, start, duration, graph)
        if completed is None:
            return ProbeResult([], None, "ffmpeg did not finish")
        text = completed.stderr or ""
        if completed.returncode == 0:
            break
    else:
        return ProbeResult([], None, "ffmpeg could not read audio")
    return ProbeResult(_parse_silence(text, start, end), _parse_loudness(text), None)


def ffmpeg_available() -> bool:
    return shutil.which("ffmpeg") is not None


def whisper_available() -> bool:
    return resolve_whisper() is not None


def resolve_whisper() -> tuple[str, Transcriber] | None:
    """A local backend, or None. This does not download a model."""
    cpp = _cpp_backend()
    if cpp is not None:
        binary, model = cpp
        return "whisper.cpp", lambda path, start, end: _transcribe_cpp(binary, model, path, start, end)
    name = _faster_model_name()
    if name and _faster_importable():
        loaded: dict = {}

        def run(path: Path, start: Fraction, end: Fraction) -> list[Word]:
            if "model" not in loaded:
                from faster_whisper import WhisperModel

                loaded["model"] = WhisperModel(name, device="cpu", compute_type="int8", local_files_only=True)
            return _transcribe_faster(loaded["model"], path, start, end)

        return "faster-whisper", run
    return None


def is_speech_role(role: str | None) -> bool:
    """Music and effects roles are not transcribed and do not block a silence trim."""
    if not role:
        return True
    return not role.strip().lower().startswith(_NON_SPEECH_ROLES)


def _measure(report: SignalReport, reachable, cache: Path, probe: Probe):
    silence_of: dict[Path, list[Interval]] = {}
    failed: set[Path] = set()
    for path, group in _groups(reachable).items():
        needed = _needed(group, Fraction(0))
        if not needed:
            continue
        ranges, hit, error = _cached_silence(cache, path, needed, probe)
        if error:
            report.reasons.append(f"{path.name}: {error}")
            failed.add(path)
            continue
        silence_of[path] = ranges
        if hit:
            report.cache_hits["silence"] += 1
    return silence_of, failed


def _transcribe(
    report: SignalReport,
    spans: list[AudibleSpan],
    reachable: list[AudibleSpan],
    cache: Path,
    *,
    ffmpeg: bool,
    transcript_supplied: bool,
    transcriber: Transcriber | None,
    whisper_tool: str | None,
) -> None:
    if transcript_supplied:
        report.transcript = "file"
        report.reasons.append("an SRT or WebVTT was passed, so local transcription did not run")
        return
    if report.transcribe_mode == "off":
        report.reasons.append("local transcription is off")
        return
    speech = [span for span in reachable if is_speech_role(span.role)]
    if not speech:
        report.reasons.append("no reachable media to transcribe")
        return
    cached_only = report.transcribe_mode == "cached"
    tool = whisper_tool
    runner = transcriber
    if cached_only:
        runner = None
    elif not ffmpeg:
        report.reasons.append("ffmpeg is not on PATH, so a transcript range cannot be read")
        return
    elif runner is None:
        resolved = resolve_whisper()
        if resolved is None:
            report.reasons.append(_whisper_skip_reason())
            return
        tool, runner = resolved
    words_by_file: dict[Path, list[Word]] = {}
    for path, group in _groups(speech).items():
        needed = _needed(group, WORD_PAD)
        if not needed:
            continue
        try:
            words, hit, stored_tool = _cached_words(cache, path, needed, runner, tool)
        except Exception as exc:  # a local model can fail in many ways; a cut goes on without words
            report.reasons.append(f"{path.name}: transcription failed ({exc})")
            continue
        if words is None:
            report.reasons.append(f"{path.name}: no cached transcript")
            continue
        words_by_file[path] = words
        tool = tool or stored_tool
        if hit:
            report.cache_hits["transcript"] += 1
    if not words_by_file:
        return
    report.transcript = "whisper"
    report.whisper_tool = tool
    report.words = _timeline_words(speech, words_by_file)
    report.cues = words_to_cues(report.words)


def _timeline_words(spans: list[AudibleSpan], words_by_file: dict[Path, list[Word]]) -> list[TimelineWord]:
    """Spine words, plus connected words that no spine word already covers."""
    placed: list[TimelineWord] = []
    for span in spans:
        if span.path is None:
            continue
        for word in words_by_file.get(span.path, []):
            heard = timeline_intervals(span.pieces, word.start, word.end)
            if not heard:
                continue
            start, end = heard[0][0], heard[-1][1]
            clipped = _heard_length(span, word) < (word.end - word.start)
            placed.append(
                TimelineWord(
                    text=word.text,
                    start=start,
                    end=end,
                    sequence=span.sequence,
                    clip_id=span.clip_id,
                    clip_name=span.clip_name,
                    connected=span.connected,
                    src=span.src,
                    file_start=word.start,
                    file_end=word.end,
                    partial=clipped,
                    confidence=word.confidence,
                )
            )
    spine = [word for word in placed if not word.connected]
    kept = list(spine)
    for word in placed:
        if not word.connected:
            continue
        if any(
            other.sequence == word.sequence and other.start < word.end and word.start < other.end
            for other in spine
        ):
            continue
        kept.append(word)
    kept.sort(key=lambda item: (item.sequence, item.start, item.end, item.connected, item.text))
    return kept


def _heard_length(span: AudibleSpan, word: Word) -> Fraction:
    total = Fraction(0)
    for piece in span.pieces:
        lo = max(min(piece.media_start, piece.media_end), word.start)
        hi = min(max(piece.media_start, piece.media_end), word.end)
        if hi > lo:
            total += hi - lo
    return total


def _clip_row(
    span: AudibleSpan,
    silence_of: dict[Path, list[Interval]],
    failed: set[Path],
    cache: Path,
    probe: Probe,
    report: SignalReport,
) -> dict:
    media = span.media_bounds()
    timeline = span.timeline_bounds()
    row = {
        "sequence": span.sequence,
        "clip_id": span.clip_id,
        "clip_name": span.clip_name,
        "connected": span.connected,
        "kind": span.kind,
        "role": span.role,
        "src": span.src,
        "path": str(span.path) if span.path is not None else None,
        "reachable": span.reachable,
        "media_start_seconds": seconds(media[0]) if media else None,
        "media_end_seconds": seconds(media[1]) if media else None,
        "timeline_start_seconds": seconds(timeline[0]) if timeline else None,
        "timeline_end_seconds": seconds(timeline[1]) if timeline else None,
        "silence": [],
        "integrated_lufs": None,
        "true_peak_db": None,
        "clipping": None,
        "error": span.error,
    }
    if report.audio != "used" or not span.reachable or span.path is None or media is None:
        return row
    if span.path in failed:
        row["error"] = row["error"] or "ffmpeg could not read audio"
        return row
    for media_start, media_end in _slice(silence_of.get(span.path, []), media[0], media[1]):
        for timeline_start, timeline_end in timeline_intervals(span.pieces, media_start, media_end):
            row["silence"].append(
                {
                    "media_start_seconds": seconds(media_start),
                    "media_end_seconds": seconds(media_end),
                    "timeline_start_seconds": seconds(timeline_start),
                    "timeline_end_seconds": seconds(timeline_end),
                }
            )
    try:
        loudness, hit, error = _cached_loudness(cache, span.path, _clamp(media), probe)
    except Exception as exc:  # the probe seam may raise; a row records it
        loudness, hit, error = None, False, str(exc)
    if hit:
        report.cache_hits["loudness"] += 1
    if error and row["error"] is None:
        row["error"] = error
    if loudness is not None:
        row["integrated_lufs"] = loudness.integrated_lufs
        row["true_peak_db"] = loudness.true_peak_db
        row["clipping"] = loudness.clipping
    return row


def _spine_silences(spans, silence_of, failed, frames, rows) -> list[AudioSilence]:
    """Quiet ranges on spine items, minus anywhere a connected speech clip is heard."""
    loud: dict[tuple[str, str], dict] = {}
    for row in rows:
        if not row.get("connected") and row.get("integrated_lufs") is not None:
            loud.setdefault((row["sequence"], row["clip_id"]), row)
    by_clip: dict[tuple[str, str], list[tuple[Interval, AudibleSpan]]] = {}
    for span in spans:
        if span.connected or span.path is None or span.path not in silence_of:
            continue
        bounds = span.media_bounds()
        if bounds is None:
            continue
        for media_start, media_end in _slice(silence_of[span.path], bounds[0], bounds[1]):
            for interval in timeline_intervals(span.pieces, media_start, media_end):
                by_clip.setdefault((span.sequence, span.clip_id), []).append((interval, span))
    found: list[AudioSilence] = []
    for (sequence, clip_id), items in by_clip.items():
        blocked = _blocked(spans, sequence, silence_of, failed)
        frame = frames.get(sequence) or Fraction(1, 24)
        if frame <= 0:
            frame = Fraction(1, 24)
        merged = _merge_intervals([interval for interval, _ in items], Fraction(0))
        for quiet in _subtract(merged, blocked):
            snapped = _snap_inward(quiet[0], quiet[1], frame)
            if snapped is None or snapped[1] - snapped[0] < SILENCE_GAP:
                continue
            owner = next(
                (span for interval, span in items if interval[0] <= snapped[0] < interval[1]),
                items[0][1],
            )
            pieces = list(owner.pieces)
            row = loud.get((sequence, clip_id), {})
            found.append(
                AudioSilence(
                    clip_id=clip_id,
                    timeline_start=snapped[0],
                    timeline_end=snapped[1],
                    media_start=media_at(pieces, snapped[0]) or Fraction(0),
                    media_end=media_at(pieces, snapped[1]) or Fraction(0),
                    integrated_lufs=row.get("integrated_lufs"),
                    true_peak_db=row.get("true_peak_db"),
                    clipping=row.get("clipping"),
                )
            )
    found.sort(key=lambda item: (item.clip_id, item.timeline_start, item.timeline_end))
    return found


def _blocked(spans, sequence, silence_of, failed) -> list[Interval]:
    """Timeline where a connected speech clip has sound, or could not be read."""
    blocked: list[Interval] = []
    for span in spans:
        if not span.connected or span.sequence != sequence or not is_speech_role(span.role):
            continue
        heard = _merge_intervals(
            [(piece.timeline_start, piece.timeline_end) for piece in span.pieces], Fraction(0)
        )
        if span.path is None or span.path in failed or span.path not in silence_of:
            if span.reachable or span.path is not None:
                blocked.extend(heard)
            continue
        bounds = span.media_bounds()
        quiet: list[Interval] = []
        if bounds is not None:
            for media_start, media_end in _slice(silence_of[span.path], bounds[0], bounds[1]):
                quiet.extend(timeline_intervals(span.pieces, media_start, media_end))
        blocked.extend(_subtract(heard, _merge_intervals(sorted(quiet), Fraction(0))))
    return _merge_intervals(sorted(blocked), Fraction(0))


def _needed(group: list[AudibleSpan], pad: Fraction) -> list[Interval]:
    ranges = []
    for span in group:
        bounds = span.media_bounds()
        if bounds is None:
            continue
        lo, hi = _clamp((bounds[0] - pad, bounds[1] + pad))
        if hi > lo:
            ranges.append((lo, hi))
    return _merge_intervals(sorted(ranges), MERGE_GAP)


def _cached_silence(cache: Path, path: Path, needed: list[Interval], probe: Probe):
    record = _load(cache, path) or {}
    block = record.get("silence") or {}
    covered = _intervals(block.get("covered"))
    ranges = _intervals(block.get("ranges"))
    missing = _subtract(needed, covered)
    if not missing:
        return _within(ranges, needed), True, None
    fresh_ranges: list[Interval] = []
    loudness: dict[str, Loudness] = {}
    for lo, hi in missing:
        try:
            result = probe(path, lo, hi)
        except Exception as exc:  # the probe seam may raise; a file records it
            return [], False, str(exc)
        if result.error:
            return [], False, result.error
        fresh_ranges.extend(result.silence)
        if result.loudness is not None:
            loudness[_range_key((lo, hi))] = result.loudness
    covered = _merge_intervals(sorted(covered + missing), Fraction(0))
    ranges = _merge_intervals(sorted(ranges + fresh_ranges), Fraction(0))
    _store(
        cache,
        path,
        silence={"covered": _dump(covered), "ranges": _dump(ranges)},
        loudness=loudness,
    )
    return _within(ranges, needed), False, None


def _cached_loudness(cache: Path, path: Path, bounds: Interval, probe: Probe):
    record = _load(cache, path) or {}
    key = _range_key(bounds)
    stored = (record.get("loudness") or {}).get(key)
    if stored is not None:
        return (
            Loudness(stored.get("integrated_lufs"), stored.get("true_peak_db"), bool(stored.get("clipping"))),
            True,
            None,
        )
    result = probe(path, bounds[0], bounds[1])
    if result.loudness is None:
        return None, False, result.error
    _store(cache, path, loudness={key: result.loudness})
    return result.loudness, False, result.error


def _cached_words(cache: Path, path: Path, needed: list[Interval], runner: Transcriber | None, tool: str | None):
    """Words inside ``needed``. ``runner`` None means cache only, and None when missing."""
    record = _load(cache, path) or {}
    block = record.get("words") or {}
    covered = _intervals(block.get("covered"))
    items = [_word_from(item) for item in block.get("items") or []]
    stored_tool = block.get("tool")
    missing = [interval for interval in needed if _subtract([interval], covered)]
    if not missing:
        return _words_within(items, needed), True, stored_tool
    if runner is None:
        return None, False, stored_tool
    for lo, hi in missing:
        fresh = [word for word in runner(path, lo, hi) if word.end > word.start]
        items = [word for word in items if word.end <= lo or word.start >= hi] + fresh
        covered = _merge_intervals(sorted(covered + [(lo, hi)]), Fraction(0))
    items.sort(key=lambda word: (word.start, word.end, word.text))
    _store(
        cache,
        path,
        words={
            "tool": tool or stored_tool,
            "covered": _dump(covered),
            "items": [
                {
                    "start": _frac(word.start),
                    "end": _frac(word.end),
                    "text": word.text,
                    "confidence": word.confidence,
                }
                for word in items
            ],
        },
    )
    return _words_within(items, needed), False, tool or stored_tool


def _word_from(item: dict) -> Word:
    confidence = item.get("confidence")
    return Word(
        Fraction(item["start"]),
        Fraction(item["end"]),
        item["text"],
        float(confidence) if confidence is not None else None,
    )


def _words_within(words: list[Word], needed: list[Interval]) -> list[Word]:
    return [word for word in words if any(word.end > lo and word.start < hi for lo, hi in needed)]


def _load(cache: Path, path: Path) -> dict | None:
    try:
        identity = _identity(path)
        file = _cache_file(cache, identity)
        if not file.is_file():
            return None
        payload = json.loads(file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(payload, dict) or payload.get("version") != CACHE_VERSION:
        return None
    if payload.get("identity") != identity:
        return None
    return payload


def _store(cache: Path, path: Path, *, silence=None, words=None, loudness=None) -> None:
    """Best effort. A cache that cannot be written only costs a later decode."""
    try:
        identity = _identity(path)
        cache.mkdir(parents=True, exist_ok=True)
    except OSError:
        return
    current = _load(cache, path) or {"version": CACHE_VERSION, "identity": identity, "loudness": {}}
    if silence is not None:
        current["silence"] = silence
    if words is not None:
        current["words"] = words
    for key, item in (loudness or {}).items():
        current.setdefault("loudness", {})[key] = {
            "integrated_lufs": item.integrated_lufs,
            "true_peak_db": item.true_peak_db,
            "clipping": item.clipping,
        }
    file = _cache_file(cache, identity)
    temporary = file.with_name(f"{file.name}.{os.getpid()}.tmp")
    try:
        temporary.write_text(json.dumps(current, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        temporary.replace(file)
    except OSError:
        temporary.unlink(missing_ok=True)


def _cache_file(cache: Path, identity: dict) -> Path:
    digest = hashlib.blake2b(json.dumps(identity, sort_keys=True).encode(), digest_size=16).hexdigest()
    return cache / f"{digest}.json"


def _identity(path: Path) -> dict:
    stat = path.stat()
    return {"path": str(path.resolve()), "size": stat.st_size, "mtime_ns": stat.st_mtime_ns}


def _ffmpeg(binary: str, path: Path, start: Fraction, duration: Fraction, graph: str):
    command = [
        binary,
        "-nostdin",
        "-hide_banner",
        "-nostats",
        "-ss",
        f"{float(start):.6f}",
        "-t",
        f"{float(duration):.6f}",
        "-i",
        str(path),
        "-map",
        "0:a:0",
        "-af",
        graph,
        "-f",
        "null",
        "-",
    ]
    try:
        return subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=600,
            check=False,
            stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None


def _parse_silence(text: str, origin: Fraction, limit: Fraction) -> list[Interval]:
    """ffmpeg times are relative to ``-ss``; add ``origin`` back. An open silence runs to ``limit``."""
    pending: Fraction | None = None
    ranges: list[Interval] = []
    for line in text.splitlines():
        if "silence_start:" in line:
            raw = line.split("silence_start:", 1)[1].strip().split()[0]
            pending = max(origin, origin + _decimal(raw))
            continue
        if "silence_end:" in line and pending is not None:
            raw = line.split("silence_end:", 1)[1].strip().split()[0]
            stop = min(origin + _decimal(raw), limit)
            if stop > pending:
                ranges.append((pending, stop))
            pending = None
    if pending is not None and limit > pending:
        ranges.append((pending, limit))
    return ranges


def _parse_loudness(text: str) -> Loudness | None:
    integrated = None
    in_summary = False
    for line in text.splitlines():
        if "Summary:" in line:
            in_summary = True
        if not in_summary:
            continue
        stripped = line.strip()
        if stripped.startswith("I:") and "LUFS" in stripped:
            integrated = _float_token(stripped)
    peak = None
    marker = "True peak:"
    if marker in text:
        tail = text.split(marker, 1)[1]
        for line in tail.splitlines():
            stripped = line.strip()
            if stripped.startswith("Peak:"):
                peak = _float_token(stripped)
                break
    if integrated is None and peak is None:
        return None
    integrated = integrated if integrated is None or math.isfinite(integrated) else None
    peak = peak if peak is None or math.isfinite(peak) else None
    clipping = peak is not None and peak >= CLIP_PEAK_DB
    return Loudness(integrated, peak, clipping)


def _float_token(line: str) -> float | None:
    for token in line.replace(":", " ").split():
        if token in {"-inf", "inf"}:
            return float("-inf") if token == "-inf" else float("inf")
        try:
            return float(token)
        except ValueError:
            continue
    return None


def _decimal(text: str) -> Fraction:
    try:
        return Fraction(Decimal(text))
    except (ArithmeticError, ValueError):
        return Fraction(0)


def _extract_wav(path: Path, start: Fraction, end: Fraction) -> Path:
    binary = shutil.which("ffmpeg")
    if binary is None:
        raise ConductorError("ffmpeg is not on PATH")
    handle = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
    handle.close()
    destination = Path(handle.name)
    command = [
        binary,
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "error",
        "-ss",
        f"{float(start):.6f}",
        "-t",
        f"{float(end - start):.6f}",
        "-i",
        str(path),
        "-map",
        "0:a:0",
        "-ac",
        "1",
        "-ar",
        "16000",
        "-c:a",
        "pcm_s16le",
        "-y",
        str(destination),
    ]
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=600,
            check=False,
            stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        destination.unlink(missing_ok=True)
        raise ConductorError("ffmpeg could not extract audio for transcription") from exc
    if completed.returncode != 0 or not destination.is_file():
        destination.unlink(missing_ok=True)
        raise ConductorError("ffmpeg could not extract audio for transcription")
    return destination


def _transcribe_faster(model, path: Path, start: Fraction, end: Fraction) -> list[Word]:
    """Local faster-whisper. The model was built with ``local_files_only``."""
    wav = _extract_wav(path, start, end)
    try:
        segments, _info = model.transcribe(str(wav), word_timestamps=True, vad_filter=False)
        words: list[Word] = []
        for segment in segments:
            for word in getattr(segment, "words", None) or []:
                text = str(getattr(word, "word", "") or "").strip()
                if not text:
                    continue
                probability = getattr(word, "probability", None)
                words.append(
                    Word(
                        start + _seconds(getattr(word, "start", 0.0)),
                        start + _seconds(getattr(word, "end", 0.0)),
                        text,
                        round(float(probability), 4) if probability is not None else None,
                    )
                )
        return [word for word in words if word.end > word.start]
    finally:
        wav.unlink(missing_ok=True)


def _transcribe_cpp(binary: str, model: Path, path: Path, start: Fraction, end: Fraction) -> list[Word]:
    """whisper.cpp with one word per row (``-ml 1 -sow``), read from its JSON."""
    wav = _extract_wav(path, start, end)
    try:
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "out"
            completed = subprocess.run(
                [binary, "-m", str(model), "-f", str(wav), "-ml", "1", "-sow", "-oj", "-of", str(out), "-np"],
                capture_output=True,
                text=True,
                timeout=1800,
                check=False,
                stdin=subprocess.DEVNULL,
            )
            produced = out.with_suffix(".json")
            if not produced.is_file():
                alt = list(Path(tmp).glob("*.json"))
                produced = alt[0] if alt else produced
            if completed.returncode != 0 or not produced.is_file():
                raise ConductorError("whisper.cpp produced no transcript")
            payload = json.loads(produced.read_text(encoding="utf-8"))
        return _words_from_cpp(payload, start)
    finally:
        wav.unlink(missing_ok=True)


def _words_from_cpp(payload: object, origin: Fraction) -> list[Word]:
    if not isinstance(payload, dict):
        return []
    rows = payload.get("transcription")
    if not isinstance(rows, list):
        return []
    words: list[Word] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        nested = row.get("words")
        items = nested if isinstance(nested, list) and nested else [row]
        for item in items:
            if not isinstance(item, dict):
                continue
            text = str(item.get("text") or "").strip()
            span = _cpp_span(item, origin)
            if text and span is not None and not (text.startswith("[") and text.endswith("]")):
                words.append(Word(span[0], span[1], text))
    return words


def _cpp_span(row: dict, origin: Fraction) -> Interval | None:
    offsets = row.get("offsets") if isinstance(row.get("offsets"), dict) else None
    if offsets and "from" in offsets and "to" in offsets:
        try:
            start = origin + Fraction(int(offsets["from"]), 1000)
            end = origin + Fraction(int(offsets["to"]), 1000)
        except (TypeError, ValueError):
            return None
        return (start, end) if end > start else None
    stamps = row.get("timestamps") if isinstance(row.get("timestamps"), dict) else None
    if not stamps:
        return None
    try:
        start = origin + _clock_ms(str(stamps.get("from") or ""))
        end = origin + _clock_ms(str(stamps.get("to") or ""))
    except (TypeError, ValueError, ArithmeticError):
        return None
    return (start, end) if end > start else None


def _clock_ms(value: str) -> Fraction:
    """``HH:MM:SS,mmm`` or ``HH:MM:SS.mmm`` as seconds."""
    raw = value.strip().replace(",", ".")
    parts = raw.split(":")
    if len(parts) != 3:
        raise ValueError(value)
    hour, minute, sec = parts
    return Fraction(int(hour)) * 3600 + Fraction(int(minute)) * 60 + Fraction(Decimal(sec))


def _cpp_backend() -> tuple[str, Path] | None:
    binary = shutil.which("whisper-cli") or shutil.which("whisper-cpp")
    if not binary:
        return None
    chosen = os.environ.get("CONDUCTOR_WHISPER_MODEL", "").strip()
    if chosen and Path(chosen).expanduser().is_file():
        return binary, Path(chosen).expanduser()
    return None


def _faster_model_name() -> str | None:
    chosen = os.environ.get("CONDUCTOR_WHISPER_MODEL", "").strip()
    if chosen and Path(chosen).expanduser().is_dir():
        return str(Path(chosen).expanduser())
    if chosen and Path(chosen).expanduser().is_file():
        return None
    hub = _hf_hub()
    if chosen:
        folder = hub / f"models--Systran--faster-whisper-{chosen}"
        return chosen if folder.is_dir() else None
    if not hub.is_dir():
        return None
    matches = sorted(path.name for path in hub.glob("models--Systran--faster-whisper-*") if path.is_dir())
    if not matches:
        return None
    return matches[0].split("faster-whisper-")[-1]


def _hf_hub() -> Path:
    cache = os.environ.get("HF_HUB_CACHE")
    if cache:
        return Path(cache)
    home = os.environ.get("HF_HOME")
    if home:
        return Path(home) / "hub"
    return Path.home() / ".cache" / "huggingface" / "hub"


def _faster_importable() -> bool:
    try:
        import faster_whisper  # noqa: F401
    except Exception:  # a broken install (missing ctranslate2) is the same as absent
        return False
    return True


def _whisper_skip_reason() -> str:
    chosen = os.environ.get("CONDUCTOR_WHISPER_MODEL", "").strip()
    if not _faster_importable() and shutil.which("whisper-cli") is None and shutil.which("whisper-cpp") is None:
        return "no local whisper tool is installed (faster-whisper or whisper.cpp)"
    if not chosen:
        return "no local whisper model is on disk; set CONDUCTOR_WHISPER_MODEL to a model already downloaded"
    return "the whisper model in CONDUCTOR_WHISPER_MODEL is not usable offline"


def _seconds(value: object) -> Fraction:
    return Fraction(Decimal(str(round(float(value), 3))))


def _groups(spans: list[AudibleSpan]) -> dict[Path, list[AudibleSpan]]:
    grouped: dict[Path, list[AudibleSpan]] = {}
    for span in spans:
        if span.path is not None:
            grouped.setdefault(span.path, []).append(span)
    return grouped


def _clamp(bounds: Interval) -> Interval:
    return max(bounds[0], Fraction(0)), max(bounds[1], Fraction(0))


def _slice(ranges, start: Fraction, end: Fraction) -> list[Interval]:
    found = []
    for left, right in ranges:
        lo = max(left, start)
        hi = min(right, end)
        if hi > lo:
            found.append((lo, hi))
    return found


def _within(ranges: list[Interval], needed: list[Interval]) -> list[Interval]:
    found: list[Interval] = []
    for lo, hi in needed:
        found.extend(_slice(ranges, lo, hi))
    return _merge_intervals(sorted(found), Fraction(0))


def _merge_intervals(ranges: list[Interval], gap: Fraction) -> list[Interval]:
    merged: list[Interval] = []
    for start, end in ranges:
        if end <= start:
            continue
        if merged and start <= merged[-1][1] + gap:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def _subtract(ranges: list[Interval], holes: list[Interval]) -> list[Interval]:
    """``ranges`` minus ``holes``. Both sorted and non-overlapping."""
    result: list[Interval] = []
    for start, end in ranges:
        cursor = start
        for lo, hi in holes:
            if hi <= cursor or lo >= end:
                continue
            if lo > cursor:
                result.append((cursor, lo))
            cursor = max(cursor, hi)
            if cursor >= end:
                break
        if cursor < end:
            result.append((cursor, end))
    return result


def _snap_inward(start: Fraction, end: Fraction, frame: Fraction) -> Interval | None:
    """Whole frames inside the quiet range, so a trim never takes a frame of sound."""
    snapped_start = math.ceil(start / frame) * frame
    snapped_end = math.floor(end / frame) * frame
    if snapped_end <= snapped_start:
        return None
    return Fraction(snapped_start), Fraction(snapped_end)


def _intervals(raw) -> list[Interval]:
    if not isinstance(raw, list):
        return []
    found: list[Interval] = []
    for item in raw:
        try:
            found.append((Fraction(item[0]), Fraction(item[1])))
        except (TypeError, ValueError, IndexError, ZeroDivisionError):
            continue
    return _merge_intervals(sorted(found), Fraction(0))


def _dump(ranges: list[Interval]) -> list[list[str]]:
    return [[_frac(lo), _frac(hi)] for lo, hi in ranges]


def _unreachable(spans: list[AudibleSpan]) -> list[str]:
    found = []
    for span in spans:
        if span.reachable or not span.src:
            continue
        if span.src not in found:
            found.append(span.src)
    return found


def _range_key(bounds: Interval) -> str:
    return f"{_frac(bounds[0])}:{_frac(bounds[1])}"


def _frac(value: Fraction) -> str:
    value = Fraction(value)
    return f"{value.numerator}/{value.denominator}"


def _mode(value: str, flag: str, allowed: frozenset[str]) -> str:
    if value not in allowed:
        raise ConductorError(f"{flag} must be auto, on, or off")
    return value


def _summary(report: SignalReport) -> str:
    if report.audio == "used":
        files = sorted({row["path"] for row in report.clips if row.get("reachable") and row.get("path")})
        audio = f"audio silence and loudness from {len(files)} file" + ("" if len(files) == 1 else "s")
        audio += " (ffmpeg)"
    else:
        audio = "audio skipped"
        why = _first(
            report.reasons,
            ("audio signals are off", "ffmpeg is not", "could not read audio", "no referenced media"),
        )
        if why:
            audio += f": {why}"
    if report.transcript == "file":
        transcript = "transcript from the supplied SRT or WebVTT"
    elif report.transcript == "whisper":
        tool = report.whisper_tool or "local whisper"
        hits = report.cache_hits.get("transcript", 0)
        transcript = f"{len(report.words)} words from {tool}" + (" (cached)" if hits else "")
    else:
        transcript = "transcript skipped"
        why = _first(
            report.reasons,
            (
                "local transcription is off",
                "no local whisper",
                "no reachable media to transcribe",
                "CONDUCTOR_WHISPER_MODEL",
                "transcription failed",
                "no cached transcript",
                "ffmpeg is not on PATH, so",
            ),
        )
        if why:
            transcript += f": {why}"
    tail = ""
    if report.unreachable:
        count = len(report.unreachable)
        tail = f" {count} referenced file" + (" was" if count == 1 else "s were") + " not on disk."
    return f"{audio}. {transcript}.{tail}"


def _first(reasons: list[str], prefixes: tuple[str, ...]) -> str | None:
    for prefix in prefixes:
        for reason in reasons:
            if prefix in reason:
                return reason
    return None
