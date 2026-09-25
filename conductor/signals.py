"""Optional silence, loudness, and a local transcript from reachable media.

Nothing here contacts the network. ffmpeg measures silence and loudness when
it is on ``PATH`` and a ``media-rep`` ``file://`` path exists on this machine.
A transcript is read from faster-whisper or whisper.cpp only when that tool
and a model are already on disk. A missing tool, a missing model, or a media
path that does not resolve is a skip recorded on the report, not a failed run.

Results are cached per media file (size and mtime, plus the range that was
decoded). A later ``iterate`` round slices that cache instead of transcribing
again.
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
from .fcpxml import Document, Sequence
from .timeutil import seconds
from .timing import AudibleSpan, audible_spans, timeline_intervals
from .transcript import Cue

NOISE_DB = -40
MIN_SILENCE = Fraction(3, 10)
CLIP_PEAK_DB = -0.1
UTTERANCE_GAP = Fraction("0.35")
CACHE_VERSION = 1
_MODES = frozenset({"auto", "on", "off"})

Probe = Callable[[Path, Fraction, Fraction], "ProbeResult"]
Transcriber = Callable[[Path, Fraction, Fraction], list["Word"]]


@dataclass(frozen=True)
class Word:
    start: Fraction
    end: Fraction
    text: str


@dataclass(frozen=True)
class Loudness:
    integrated_lufs: float | None
    true_peak_db: float | None
    clipping: bool


@dataclass(frozen=True)
class ProbeResult:
    silence: list[tuple[Fraction, Fraction]]
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
    sequences: Sequence[Sequence],
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

    ``probe`` and ``transcriber`` are test seams. Production leaves them unset.
    ``transcriber`` returning normally means the local tool ran. Raise to skip.
    """
    mode = _mode(signals, "--signals")
    transcribe_mode = _mode(transcribe, "--transcribe")
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
    if mode == "off":
        report.reasons.append("audio signals are off")
    elif not ffmpeg:
        report.reasons.append("ffmpeg is not on PATH")
    elif not reachable:
        report.reasons.append("no referenced media file is on this machine")
    else:
        report.audio = "used"
        _measure(report, spans, reachable, cache, frames, measure)
    if report.audio != "used":
        for span in spans:
            report.clips.append(_clip_row(span, None, cache, measure, report))

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


def words_to_cues(words: Sequence[Word]) -> list[Cue]:
    """Group word timings into the cues the dialogue and pacing passes already read.

    A filler token is its own cue. Any other gap of at least 0.35s starts a
    new cue, so a pause of 0.80s or more is visible to the existing heuristic.
    """
    ordered = [word for word in words if word.text.strip() and word.end > word.start]
    ordered.sort(key=lambda word: (word.start, word.end, word.text))
    unique: list[Word] = []
    for word in ordered:
        if unique and unique[-1] == word:
            continue
        unique.append(word)
    if not unique:
        return []
    groups: list[list[Word]] = [[unique[0]]]
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
    """One ffmpeg pass: silencedetect plus ebur128. No network."""
    binary = shutil.which("ffmpeg")
    if binary is None:
        return ProbeResult([], None, "ffmpeg is not on PATH")
    duration = end - start
    if duration <= 0:
        return ProbeResult([], None, "empty media range")
    graph = (
        f"silencedetect=noise={NOISE_DB}dB:d={float(MIN_SILENCE):.2f},"
        "ebur128=peak=true:framelog=quiet"
    )
    completed = _ffmpeg(binary, path, start, duration, graph)
    if completed is None:
        return ProbeResult([], None, "ffmpeg did not finish")
    text = completed.stderr or ""
    if completed.returncode != 0 and "framelog" in text:
        graph = f"silencedetect=noise={NOISE_DB}dB:d={float(MIN_SILENCE):.2f},ebur128=peak=true"
        completed = _ffmpeg(binary, path, start, duration, graph)
        text = (completed.stderr or "") if completed is not None else ""
    if completed is None or (completed.returncode != 0 and "silence_" not in text):
        silence_only = f"silencedetect=noise={NOISE_DB}dB:d={float(MIN_SILENCE):.2f}"
        completed = _ffmpeg(binary, path, start, duration, silence_only)
        text = (completed.stderr or "") if completed is not None else ""
    if completed is None or (completed.returncode != 0 and "silence_" not in text):
        return ProbeResult([], None, "ffmpeg could not read audio")
    silence = _parse_silence(text, start, end)
    return ProbeResult(silence, _parse_loudness(text), None)


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
        return "faster-whisper", lambda path, start, end: _transcribe_faster(name, path, start, end)
    return None


def _measure(report, spans, reachable, cache: Path, frames, probe: Probe) -> None:
    by_file: dict[Path, list[AudibleSpan]] = {}
    for span in reachable:
        assert span.path is not None
        by_file.setdefault(span.path, []).append(span)
    silence_of: dict[Path, list[tuple[Fraction, Fraction]]] = {}
    failed: set[Path] = set()
    for path, group in by_file.items():
        bounds = _bounds([span.media_bounds() for span in group])
        if bounds is None:
            continue
        ranges, hit, error = _cached_silence(cache, path, bounds, probe)
        if error and report.audio == "used":
            report.reasons.append(f"{path.name}: {error}")
        silence_of[path] = ranges
        if hit:
            report.cache_hits["silence"] += 1
        if error:
            failed.add(path)
    for span in spans:
        report.clips.append(
            _clip_row(
                span,
                silence_of.get(span.path) if span.path else None,
                cache,
                probe,
                report,
                skip_loudness=span.path in failed,
            )
        )
    readable = [row for row in report.clips if row.get("reachable")]
    if readable and all(row.get("error") for row in readable):
        report.audio = "skipped"
        report.silences = []
        if not any("could not read audio" in reason for reason in report.reasons):
            report.reasons.append("ffmpeg could not read audio from the reachable files")
        return
    report.silences = _spine_silences(spans, silence_of, frames, report)


def _transcribe(
    report,
    spans,
    reachable,
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
    if not reachable:
        report.reasons.append("no reachable media to transcribe")
        return
    if not ffmpeg:
        report.reasons.append("ffmpeg is not on PATH, so a transcript range cannot be read")
        return
    tool = whisper_tool
    runner = transcriber
    if runner is None:
        resolved = resolve_whisper()
        if resolved is None:
            report.reasons.append(_whisper_skip_reason())
            return
        tool, runner = resolved
    report.whisper_tool = tool
    words_by_file: dict[Path, list[Word]] = {}
    attempts = 0
    failures = 0
    for path, group in _groups(reachable).items():
        bounds = _bounds([span.media_bounds() for span in group])
        if bounds is None:
            continue
        attempts += 1
        try:
            words, hit = _cached_words(cache, path, bounds, runner)
        except (OSError, subprocess.SubprocessError, ConductorError, RuntimeError, ValueError) as exc:
            failures += 1
            report.reasons.append(f"{path.name}: transcription failed ({exc})")
            continue
        words_by_file[path] = words
        if hit:
            report.cache_hits["transcript"] += 1
    if attempts and failures < attempts:
        report.transcript = "whisper"
    tokens: list[Word] = []
    for span in spans:
        if span.path is None:
            continue
        for word in words_by_file.get(span.path, []):
            for start, end in timeline_intervals(span.pieces, word.start, word.end):
                if end > start:
                    tokens.append(Word(start, end, word.text))
    report.cues = words_to_cues(tokens)


def _clip_row(
    span: AudibleSpan,
    file_silence,
    cache: Path,
    probe: Probe,
    report: SignalReport,
    *,
    skip_loudness: bool = False,
) -> dict:
    media = span.media_bounds()
    timeline = span.timeline_bounds()
    row = {
        "sequence": span.sequence,
        "clip_id": span.clip_id,
        "clip_name": span.clip_name,
        "connected": span.connected,
        "kind": span.kind,
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
    if not span.reachable or span.path is None or media is None or file_silence is None:
        return row
    if report.audio != "used":
        return row
    heard = _slice(file_silence, media[0], media[1])
    mapped = []
    for media_start, media_end in heard:
        for timeline_start, timeline_end in timeline_intervals(span.pieces, media_start, media_end):
            mapped.append(
                {
                    "media_start_seconds": seconds(media_start),
                    "media_end_seconds": seconds(media_end),
                    "timeline_start_seconds": seconds(timeline_start),
                    "timeline_end_seconds": seconds(timeline_end),
                }
            )
    row["silence"] = mapped
    if skip_loudness:
        if row["error"] is None:
            row["error"] = "ffmpeg could not read audio"
        return row
    loudness, hit, error = _cached_loudness(cache, span.path, media, probe)
    if hit:
        report.cache_hits["loudness"] += 1
    if error and row["error"] is None:
        row["error"] = error
    if loudness is not None:
        row["integrated_lufs"] = loudness.integrated_lufs
        row["true_peak_db"] = loudness.true_peak_db
        row["clipping"] = loudness.clipping
    return row


def _spine_silences(spans, silence_of, frames, report: SignalReport) -> list[AudioSilence]:
    if report.audio != "used":
        return []
    grouped: dict[str, list[tuple[Fraction, Fraction, Fraction, Fraction, dict]]] = {}
    loudness_of: dict[str, dict] = {}
    for row in report.clips:
        if row.get("integrated_lufs") is not None or row.get("clipping") is not None:
            loudness_of[row["clip_id"]] = row
    for span in spans:
        if span.connected or span.path is None:
            continue
        ranges = silence_of.get(span.path) or []
        bounds = span.media_bounds()
        timeline = span.timeline_bounds()
        if bounds is None or timeline is None:
            continue
        frame = frames.get(span.sequence) or Fraction(1, 24)
        if frame <= 0:
            frame = Fraction(1, 24)
        for media_start, media_end in _slice(ranges, bounds[0], bounds[1]):
            for start, end in timeline_intervals(span.pieces, media_start, media_end):
                snapped = _snap(start, end, frame, timeline[0], timeline[1])
                if snapped is None:
                    continue
                grouped.setdefault(span.clip_id, []).append(
                    (snapped[0], snapped[1], media_start, media_end, loudness_of.get(span.clip_id, {}))
                )
    found: list[AudioSilence] = []
    for clip_id, items in grouped.items():
        for start, end, media_start, media_end, loud in _merge_items(items):
            if end - start < SILENCE_GAP:
                continue
            found.append(
                AudioSilence(
                    clip_id=clip_id,
                    timeline_start=start,
                    timeline_end=end,
                    media_start=media_start,
                    media_end=media_end,
                    integrated_lufs=loud.get("integrated_lufs"),
                    true_peak_db=loud.get("true_peak_db"),
                    clipping=loud.get("clipping"),
                )
            )
    found.sort(key=lambda item: (item.clip_id, item.timeline_start, item.timeline_end))
    return found


def _cached_silence(cache, path, bounds, probe: Probe):
    record = _load(cache, path)
    block = record.get("silence") if record else None
    if _covers(block, bounds):
        ranges = _slice(
            [(Fraction(item["start"]), Fraction(item["end"])) for item in block["ranges"]],
            bounds[0],
            bounds[1],
        )
        return ranges, True, None
    result = probe(path, bounds[0], bounds[1])
    if result.error:
        return [], False, result.error
    block = {
        "start": _frac(bounds[0]),
        "end": _frac(bounds[1]),
        "ranges": [{"start": _frac(a), "end": _frac(b)} for a, b in result.silence],
    }
    _store(cache, path, silence=block, loudness_key=_range_key(bounds), loudness=result.loudness)
    return result.silence, False, None


def _cached_loudness(cache, path, bounds, probe: Probe):
    record = _load(cache, path)
    key = _range_key(bounds)
    stored = (record or {}).get("loudness") or {}
    if key in stored and stored[key] is not None:
        item = stored[key]
        return (
            Loudness(item.get("integrated_lufs"), item.get("true_peak_db"), bool(item.get("clipping"))),
            True,
            None,
        )
    result = probe(path, bounds[0], bounds[1])
    if result.loudness is None:
        return None, False, result.error
    _store(cache, path, loudness_key=key, loudness=result.loudness)
    return result.loudness, False, result.error


def _cached_words(cache, path, bounds, transcriber: Transcriber):
    record = _load(cache, path)
    block = record.get("words") if record else None
    if _covers(block, bounds):
        words = [
            Word(Fraction(item["start"]), Fraction(item["end"]), item["text"])
            for item in block["items"]
            if Fraction(item["end"]) > bounds[0] and Fraction(item["start"]) < bounds[1]
        ]
        return words, True
    words = list(transcriber(path, bounds[0], bounds[1]))
    _store(
        cache,
        path,
        words={
            "start": _frac(bounds[0]),
            "end": _frac(bounds[1]),
            "items": [
                {"start": _frac(word.start), "end": _frac(word.end), "text": word.text} for word in words
            ],
        },
    )
    return words, False


def _load(cache: Path, path: Path) -> dict | None:
    file = _cache_file(cache, path)
    if not file.is_file():
        return None
    try:
        payload = json.loads(file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict) or payload.get("version") != CACHE_VERSION:
        return None
    identity = _identity(path)
    if payload.get("identity") != identity:
        return None
    return payload


def _store(cache: Path, path: Path, **updates) -> None:
    try:
        cache.mkdir(parents=True, exist_ok=True)
    except OSError:
        return
    current = _load(cache, path) or {"version": CACHE_VERSION, "identity": _identity(path), "loudness": {}}
    if "silence" in updates and updates["silence"] is not None:
        current["silence"] = updates["silence"]
    if "words" in updates and updates["words"] is not None:
        current["words"] = updates["words"]
    if updates.get("loudness_key") and updates.get("loudness") is not None:
        loud = current.setdefault("loudness", {})
        item = updates["loudness"]
        loud[updates["loudness_key"]] = {
            "integrated_lufs": item.integrated_lufs,
            "true_peak_db": item.true_peak_db,
            "clipping": item.clipping,
        }
    file = _cache_file(cache, path)
    temporary = file.with_suffix(".tmp")
    try:
        temporary.write_text(json.dumps(current, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        temporary.replace(file)
    except OSError:
        return


def _cache_file(cache: Path, path: Path) -> Path:
    digest = hashlib.blake2b(
        json.dumps(_identity(path), sort_keys=True).encode(),
        digest_size=16,
    ).hexdigest()
    return cache / f"{digest}.json"


def _identity(path: Path) -> dict:
    stat = path.stat()
    return {"path": str(path.resolve()), "size": stat.st_size, "mtime_ns": stat.st_mtime_ns}


def _covers(block, bounds) -> bool:
    if not isinstance(block, dict) or "start" not in block or "end" not in block:
        return False
    return Fraction(block["start"]) <= bounds[0] and Fraction(block["end"]) >= bounds[1]


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


def _parse_silence(text: str, origin: Fraction, limit: Fraction) -> list[tuple[Fraction, Fraction]]:
    pending: Fraction | None = None
    ranges: list[tuple[Fraction, Fraction]] = []
    for line in text.splitlines():
        if "silence_start:" in line:
            raw = line.split("silence_start:", 1)[1].strip().split()[0]
            pending = origin + _decimal(raw)
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


def _transcribe_faster(model_name: str, path: Path, start: Fraction, end: Fraction) -> list[Word]:
    """Local faster-whisper. ``local_files_only`` refuses a model download."""
    from faster_whisper import WhisperModel

    wav = _extract_wav(path, start, end)
    try:
        model = WhisperModel(model_name, device="cpu", compute_type="int8", local_files_only=True)
        segments, _info = model.transcribe(str(wav), word_timestamps=True, vad_filter=False)
        words: list[Word] = []
        for segment in segments:
            segment_words = getattr(segment, "words", None) or []
            if segment_words:
                for word in segment_words:
                    text = str(getattr(word, "word", "") or "").strip()
                    if not text:
                        continue
                    words.append(
                        Word(
                            start + _seconds(getattr(word, "start", 0.0)),
                            start + _seconds(getattr(word, "end", 0.0)),
                            text,
                        )
                    )
                continue
            text = str(getattr(segment, "text", "") or "").strip()
            if text:
                words.append(
                    Word(
                        start + _seconds(getattr(segment, "start", 0.0)),
                        start + _seconds(getattr(segment, "end", 0.0)),
                        text,
                    )
                )
        return [word for word in words if word.end > word.start]
    finally:
        wav.unlink(missing_ok=True)


def _transcribe_cpp(binary: str, model: Path, path: Path, start: Fraction, end: Fraction) -> list[Word]:
    wav = _extract_wav(path, start, end)
    try:
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "out"
            completed = subprocess.run(
                [binary, "-m", str(model), "-f", str(wav), "-oj", "-of", str(out), "-np"],
                capture_output=True,
                text=True,
                timeout=600,
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
        if isinstance(nested, list) and nested:
            for word in nested:
                if not isinstance(word, dict):
                    continue
                text = str(word.get("text") or "").strip()
                span = _cpp_span(word, origin)
                if text and span is not None:
                    words.append(Word(span[0], span[1], text))
            continue
        text = str(row.get("text") or "").strip()
        span = _cpp_span(row, origin)
        if text and span is not None:
            words.append(Word(span[0], span[1], text))
    return words


def _cpp_span(row: dict, origin: Fraction) -> tuple[Fraction, Fraction] | None:
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
    except (TypeError, ValueError):
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
    if chosen and Path(chosen).is_file():
        return binary, Path(chosen)
    return None


def _faster_model_name() -> str | None:
    chosen = os.environ.get("CONDUCTOR_WHISPER_MODEL", "").strip()
    if chosen and Path(chosen).is_dir():
        return chosen
    if chosen and Path(chosen).is_file():
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
    home = os.environ.get("HF_HOME")
    if home:
        return Path(home) / "hub"
    return Path.home() / ".cache" / "huggingface" / "hub"


def _faster_importable() -> bool:
    try:
        import faster_whisper  # noqa: F401
    except ImportError:
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


def _bounds(items: list[tuple[Fraction, Fraction] | None]) -> tuple[Fraction, Fraction] | None:
    pairs = [item for item in items if item is not None]
    if not pairs:
        return None
    return min(item[0] for item in pairs), max(item[1] for item in pairs)


def _slice(ranges, start: Fraction, end: Fraction):
    found = []
    for left, right in ranges:
        lo = max(left, start)
        hi = min(right, end)
        if hi > lo:
            found.append((lo, hi))
    return found


def _snap(start, end, frame, limit_start, limit_end):
    snapped_start = math.floor(start / frame) * frame
    snapped_end = math.ceil(end / frame) * frame
    if snapped_start < limit_start:
        snapped_start = limit_start
    if snapped_end > limit_end:
        snapped_end = limit_end
    if snapped_end <= snapped_start:
        return None
    return snapped_start, snapped_end


def _merge_items(items):
    ordered = sorted(items, key=lambda item: (item[0], item[1]))
    merged = []
    for start, end, media_start, media_end, loud in ordered:
        if merged and start <= merged[-1][1]:
            prev = merged[-1]
            merged[-1] = (prev[0], max(prev[1], end), prev[2], media_end, prev[4])
        else:
            merged.append((start, end, media_start, media_end, loud))
    return merged


def _unreachable(spans: list[AudibleSpan]) -> list[str]:
    found = []
    for span in spans:
        if span.reachable or not span.src:
            continue
        if span.src not in found:
            found.append(span.src)
    return found


def _range_key(bounds: tuple[Fraction, Fraction]) -> str:
    return f"{_frac(bounds[0])}:{_frac(bounds[1])}"


def _frac(value: Fraction) -> str:
    value = Fraction(value)
    return f"{value.numerator}/{value.denominator}"


def _mode(value: str, flag: str) -> str:
    if value not in _MODES:
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
        transcript = f"transcript from {tool}" + (" (cached)" if hits else "")
    else:
        transcript = "transcript skipped"
        why = _first(
            report.reasons,
            ("local transcription is off", "no local whisper", "no reachable media to transcribe", "CONDUCTOR_WHISPER_MODEL"),
        )
        if why:
            transcript += f": {why}"
    tail = ""
    if report.unreachable:
        count = len(report.unreachable)
        tail = f" {count} referenced file" + (" was" if count == 1 else "s were") + " not on disk."
    return f"{audio}. {transcript}.{tail}"


def _first(reasons: list[str], prefixes: tuple[str, ...]) -> str | None:
    for reason in reasons:
        for prefix in prefixes:
            if prefix in reason:
                return reason
    return None
