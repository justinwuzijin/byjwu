"""Raw material for an assembly: footage, music, and per-clip signals.

Footage comes from a folder (the same inventory ``ingest`` uses) or from the
assets of an FCPXML Justin exported with everything in it. Music is any audio
file in that folder, in a ``music/`` subfolder, or passed with ``--music``.

Signals are the extension point for the media-signals work. A provider is
``(MediaClip) -> ClipSignals | None``. The default reads a sidecar transcript
beside the clip (``clip.srt``, ``clip.vtt`` or whisper ``clip.json``, all in
the clip's own time) and, when there is none and ffmpeg is installed, runs
``silencedetect`` for speech ranges. Register a better provider with
:func:`register_signal_provider`; providers run in registration order and the
first non-None answer wins.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import wave
from collections.abc import Callable
from dataclasses import dataclass, field
from fractions import Fraction
from pathlib import Path
from urllib.parse import unquote, urlparse

from cutmcp.extract import is_trivial_filler

from ..errors import ConductorError
from ..fcpxml import parse_fcpxml
from ..ingest import (
    DEFAULT_FRAME,
    DEFAULT_HEIGHT,
    DEFAULT_WIDTH,
    VIDEO_EXTENSIONS,
    MediaClip,
    ProbeFn,
    file_hash,
    file_url,
    inventory,
    quantize,
)
from ..transcript import Cue, parse_cues
from .beats import BeatGrid, detect_beats

AUDIO_EXTENSIONS = frozenset({".wav", ".mp3", ".m4a", ".aif", ".aiff", ".flac", ".caf", ".aac"})
BROLL_HINT = re.compile(r"(?:^|[^a-z])(b[-_ ]?roll|broll|insert|cutaway|drone|bts|detail)", re.IGNORECASE)
AROLL_HINT = re.compile(r"(?:^|[^a-z])(a[-_ ]?roll|aroll|interview|talk|talking|vlog|piece|pov)", re.IGNORECASE)


@dataclass(frozen=True)
class Word:
    start: Fraction
    end: Fraction
    text: str


@dataclass
class ClipSignals:
    """What is known about one clip, in the clip's own time."""

    cues: list[Cue] = field(default_factory=list)
    words: list[Word] = field(default_factory=list)
    silences: list[tuple[Fraction, Fraction]] = field(default_factory=list)
    loudness_lufs: float | None = None
    source: str = "none"

    @property
    def has_transcript(self) -> bool:
        return bool(self.cues)

    def speech_ranges(self, duration: Fraction, min_seconds: Fraction) -> list[tuple[Fraction, Fraction]]:
        """Non-silent ranges when only silence detection ran."""
        if not self.silences:
            return []
        ranges: list[tuple[Fraction, Fraction]] = []
        cursor = Fraction(0)
        for start, end in sorted(self.silences):
            if start - cursor >= min_seconds:
                ranges.append((cursor, start))
            cursor = max(cursor, end)
        if duration - cursor >= min_seconds:
            ranges.append((cursor, duration))
        return ranges


SignalProvider = Callable[[MediaClip], "ClipSignals | None"]
_PROVIDERS: list[tuple[str, SignalProvider]] = []


def register_signal_provider(name: str, provider: SignalProvider) -> None:
    """Install a provider ahead of the default. Replaces one with the same name."""
    global _PROVIDERS
    _PROVIDERS = [(key, fn) for key, fn in _PROVIDERS if key != name]
    _PROVIDERS.append((name, provider))


def clear_signal_providers() -> None:
    _PROVIDERS.clear()


@dataclass
class Footage:
    clip: MediaClip
    signals: ClipSignals
    role: str
    role_reason: str

    @property
    def name(self) -> str:
        return self.clip.stem


@dataclass
class Song:
    path: Path
    name: str
    src: str
    uid: str
    duration: Fraction
    channels: int
    rate: int
    blake2b: str
    duration_source: str
    beats: BeatGrid | None = None

    def to_state(self) -> dict:
        return {
            "name": self.name,
            "path": str(self.path),
            "duration_seconds": round(float(self.duration), 6),
            "duration_source": self.duration_source,
            "channels": self.channels,
            "rate": self.rate,
            "blake2b": self.blake2b,
            "beats": self.beats.to_state() if self.beats else None,
        }


@dataclass
class Material:
    root: Path | None
    footage: list[Footage]
    songs: list[Song]
    frame: Fraction
    width: int
    height: int
    warnings: list[str] = field(default_factory=list)
    hashes: dict[Path, str] = field(default_factory=dict)


def gather(
    *,
    media: str | Path | None = None,
    fcpxml: str | Path | None = None,
    music: str | Path | list | tuple | None = None,
    transcript_path: str | Path | None = None,
    overrides: dict[str, Fraction] | None = None,
    probe: ProbeFn | None = None,
    signals: SignalProvider | None = None,
    beats: bool = True,
    beat_range: tuple[float, float] = (70.0, 180.0),
    fallback_bpm: float | None = None,
    beats_per_bar: int = 4,
) -> Material:
    """Collect footage, songs and signals. Source media is only read."""
    if bool(media) == bool(fcpxml):
        raise ConductorError("assembly needs exactly one of a media folder or an FCPXML")
    overrides = dict(overrides or {})
    warnings: list[str] = []
    if media is not None:
        root = Path(media)
        video_overrides = {k: v for k, v in overrides.items() if Path(k).suffix.lower() in VIDEO_EXTENSIONS}
        found = inventory(root, probe=probe, overrides=video_overrides)
        clips = found.clips
        warnings.extend(found.warnings)
        root = found.media_dir
        song_paths = _audio_files(root) + _audio_files(root / "music")
    else:
        root = None
        clips, song_paths, fcp_warnings = _from_fcpxml(Path(fcpxml))
        warnings.extend(fcp_warnings)
    for extra in _music_inputs(music):
        if extra.is_dir():
            song_paths.extend(_audio_files(extra))
        elif extra.is_file():
            song_paths.append(extra.resolve())
        else:
            warnings.append(f"music not on this machine, skipped: {extra}")
    seen: set[Path] = set()
    songs: list[Song] = []
    for path in song_paths:
        resolved = path.resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        song, warning = _song(resolved, overrides)
        if warning:
            warnings.append(warning)
        if song is None:
            continue
        if beats:
            song.beats = detect_beats(
                resolved,
                duration=float(song.duration),
                min_bpm=beat_range[0],
                max_bpm=beat_range[1],
                fallback_bpm=fallback_bpm,
                beats_per_bar=beats_per_bar,
            )
            if song.beats is None:
                warnings.append(
                    f"{song.name}: no beat grid (not WAV, ffmpeg missing or unreadable, and no "
                    "fallback_bpm); cuts land on frames, not beats."
                )
            elif song.beats.source == "fallback":
                warnings.append(f"{song.name}: beat grid is the profile's fallback_bpm, not detected.")
        songs.append(song)
    if not songs:
        warnings.append("no music files found; the timeline has no music bed and no beat grid.")
    footage = [_footage(clip, signals) for clip in clips]
    if transcript_path:
        warnings.extend(_apply_transcript(footage, Path(transcript_path)))
    first = clips[0]
    hashes = {clip.path: clip.blake2b for clip in clips if clip.blake2b}
    hashes.update({song.path: song.blake2b for song in songs})
    return Material(
        root=root,
        footage=footage,
        songs=songs,
        frame=first.frame_duration or DEFAULT_FRAME,
        width=first.width or DEFAULT_WIDTH,
        height=first.height or DEFAULT_HEIGHT,
        warnings=warnings,
        hashes=hashes,
    )


def _music_inputs(music) -> list[Path]:
    if music is None:
        return []
    if isinstance(music, (str, Path)):
        return [Path(music)]
    return [Path(item) for item in music]


def _apply_transcript(footage: list[Footage], path: Path) -> list[str]:
    """Attach a room-run transcript when it names one clip, or the only A-roll."""
    if not path.is_file():
        return [f"no transcript at {path}; per-clip sidecars are used when they exist"]
    loaded = load_sidecar_cues(path)
    if not loaded or not loaded[0]:
        return [f"{path.name} has no cues; per-clip sidecars are used when they exist"]
    cues, words = loaded
    named = [item for item in footage if item.clip.path.stem == path.stem and not item.signals.has_transcript]
    if len(named) == 1:
        _set_transcript(named[0], cues, words, path)
        return []
    bare = [item for item in footage if item.role == "a_roll" and not item.signals.has_transcript]
    if len(bare) == 1:
        kept = [cue for cue in cues if cue.start < bare[0].clip.duration]
        if kept:
            _set_transcript(bare[0], kept, words, path)
            return []
    return [
        f"{path.name} was not mapped onto a clip (the name does not match, and more than one "
        "A-roll has no sidecar). Per-clip transcripts are used instead."
    ]


def _set_transcript(item: Footage, cues: list[Cue], words: list[Word], path: Path) -> None:
    item.signals = ClipSignals(cues=list(cues), words=list(words), source=f"transcript:{path.name}")
    item.role, item.role_reason = role_for(item.clip, item.signals)


def assert_unchanged(material: Material) -> None:
    for path, digest in material.hashes.items():
        if not path.is_file() or file_hash(path) != digest:
            raise ConductorError(f"refusing to finish: source media changed: {path}")


def default_signals(clip: MediaClip) -> ClipSignals:
    sidecar = _sidecar(clip.path)
    if sidecar is not None:
        return sidecar
    if clip.has_audio and clip.duration_source != "fallback" and clip.path.is_file():
        silences = ffmpeg_silences(clip.path)
        if silences is not None:
            return ClipSignals(silences=silences, source="ffmpeg:silencedetect")
    return ClipSignals(source="none")


def ffmpeg_silences(
    path: Path, *, noise_db: float = -35.0, min_seconds: float = 0.35
) -> list[tuple[Fraction, Fraction]] | None:
    binary = shutil.which("ffmpeg")
    if binary is None:
        return None
    command = [
        binary,
        "-hide_banner",
        "-nostats",
        "-i",
        str(path),
        "-vn",
        "-af",
        f"silencedetect=noise={noise_db}dB:d={min_seconds}",
        "-f",
        "null",
        "-",
    ]
    try:
        completed = subprocess.run(command, capture_output=True, text=True, timeout=120, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if completed.returncode != 0:
        return None
    silences: list[tuple[Fraction, Fraction]] = []
    start: Fraction | None = None
    for line in completed.stderr.splitlines():
        if "silence_start:" in line:
            start = _fraction(line.split("silence_start:")[1].split()[0])
        elif "silence_end:" in line and start is not None:
            end = _fraction(line.split("silence_end:")[1].split()[0])
            silences.append((max(Fraction(0), start), end))
            start = None
    if start is not None:
        silences.append((max(Fraction(0), start), Fraction(10**9)))
    return silences


def load_sidecar_cues(path: Path) -> tuple[list[Cue], list[Word]] | None:
    """SRT, WebVTT, or whisper/whisperx JSON. None when the file is unreadable."""
    try:
        text = path.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeError):
        return None
    if path.suffix.lower() == ".json":
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            return None
        segments = data.get("segments") if isinstance(data, dict) else None
        if not isinstance(segments, list):
            return None
        cues: list[Cue] = []
        words: list[Word] = []
        for segment in segments:
            if not isinstance(segment, dict):
                continue
            try:
                start = _fraction(segment["start"])
                end = _fraction(segment["end"])
            except (KeyError, TypeError, ValueError):
                continue
            body = " ".join(str(segment.get("text") or "").split())
            if body and end > start:
                cues.append(Cue(start, end, body))
            for word in segment.get("words") or []:
                if not isinstance(word, dict) or "start" not in word or "end" not in word:
                    continue
                token = str(word.get("word") or word.get("text") or "").strip()
                if token:
                    words.append(Word(_fraction(word["start"]), _fraction(word["end"]), token))
        return sorted(cues, key=lambda c: (c.start, c.end)), words
    cues = parse_cues(text)
    return (cues, []) if cues else None


def role_for(clip: MediaClip, signals: ClipSignals) -> tuple[str, str]:
    """``a_roll`` or ``b_roll``, and why. A heuristic the decision batch can overrule."""
    name = clip.name
    if not clip.has_audio:
        return "b_roll", "no audio stream"
    if BROLL_HINT.search(name):
        return "b_roll", "file name reads as b-roll"
    if signals.has_transcript:
        speech = [cue for cue in signals.cues if not is_trivial_filler(cue.text)]
        if speech:
            return "a_roll", f"{len(speech)} transcript cues"
        return "b_roll", "transcript is only filler"
    if signals.silences:
        voiced = signals.speech_ranges(clip.duration, Fraction(1, 2))
        if voiced:
            return "a_roll", f"{len(voiced)} non-silent ranges (silencedetect)"
        return "b_roll", "silent throughout (silencedetect)"
    if AROLL_HINT.search(name):
        return "a_roll", "file name reads as A-roll; no transcript or silence data"
    return "a_roll", "has audio; no transcript or silence data"


def _footage(clip: MediaClip, provider: SignalProvider | None) -> Footage:
    found: ClipSignals | None = None
    chain: list[tuple[str, SignalProvider]] = []
    if provider is not None:
        chain.append(("argument", provider))
    chain.extend(_PROVIDERS)
    for name, fn in chain:
        found = fn(clip)
        if found is not None:
            if found.source == "none":
                found.source = f"provider:{name}"
            break
    if found is None:
        found = default_signals(clip)
    role, reason = role_for(clip, found)
    return Footage(clip=clip, signals=found, role=role, role_reason=reason)


def _sidecar(path: Path) -> ClipSignals | None:
    for suffix in (".srt", ".vtt", ".json"):
        candidate = path.with_suffix(suffix)
        if candidate.is_file():
            loaded = load_sidecar_cues(candidate)
            if loaded is not None:
                cues, words = loaded
                return ClipSignals(cues=cues, words=words, source=f"sidecar:{suffix[1:]}")
    return None


def _audio_files(folder: Path) -> list[Path]:
    if not folder.is_dir():
        return []
    paths = [
        entry.resolve()
        for entry in folder.iterdir()
        if entry.is_file() and not entry.name.startswith(".") and entry.suffix.lower() in AUDIO_EXTENSIONS
    ]
    return sorted(paths, key=lambda item: (item.name.casefold(), item.name))


def _song(path: Path, overrides: dict[str, Fraction]) -> tuple[Song | None, str | None]:
    channels, rate = 2, 48000
    duration: Fraction | None = None
    source = "override"
    if path.name in overrides:
        duration = overrides[path.name]
    if path.suffix.lower() == ".wav":
        try:
            with wave.open(str(path), "rb") as handle:
                channels = handle.getnchannels()
                rate = handle.getframerate()
                if duration is None:
                    duration = Fraction(handle.getnframes(), rate)
                    source = "wav"
        except (wave.Error, EOFError, OSError):
            pass
    if duration is None:
        probed = _ffprobe_audio(path)
        if probed is not None:
            duration, channels, rate = probed
            source = "ffprobe"
    if duration is None or duration <= 0:
        return None, f"{path.name}: music length unknown (no ffprobe, not WAV, no --durations entry); skipped."
    return (
        Song(
            path=path,
            name=path.stem,
            src=file_url(path),
            uid=_uid(path),
            duration=quantize(duration, Fraction(1, 1000)),
            channels=channels,
            rate=rate,
            blake2b=file_hash(path),
            duration_source=source,
        ),
        None,
    )


def _ffprobe_audio(path: Path) -> tuple[Fraction, int, int] | None:
    binary = shutil.which("ffprobe")
    if binary is None:
        return None
    command = [binary, "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path)]
    try:
        completed = subprocess.run(command, capture_output=True, text=True, timeout=30, check=False)
        payload = json.loads(completed.stdout or "{}")
    except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError):
        return None
    audio = next(
        (s for s in payload.get("streams") or [] if isinstance(s, dict) and s.get("codec_type") == "audio"),
        None,
    )
    if audio is None:
        return None
    raw = (payload.get("format") or {}).get("duration") or audio.get("duration")
    try:
        duration = Fraction(str(raw))
    except (TypeError, ValueError, ZeroDivisionError):
        return None
    channels = int(audio.get("channels") or 2)
    rate = int(audio.get("sample_rate") or 48000)
    return duration, channels, rate


def _from_fcpxml(path: Path) -> tuple[list[MediaClip], list[Path], list[str]]:
    document = parse_fcpxml(path)
    clips: list[MediaClip] = []
    songs: list[Path] = []
    warnings: list[str] = []
    for asset in document.assets.values():
        if not asset.src or not asset.src.startswith("file://"):
            warnings.append(f"asset {asset.name!r} has no file:// media-rep; skipped.")
            continue
        file = Path(unquote(urlparse(asset.src).path))
        if not asset.has_video:
            if asset.has_audio and file.suffix.lower() in AUDIO_EXTENSIONS | {""}:
                songs.append(file)
            continue
        fmt = document.formats.get(asset.format_id or "")
        frame = fmt.frame_duration if fmt else DEFAULT_FRAME
        exists = file.is_file()
        if not exists:
            warnings.append(f"{file.name}: not found on this machine; signals skipped, Final Cut will need Relink.")
        clips.append(
            MediaClip(
                path=file,
                name=file.name,
                stem=asset.name or file.stem,
                src=asset.src,
                uid=_uid(file),
                duration=quantize(asset.duration, frame) if asset.duration > 0 else Fraction(10),
                duration_source="fcpxml",
                frame_duration=frame,
                width=(fmt.width if fmt and fmt.width else DEFAULT_WIDTH),
                height=(fmt.height if fmt and fmt.height else DEFAULT_HEIGHT),
                has_video=True,
                has_audio=asset.has_audio,
                blake2b=file_hash(file) if exists else "",
            )
        )
    if not clips:
        raise ConductorError(f"{path} has no video assets with file:// media to assemble")
    clips.sort(key=lambda clip: (clip.name.casefold(), clip.name))
    return clips, songs, warnings


def _uid(path: Path) -> str:
    import hashlib

    return hashlib.blake2b(f"asset\0{path}".encode(), digest_size=8).hexdigest().upper()


def _fraction(value) -> Fraction:
    return Fraction(str(value)).limit_denominator(1000)
