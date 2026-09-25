"""Synthetic footage and music for tests and the end-to-end dry run.

``make_fixture(folder)`` writes a small shoot: three talking clips with SRT
sidecars, two silent b-roll clips, a ``durations.json``, and a click-track
song in ``music/``. With ffmpeg the clips are real (tiny test patterns with a
voiced tone that pauses where the SRT pauses); without it they are
placeholder bytes and ``durations.json`` supplies the lengths. The song is
always a real WAV written with numpy, so beat detection runs either way.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import wave
from dataclasses import dataclass
from pathlib import Path

import numpy as np


@dataclass(frozen=True)
class Shot:
    name: str
    seconds: int
    lines: tuple[tuple[float, float, str], ...] = ()
    pattern: str = "testsrc2"


SHOTS: tuple[Shot, ...] = (
    Shot(
        "01_talk_hook.mp4",
        9,
        (
            (0.3, 2.4, "Why does every edit I make feel slow?"),
            (2.9, 5.2, "Today I'm going to fix that with one rule."),
            (5.8, 8.6, "Cut on the beat, and never wait for the music."),
        ),
    ),
    Shot(
        "02_talk_main.mp4",
        14,
        (
            (0.4, 3.1, "The rule is simple. Every shot earns its length."),
            (3.6, 6.4, "If a line lands, the cut lands with it."),
            (6.9, 9.0, "Um."),
            (9.4, 13.4, "Honestly, this is the biggest change I ever made to my workflow!"),
        ),
        "smptehdbars",
    ),
    Shot("03_broll_city.mp4", 7, (), "mandelbrot"),
    Shot("04_broll_desk.mp4", 6, (), "life=size=320x180:mold=10:ratio=0.3:death_color=#101014:life_color=#FF2D6F"),
    Shot(
        "05_talk_outro.mp4",
        6,
        (
            (0.3, 2.6, "That's it for this one."),
            (3.0, 5.6, "Thanks for watching, see you next week."),
        ),
    ),
)
SONG = "song_120bpm.wav"


def make_fixture(folder: Path, *, use_ffmpeg: bool = True, song_seconds: float = 40.0, bpm: float = 120.0) -> dict:
    """Write the shoot into ``folder``. Returns what was written and how."""
    folder.mkdir(parents=True, exist_ok=True)
    ffmpeg = shutil.which("ffmpeg") if use_ffmpeg else None
    real = False
    for shot in SHOTS:
        path = folder / shot.name
        made = ffmpeg is not None and _ffmpeg_clip(ffmpeg, path, shot)
        real = real or made
        if not made:
            path.write_bytes(b"jevid synthetic placeholder clip\n")
        if shot.lines:
            path.with_suffix(".srt").write_text(_srt(shot.lines), encoding="utf-8")
    durations = {shot.name: f"{shot.seconds}s" for shot in SHOTS}
    (folder / "durations.json").write_text(json.dumps(durations, indent=2) + "\n", encoding="utf-8")
    song = folder / "music" / SONG
    write_click_track(song, bpm=bpm, seconds=song_seconds)
    return {"folder": str(folder), "real_clips": real, "song": str(song), "durations": str(folder / "durations.json")}


def write_click_track(path: Path, *, bpm: float = 120.0, seconds: float = 40.0, rate: int = 22050) -> None:
    """Kick on every beat (accented downbeat), hat on the off-beat, a quiet pad."""
    t = np.arange(int(rate * seconds)) / rate
    pad = 0.06 * (np.sin(2 * np.pi * 110 * t) + 0.5 * np.sin(2 * np.pi * 164.8 * t))
    signal = pad.copy()
    period = 60.0 / bpm
    beat = 0
    while beat * period < seconds:
        start = int(beat * period * rate)
        length = min(int(0.12 * rate), signal.size - start)
        if length <= 0:
            break
        n = np.arange(length) / rate
        amp = 0.9 if beat % 4 == 0 else 0.55
        kick = amp * np.exp(-n / 0.035) * np.sin(2 * np.pi * (120 - 60 * n / 0.12) * n)
        signal[start : start + length] += kick
        hat_at = int((beat + 0.5) * period * rate)
        hat_len = min(int(0.03 * rate), signal.size - hat_at)
        if hat_len > 0:
            noise = np.random.default_rng(beat).standard_normal(hat_len)
            signal[hat_at : hat_at + hat_len] += 0.12 * noise * np.exp(-np.arange(hat_len) / (0.008 * rate))
        beat += 1
    pcm = (np.clip(signal, -1.0, 1.0) * 32767).astype("<i2")
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(pcm.tobytes())


def _ffmpeg_clip(ffmpeg: str, path: Path, shot: Shot) -> bool:
    video = f"{shot.pattern}" if "=" in shot.pattern else f"{shot.pattern}=size=320x180:rate=24"
    command = [ffmpeg, "-y", "-v", "error", "-f", "lavfi", "-i", f"{video},fps=24,trim=duration={shot.seconds}"]
    if shot.lines:
        gate = "+".join(f"between(t,{a},{b})" for a, b, _ in shot.lines)
        command += [
            "-f",
            "lavfi",
            "-i",
            f"sine=frequency=180:sample_rate=48000:duration={shot.seconds}",
            "-af",
            f"volume='0.02+0.9*({gate})':eval=frame",
            "-c:a",
            "aac",
            "-b:a",
            "48k",
        ]
    command += ["-t", str(shot.seconds), "-c:v", "mpeg4", "-q:v", "12", "-pix_fmt", "yuv420p", str(path)]
    try:
        completed = subprocess.run(command, capture_output=True, timeout=120, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return completed.returncode == 0 and path.is_file() and path.stat().st_size > 0


def _srt(lines: tuple[tuple[float, float, str], ...]) -> str:
    blocks = []
    for index, (start, end, text) in enumerate(lines, start=1):
        blocks.append(f"{index}\n{_clock(start)} --> {_clock(end)}\n{text}\n")
    return "\n".join(blocks)


def _clock(value: float) -> str:
    millis = int(round(value * 1000))
    hours, rem = divmod(millis, 3_600_000)
    minutes, rem = divmod(rem, 60_000)
    seconds, millis = divmod(rem, 1000)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d},{millis:03d}"
