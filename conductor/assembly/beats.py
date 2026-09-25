"""Beat and onset detection for music beds, with graceful fallbacks.

Order of preference:

1. ``<song>.beats.json`` beside the file (``{"bpm", "beats", "downbeats"}``
   in seconds) — a better analyzer can drop one there.
2. Decode and detect. WAV is read with the standard library; anything else
   is decoded by ffmpeg to mono float PCM. Detection is a spectral-flux onset
   envelope, an autocorrelation tempo estimate inside ``[min_bpm, max_bpm]``,
   and the phase that lines the grid up with the strongest onsets.
3. The profile's ``fallback_bpm`` as a synthetic grid from 0s.
4. None. The engine then cuts on frames and says so.

Deterministic: the same bytes give the same grid.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import wave
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

SAMPLE_RATE = 11025
HOP = 256
N_FFT = 1024
LOW_HZ = 180.0


@dataclass
class BeatGrid:
    bpm: float
    beats: list[float]
    downbeats: list[float] = field(default_factory=list)
    onsets: list[float] = field(default_factory=list)
    source: str = "detected"
    confidence: float = 0.0

    @property
    def period(self) -> float:
        return 60.0 / self.bpm

    def to_state(self) -> dict:
        return {
            "bpm": round(self.bpm, 3),
            "source": self.source,
            "confidence": round(self.confidence, 3),
            "beat_count": len(self.beats),
            "first_beat": round(self.beats[0], 4) if self.beats else None,
            "first_downbeat": round(self.downbeats[0], 4) if self.downbeats else None,
        }


def detect_beats(
    path: Path,
    *,
    duration: float,
    min_bpm: float = 70.0,
    max_bpm: float = 180.0,
    fallback_bpm: float | None = None,
    beats_per_bar: int = 4,
) -> BeatGrid | None:
    sidecar = _sidecar(path, beats_per_bar)
    if sidecar is not None:
        return sidecar
    samples = decode(path)
    if samples is not None and samples.size >= SAMPLE_RATE:
        grid = grid_from_samples(samples, min_bpm=min_bpm, max_bpm=max_bpm, beats_per_bar=beats_per_bar)
        if grid is not None:
            grid.source = "wav" if path.suffix.lower() == ".wav" else "ffmpeg"
            return grid
    if fallback_bpm:
        return synthetic_grid(fallback_bpm, duration, beats_per_bar)
    return None


def synthetic_grid(bpm: float, duration: float, beats_per_bar: int = 4) -> BeatGrid:
    period = 60.0 / bpm
    count = int(duration / period) + 1
    beats = [round(i * period, 6) for i in range(count) if i * period < duration]
    return BeatGrid(
        bpm=bpm,
        beats=beats,
        downbeats=beats[::beats_per_bar],
        onsets=[],
        source="fallback",
        confidence=0.0,
    )


def decode(path: Path) -> np.ndarray | None:
    """Mono float32 at :data:`SAMPLE_RATE`, or None when nothing can decode it."""
    if path.suffix.lower() == ".wav":
        pcm = _read_wav(path)
        if pcm is not None:
            return pcm
    return _ffmpeg_decode(path)


def grid_from_samples(
    samples: np.ndarray,
    *,
    min_bpm: float,
    max_bpm: float,
    beats_per_bar: int = 4,
) -> BeatGrid | None:
    envelope = onset_envelope(samples)
    if envelope.size < 16 or not np.any(envelope > 0):
        return None
    frame_rate = SAMPLE_RATE / HOP
    lag, strength = _tempo_lag(envelope, frame_rate, min_bpm, max_bpm)
    if lag is None:
        return None
    phase = _best_phase(envelope, lag)
    phase, lag = _refine(envelope, phase, lag)
    low = onset_envelope(samples, max_hz=LOW_HZ)
    if _grid_strength(low, phase + lag / 2, lag) > 1.2 * _grid_strength(low, phase, lag):
        phase += lag / 2
    duration = samples.size / SAMPLE_RATE
    centre = (N_FFT / 2 + HOP) / SAMPLE_RATE
    while phase - lag + centre * frame_rate >= 0:
        phase -= lag
    positions: list[float] = []
    beats: list[float] = []
    k = 0
    while True:
        position = phase + k * lag
        t = position / frame_rate + centre
        if t >= duration:
            break
        if t >= 0:
            positions.append(position)
            beats.append(round(t, 6))
        k += 1
    if len(beats) < 2:
        return None
    downbeat_offset = _downbeat_offset(envelope, low, positions, beats_per_bar)
    onsets = _peaks(envelope, frame_rate)
    return BeatGrid(
        bpm=round(60.0 * frame_rate / lag, 3),
        beats=beats,
        downbeats=beats[downbeat_offset::beats_per_bar],
        onsets=onsets,
        confidence=round(float(strength), 4),
    )


def onset_envelope(samples: np.ndarray, *, max_hz: float | None = None) -> np.ndarray:
    """Half-wave rectified spectral flux on a log-magnitude STFT, detrended.

    ``max_hz`` limits the flux to low bins (kick and bass), which is how the
    grid is told apart from an off-beat hi-hat.
    """
    if samples.size < N_FFT:
        return np.zeros(0, dtype=np.float64)
    count = 1 + (samples.size - N_FFT) // HOP
    strides = (samples.strides[0] * HOP, samples.strides[0])
    frames = np.lib.stride_tricks.as_strided(samples, shape=(count, N_FFT), strides=strides)
    window = np.hanning(N_FFT).astype(np.float32)
    magnitude = np.abs(np.fft.rfft(frames * window, axis=1))
    if max_hz is not None:
        magnitude = magnitude[:, : max(2, int(max_hz * N_FFT / SAMPLE_RATE) + 1)]
    log_mag = np.log1p(100.0 * magnitude)
    flux = np.maximum(0.0, np.diff(log_mag, axis=0)).sum(axis=1)
    flux = np.concatenate([[0.0], flux])
    width = 16
    if flux.size > width:
        kernel = np.ones(width) / width
        flux = flux - np.convolve(flux, kernel, mode="same")
    flux = np.maximum(flux, 0.0)
    peak = flux.max()
    return flux / peak if peak > 0 else flux


def _tempo_lag(
    envelope: np.ndarray, frame_rate: float, min_bpm: float, max_bpm: float
) -> tuple[float | None, float]:
    centered = envelope - envelope.mean()
    size = 1
    while size < 2 * centered.size:
        size *= 2
    spectrum = np.fft.rfft(centered, size)
    autocorr = np.fft.irfft(spectrum * np.conj(spectrum), size)[: centered.size]
    if autocorr[0] <= 0:
        return None, 0.0
    autocorr = autocorr / autocorr[0]
    low = max(1, int(np.floor(frame_rate * 60.0 / max_bpm)))
    high = min(autocorr.size - 2, int(np.ceil(frame_rate * 60.0 / min_bpm)))
    if high <= low:
        return None, 0.0
    lags = np.arange(low, high + 1)
    bpms = 60.0 * frame_rate / lags
    prior = np.exp(-0.5 * (np.log2(bpms / 120.0) / 1.0) ** 2)
    scores = autocorr[low : high + 1] * prior
    best = int(lags[int(np.argmax(scores))])
    left, middle, right = autocorr[best - 1], autocorr[best], autocorr[best + 1]
    denom = left - 2 * middle + right
    shift = 0.5 * (left - right) / denom if denom != 0 else 0.0
    shift = float(np.clip(shift, -0.5, 0.5))
    return best + shift, float(max(0.0, middle))


def _best_phase(envelope: np.ndarray, lag: float) -> float:
    best_phase, best_score = 0.0, -1.0
    steps = max(1, int(np.ceil(lag)))
    for phase in range(steps):
        positions = np.arange(phase, envelope.size, lag)
        index = np.rint(positions).astype(int)
        index = index[index < envelope.size]
        score = float(envelope[index].sum()) / max(1, index.size)
        if score > best_score:
            best_phase, best_score = float(phase), score
    return best_phase


def _grid_strength(envelope: np.ndarray, phase: float, lag: float) -> float:
    if envelope.size == 0:
        return 0.0
    index = np.rint(np.arange(phase, envelope.size, lag)).astype(int)
    index = index[(index >= 0) & (index < envelope.size)]
    return float(envelope[index].mean()) if index.size else 0.0


def _refine(envelope: np.ndarray, phase: float, lag: float) -> tuple[float, float]:
    """Least-squares fit of the grid to the strongest onset near each predicted beat."""
    radius = max(1, int(lag / 4))
    ks: list[float] = []
    ts: list[float] = []
    weights: list[float] = []
    k = 0
    while phase + k * lag < envelope.size:
        centre = int(round(phase + k * lag))
        low, high = max(0, centre - radius), min(envelope.size, centre + radius + 1)
        window = envelope[low:high]
        if window.size and window.max() > 0.2:
            ks.append(float(k))
            ts.append(float(low + int(np.argmax(window))))
            weights.append(float(window.max()))
        k += 1
    if len(ks) < 4:
        return phase, lag
    w = np.asarray(weights)
    slope, intercept = np.polyfit(np.asarray(ks), np.asarray(ts), 1, w=w)
    if not 0.9 * lag <= slope <= 1.1 * lag:
        return phase, lag
    return float(intercept), float(slope)


def _downbeat_offset(
    envelope: np.ndarray, low: np.ndarray, positions: list[float], beats_per_bar: int
) -> int:
    """The bar phase whose beats carry the most onset energy (full band plus low band)."""
    strengths = []
    for position in positions:
        centre = int(round(position))
        lo, hi = max(0, centre - 2), centre + 3
        full = envelope[lo:hi].max() if envelope[lo:hi].size else 0.0
        bass = low[lo:hi].max() if low[lo:hi].size else 0.0
        strengths.append(float(full) + float(bass))
    values = np.asarray(strengths)
    scores = [float(values[offset::beats_per_bar].mean()) for offset in range(min(beats_per_bar, len(values)))]
    return int(np.argmax(scores)) if scores else 0


def _peaks(envelope: np.ndarray, frame_rate: float) -> list[float]:
    threshold = envelope.mean() + envelope.std()
    gap = max(1, int(0.1 * frame_rate))
    found: list[float] = []
    last = -gap
    for i in range(1, envelope.size - 1):
        if envelope[i] >= threshold and envelope[i] >= envelope[i - 1] and envelope[i] > envelope[i + 1]:
            if i - last >= gap:
                found.append(round(i / frame_rate, 4))
                last = i
    return found


def _read_wav(path: Path) -> np.ndarray | None:
    try:
        with wave.open(str(path), "rb") as handle:
            channels = handle.getnchannels()
            width = handle.getsampwidth()
            rate = handle.getframerate()
            raw = handle.readframes(handle.getnframes())
    except (wave.Error, EOFError, OSError):
        return None
    if width == 1:
        data = (np.frombuffer(raw, dtype=np.uint8).astype(np.float32) - 128.0) / 128.0
    elif width == 2:
        data = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
    elif width == 3:
        bytes3 = np.frombuffer(raw, dtype=np.uint8).reshape(-1, 3)
        ints = (bytes3[:, 0].astype(np.int32) | (bytes3[:, 1].astype(np.int32) << 8) | (bytes3[:, 2].astype(np.int32) << 16))
        ints = np.where(ints & 0x800000, ints - 0x1000000, ints)
        data = ints.astype(np.float32) / 8388608.0
    elif width == 4:
        data = np.frombuffer(raw, dtype="<i4").astype(np.float32) / 2147483648.0
    else:
        return None
    if channels > 1:
        data = data[: data.size - data.size % channels].reshape(-1, channels).mean(axis=1)
    return _resample(data, rate)


def _resample(data: np.ndarray, rate: int) -> np.ndarray:
    if rate == SAMPLE_RATE or data.size == 0:
        return np.ascontiguousarray(data, dtype=np.float32)
    duration = data.size / rate
    count = int(duration * SAMPLE_RATE)
    source = np.linspace(0.0, data.size - 1, num=count)
    return np.interp(source, np.arange(data.size), data).astype(np.float32)


def _ffmpeg_decode(path: Path) -> np.ndarray | None:
    binary = shutil.which("ffmpeg")
    if binary is None:
        return None
    command = [
        binary,
        "-v",
        "error",
        "-i",
        str(path),
        "-vn",
        "-ac",
        "1",
        "-ar",
        str(SAMPLE_RATE),
        "-f",
        "f32le",
        "-",
    ]
    try:
        completed = subprocess.run(command, capture_output=True, timeout=300, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if completed.returncode != 0 or not completed.stdout:
        return None
    return np.frombuffer(completed.stdout, dtype="<f4").copy()


def _sidecar(path: Path, beats_per_bar: int) -> BeatGrid | None:
    file = path.with_suffix(".beats.json")
    if not file.is_file():
        return None
    try:
        data = json.loads(file.read_text(encoding="utf-8"))
        beats = [float(b) for b in data["beats"]]
        bpm = float(data.get("bpm") or (60.0 / np.median(np.diff(beats))))
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
        return None
    if len(beats) < 2:
        return None
    downbeats = [float(b) for b in data.get("downbeats") or beats[::beats_per_bar]]
    return BeatGrid(bpm=bpm, beats=beats, downbeats=downbeats, onsets=[], source="sidecar", confidence=1.0)
