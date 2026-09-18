"""Tier 1 — deterministic extraction. Never calls a model.

Everything here is exact, cached and free: transcript, pauses, speakers,
loudness, and the regex-obvious filler that must never reach Jev. New signals
belong here as structured fields rather than in tier 2 as cleverer
instructions — Jev judges better on good structured state than on prose.

Cache lives at `.cutmcp-cache/{media_id}.extract.json`. `media_id` is a
blake2b of the resolved path, size and mtime, so touching the media
invalidates it without anyone having to remember to.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

__all__ = [
    "FILLERS",
    "Segment",
    "Media",
    "ingest",
    "load",
    "cache_dir",
    "media_id_for",
]

#: Filler that a regex can catch with no judgment involved. Deliberately
#: tight: only tokens that carry no content in any context. "like", "right"
#: and "yeah" are *not* here — they are often load-bearing, so they go to the
#: `filler` noul in tier 2 instead of being deleted by a pattern.
FILLERS: tuple[str, ...] = (
    "um",
    "umm",
    "uhm",
    "uh",
    "uhh",
    "er",
    "erm",
    "ah",
    "mm",
    "mhm",
    "mmhmm",
    "hmm",
    "uh huh",
    "uh-huh",
    "you know",
    "i mean",
)

_FILLER_RE = re.compile(
    r"^(?:{alt})(?:[\s,.\-]+(?:{alt}))*$".format(
        alt="|".join(re.escape(f) for f in sorted(FILLERS, key=len, reverse=True))
    )
)
_PUNCT_RE = re.compile(r"[^\w\s\-']+")
_LOUDNESS_FLOOR_DB = -70.0
_PCM_RATE = 8000


def cache_dir() -> Path:
    """Where tiers 1–3 persist. Override with `CUTMCP_CACHE`."""
    d = Path(os.environ.get("CUTMCP_CACHE", ".cutmcp-cache"))
    d.mkdir(parents=True, exist_ok=True)
    return d


@dataclass
class Segment:
    """One transcript line plus everything measurable about it."""

    idx: int
    start: float
    end: float
    text: str
    speaker: str
    gap_before: float
    gap_after: float
    lufs: float | None
    words: int
    is_trivial_filler: bool

    @property
    def duration(self) -> float:
        return self.end - self.start


@dataclass
class Media:
    media_id: str
    path: str
    duration: float
    segments: list[Segment] = field(default_factory=list)

    @property
    def speakers(self) -> list[str]:
        return sorted({s.speaker for s in self.segments})

    def to_dict(self) -> dict[str, Any]:
        return {
            "media_id": self.media_id,
            "path": self.path,
            "duration": self.duration,
            "segments": [asdict(s) for s in self.segments],
        }

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> Media:
        return cls(
            media_id=d["media_id"],
            path=d["path"],
            duration=float(d["duration"]),
            segments=[Segment(**s) for s in d["segments"]],
        )

    def save(self) -> Path:
        p = cache_dir() / f"{self.media_id}.extract.json"
        p.write_text(json.dumps(self.to_dict(), indent=2, sort_keys=True))
        return p


def media_id_for(path: str | os.PathLike[str]) -> str:
    """blake2b over resolved path + size + mtime. Stable across processes."""
    p = Path(path).resolve()
    st = p.stat()
    h = hashlib.blake2b(digest_size=12)
    h.update(str(p).encode())
    h.update(f"|{st.st_size}|{int(st.st_mtime)}".encode())
    return h.hexdigest()


def ingest(path: str | os.PathLike[str], force: bool = False) -> Media:
    """Extract everything measurable about `path`, cached by `media_id`.

    Transcript source, in order: a whisper JSON sidecar beside the media,
    then `whisperx` or `whisper` on PATH. Diarization matters — `speaker` is
    what makes the interview logic work — so prefer whisperx.
    """
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"no such media file: {p}")
    mid = media_id_for(p)
    cached = cache_dir() / f"{mid}.extract.json"
    if cached.exists() and not force:
        return Media.from_dict(json.loads(cached.read_text()))

    raw = _load_sidecar(p)
    if raw is None:
        raw = _transcribe(p)
    duration = _probe_duration(p)
    segments = _build_segments(raw, duration)
    if duration is None:
        duration = segments[-1].end if segments else 0.0
        if segments:
            segments[-1].gap_after = 0.0
    _attach_loudness(p, segments)

    media = Media(media_id=mid, path=str(p.resolve()), duration=duration, segments=segments)
    media.save()
    return media


def load(media_id: str) -> Media:
    """Rehydrate a `Media` from cache, by id."""
    p = cache_dir() / f"{media_id}.extract.json"
    if not p.exists():
        raise FileNotFoundError(f"no extract cached for media_id {media_id!r}; run ingest first")
    return Media.from_dict(json.loads(p.read_text()))


# --------------------------------------------------------------------------
# transcript sources
# --------------------------------------------------------------------------


def _load_sidecar(p: Path) -> list[dict[str, Any]] | None:
    """Read `foo.mp4` → `foo.json`, whisper or whisperx shape."""
    side = p.with_suffix(".json")
    if not side.exists():
        return None
    return _segments_from_json(json.loads(side.read_text()))


def _segments_from_json(doc: Any) -> list[dict[str, Any]]:
    segs = doc.get("segments") if isinstance(doc, Mapping) else doc
    if not isinstance(segs, list):
        raise ValueError("transcript JSON has no 'segments' list")
    return [s for s in segs if isinstance(s, Mapping)]


def _transcribe(p: Path) -> list[dict[str, Any]]:
    """Shell out to whisperx, else whisper. Both write JSON to a temp dir."""
    for tool in ("whisperx", "whisper"):
        exe = shutil.which(tool)
        if not exe:
            continue
        with tempfile.TemporaryDirectory() as tmp:
            cmd = [exe, str(p), "--output_format", "json", "--output_dir", tmp]
            if tool == "whisperx" and os.environ.get("HF_TOKEN"):
                cmd += ["--diarize", "--hf_token", os.environ["HF_TOKEN"]]
            subprocess.run(cmd, check=True, capture_output=True, stdin=subprocess.DEVNULL)
            out = sorted(Path(tmp).glob("*.json"))
            if not out:
                raise RuntimeError(f"{tool} produced no JSON for {p}")
            return _segments_from_json(json.loads(out[0].read_text()))
    raise RuntimeError(
        f"no transcript for {p.name}. Either install whisperx (`pip install whisperx`, "
        f"diarization gives you speakers) or whisper (`pip install openai-whisper`), "
        f"or drop a whisper JSON sidecar beside the media as {p.with_suffix('.json').name}."
    )


def _probe_duration(p: Path) -> float | None:
    """ffprobe, or None so the caller can fall back to the last segment end."""
    exe = shutil.which("ffprobe")
    if not exe:
        return None
    try:
        r = subprocess.run(
            [exe, "-v", "error", "-show_entries", "format=duration",
             "-of", "default=nw=1:nk=1", str(p)],
            capture_output=True, text=True, timeout=60, stdin=subprocess.DEVNULL,
        )
    except (subprocess.SubprocessError, OSError):
        return None
    if r.returncode != 0:
        return None
    try:
        return float(r.stdout.strip())
    except ValueError:
        return None


# --------------------------------------------------------------------------
# segment assembly
# --------------------------------------------------------------------------


def _build_segments(raw: Sequence[Mapping[str, Any]], duration: float | None) -> list[Segment]:
    rows = []
    for s in raw:
        text = str(s.get("text", "")).strip()
        if not text:
            continue
        start = float(s.get("start", 0.0))
        end = float(s.get("end", start))
        if end < start:
            end = start
        speaker = str(s.get("speaker") or s.get("speaker_id") or "SPEAKER_00")
        words = s.get("words")
        n_words = len(words) if isinstance(words, list) and words else len(text.split())
        rows.append((start, end, text, speaker, n_words))
    rows.sort(key=lambda r: (r[0], r[1]))

    out: list[Segment] = []
    for i, (start, end, text, speaker, n_words) in enumerate(rows):
        gap_before = 0.0 if i == 0 else max(0.0, start - rows[i - 1][1])
        out.append(
            Segment(
                idx=i,
                start=start,
                end=end,
                text=text,
                speaker=speaker,
                gap_before=gap_before,
                gap_after=0.0,
                lufs=None,
                words=n_words,
                is_trivial_filler=is_trivial_filler(text),
            )
        )
    for i, seg in enumerate(out):
        if i + 1 < len(out):
            seg.gap_after = out[i + 1].gap_before
        elif duration is not None:
            seg.gap_after = max(0.0, duration - seg.end)
    return out


def is_trivial_filler(text: str) -> bool:
    """True when the *whole* line is filler, e.g. "Um." or "Uh, you know."

    Partial filler inside a real sentence is a judgment call and belongs to
    the `filler` noul in tier 2, not to a regex.
    """
    norm = _PUNCT_RE.sub(" ", text.lower()).strip()
    norm = re.sub(r"\s+", " ", norm)
    return bool(norm) and bool(_FILLER_RE.match(norm))


# --------------------------------------------------------------------------
# loudness
# --------------------------------------------------------------------------


def _attach_loudness(p: Path, segments: list[Segment]) -> None:
    """Per-segment loudness from one ffmpeg decode pass.

    This is RMS dBFS, not gated K-weighted LUFS — it is a *relative* delivery
    signal, used to tell a mumbled aside from a landed line within the same
    recording, and the field keeps the `lufs` name for continuity. Silently a
    no-op when ffmpeg is absent or the decode fails; downstream treats None
    as "unknown", never as "quiet".
    """
    if not segments:
        return
    pcm = _decode_pcm(p)
    if pcm is None or pcm.size == 0:
        return
    for seg in segments:
        a = int(seg.start * _PCM_RATE)
        b = int(seg.end * _PCM_RATE)
        window = pcm[max(0, a) : min(pcm.size, max(b, a + 1))]
        if window.size == 0:
            continue
        rms = float(np.sqrt(np.mean(np.square(window, dtype=np.float64))))
        seg.lufs = round(max(_LOUDNESS_FLOOR_DB, 20.0 * np.log10(rms)), 2) if rms > 0 else _LOUDNESS_FLOOR_DB


def _decode_pcm(p: Path) -> np.ndarray | None:
    exe = shutil.which("ffmpeg")
    if not exe:
        return None
    try:
        r = subprocess.run(
            [exe, "-v", "error", "-nostdin", "-i", str(p), "-map", "a:0",
             "-ac", "1", "-ar", str(_PCM_RATE), "-f", "s16le", "-"],
            capture_output=True, timeout=600, stdin=subprocess.DEVNULL,
        )
    except (subprocess.SubprocessError, OSError):
        return None
    if r.returncode != 0 or not r.stdout:
        return None
    return np.frombuffer(r.stdout, dtype="<i2").astype(np.float32) / 32768.0
