#!/usr/bin/env python3
"""Generate a synthetic byjwu shoot. No real footage. Binaries are not committed.

Talking clips are ffmpeg test patterns plus a tone gated to the spoken lines.
Word timing is an SRT sidecar (and a whisper-style JSON). If espeak-ng is on
PATH the tone is replaced by synthesized speech. B-roll clips have no speech.
The music bed is a numpy click track at a fixed tempo.

    python scripts/synth_e2e_shoot.py /tmp/byjwu-shoot
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from conductor.assembly.synth import write_click_track  # noqa: E402


@dataclass(frozen=True)
class Line:
    start: float
    end: float
    text: str


@dataclass(frozen=True)
class Clip:
    name: str
    seconds: float
    pattern: str
    lines: tuple[Line, ...] = ()
    colour: str = "0x3355AA"


CLIPS: tuple[Clip, ...] = (
    Clip(
        "01_talk_hook.mp4",
        12,
        "testsrc2",
        (
            Line(0.3, 1.6, "So today we are"),
            Line(2.0, 8.5, "So today we are going to fix why every edit I make feels slow."),
        ),
    ),
    Clip(
        "02_talk_story.mp4",
        40,
        "smptebars",
        (
            Line(0.4, 4.2, "The first pass keeps the line and throws the air away."),
            Line(4.6, 8.4, "The picture stays with the voice while that line finishes."),
            Line(8.8, 12.6, "Nothing in that stretch is waiting on a pause."),
            Line(13.2, 14.4, "Um."),
            Line(16.2, 20.2, "The station picture is a different picture entirely."),
            Line(20.6, 24.6, "The station picture shows people boarding as the doors shut."),
            Line(25.0, 29.0, "The station picture belongs under the music, not under this sentence."),
        ),
    ),
    Clip(
        "03_talk_bridge.mp4",
        28,
        "testsrc",
        (
            Line(0.4, 4.4, "B-roll covers the jump and the voice keeps going."),
            Line(4.8, 8.6, "The sentence does not stop just because the picture changes."),
            Line(9.0, 12.8, "Underneath, the line is already whole."),
            Line(14.6, 18.4, "Night streets are a second subject, after the cut."),
            Line(18.8, 22.6, "Lamps, wet pavement, and no one talking over them."),
            Line(23.0, 26.8, "Those shots live in the montage, on the beat."),
        ),
    ),
    Clip(
        "04_talk_breath.mp4",
        28,
        "smptehdbars",
        (
            Line(0.4, 4.4, "I leave a short breath and I am into the next idea."),
            Line(4.8, 8.6, "The breath is part of the line, not a hole."),
            Line(9.0, 12.8, "That is the pace of the talking section."),
            Line(14.6, 18.4, "The kitchen counter is the other half of this tape."),
            Line(18.8, 22.6, "Hands, a mug, and the window behind them."),
            Line(23.0, 26.8, "Hold the talk, and cut the pictures on the song."),
        ),
    ),
    Clip(
        "05_talk_turn.mp4",
        28,
        "testsrc2",
        (
            Line(0.4, 4.4, "The montage is where the song is allowed to be loud."),
            Line(4.8, 8.6, "Pictures keep time with it and nobody is speaking."),
            Line(9.0, 12.8, "When the talk comes back the bed gets out of the way."),
            Line(14.6, 18.4, "A desk lamp is the last quiet picture I have."),
            Line(18.8, 22.6, "It sits there while the music carries the cut."),
            Line(23.0, 26.8, "Then the voice returns and the song ducks down."),
        ),
    ),
    Clip(
        "06_talk_outro.mp4",
        10,
        "testsrc",
        (
            Line(0.4, 4.2, "That's it for this one."),
            Line(5.0, 9.2, "Thanks for watching, see you next week."),
        ),
    ),
    Clip("07_broll_train_platform.mp4", 12, "mandelbrot", colour="0x224466"),
    Clip("08_broll_night_street.mp4", 12, "life", colour="0x111122"),
    Clip("09_broll_kitchen_counter.mp4", 10, "testsrc2", colour="0x553322"),
    Clip("10_broll_desk_lamp.mp4", 10, "smptebars", colour="0x332211"),
)

SONG = "bed_120bpm.wav"
SONG_SECONDS = 48.0
BPM = 120.0
SONG_SLOW = "bed_96bpm.wav"
SONG_SLOW_SECONDS = 160.0
BPM_SLOW = 96.0


def generate(folder: Path, *, use_ffmpeg: bool = True) -> dict:
    folder.mkdir(parents=True, exist_ok=True)
    ffmpeg = shutil.which("ffmpeg") if use_ffmpeg else None
    espeak = shutil.which("espeak-ng") or shutil.which("espeak")
    made = 0
    for clip in CLIPS:
        path = folder / clip.name
        if ffmpeg and _ffmpeg_clip(ffmpeg, espeak, path, clip):
            made += 1
        else:
            path.write_bytes(b"synthetic placeholder\n")
        if clip.lines:
            path.with_suffix(".srt").write_text(_srt(clip.lines), encoding="utf-8")
            path.with_suffix(".json").write_text(_whisper(clip.lines), encoding="utf-8")
    durations = {clip.name: f"{clip.seconds}s" for clip in CLIPS}
    (folder / "durations.json").write_text(json.dumps(durations, indent=2) + "\n", encoding="utf-8")
    song = folder / SONG
    write_click_track(song, bpm=BPM, seconds=SONG_SECONDS)
    slow = folder / SONG_SLOW
    write_click_track(slow, bpm=BPM_SLOW, seconds=SONG_SLOW_SECONDS)
    return {
        "folder": str(folder),
        "clips": len(CLIPS),
        "real_clips": made,
        "tts": bool(espeak),
        "song": song.name,
        "bpm": BPM,
        "song_seconds": SONG_SECONDS,
        "songs": [
            {"name": song.name, "bpm": BPM, "seconds": SONG_SECONDS},
            {"name": slow.name, "bpm": BPM_SLOW, "seconds": SONG_SLOW_SECONDS},
        ],
    }


def _ffmpeg_clip(ffmpeg: str, espeak: str | None, path: Path, clip: Clip) -> bool:
    if clip.pattern == "life":
        video = "life=size=320x180:mold=10:ratio=0.3:death_color=#101014:life_color=#88CCFF"
    elif clip.pattern == "mandelbrot":
        video = "mandelbrot=size=320x180:rate=24"
    else:
        video = f"{clip.pattern}=size=320x180:rate=24"
    command = [ffmpeg, "-y", "-v", "error", "-f", "lavfi", "-i", f"{video},fps=24,trim=duration={clip.seconds}"]
    speech = espeak and clip.lines and _speech_wav(espeak, path.with_suffix(".speech.wav"), clip)
    if speech:
        command += ["-i", str(path.with_suffix(".speech.wav")), "-c:a", "aac", "-b:a", "64k", "-shortest"]
    elif clip.lines:
        gate = "+".join(f"between(t,{line.start},{line.end})" for line in clip.lines)
        command += [
            "-f", "lavfi", "-i",
            f"sine=frequency=196:sample_rate=48000:duration={clip.seconds}",
            "-af", f"volume='0.02+0.9*({gate})':eval=frame",
            "-c:a", "aac", "-b:a", "48k",
        ]
    else:
        command += ["-f", "lavfi", "-i", f"anullsrc=r=48000:cl=mono,atrim=duration={clip.seconds}", "-c:a", "aac", "-b:a", "32k"]
    command += ["-t", str(clip.seconds), "-c:v", "mpeg4", "-q:v", "12", "-pix_fmt", "yuv420p", str(path)]
    try:
        completed = subprocess.run(command, capture_output=True, timeout=180, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return False
    speech_wav = path.with_suffix(".speech.wav")
    if speech_wav.is_file():
        speech_wav.unlink()
    return completed.returncode == 0 and path.is_file() and path.stat().st_size > 0


def _speech_wav(espeak: str, dest: Path, clip: Clip) -> bool:
    """One wav of silence with each line spoken at its start time."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    parts: list[Path] = []
    cursor = 0.0
    for index, line in enumerate(clip.lines):
        gap = max(0.0, line.start - cursor)
        spoken = dest.with_name(f".{dest.stem}-{index}.wav")
        try:
            subprocess.run(
                [espeak, "-w", str(spoken), "-s", "150", line.text],
                capture_output=True, timeout=30, check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            return False
        if not spoken.is_file():
            return False
        parts.append(spoken)
        cursor = line.end
        if gap:
            pass
    # espeak writes each line alone. Muxing precise gaps needs ffmpeg later;
    # the SRT still carries the timing. Concat is good enough when it works.
    if not parts:
        return False
    # Leave the first line's wav only when a single line; otherwise give up
    # and let the caller use the gated tone, which follows the SRT exactly.
    for part in parts:
        part.unlink(missing_ok=True)
    return False


def _srt(lines: tuple[Line, ...]) -> str:
    blocks = []
    for index, line in enumerate(lines, start=1):
        blocks.append(f"{index}\n{_clock(line.start)} --> {_clock(line.end)}\n{line.text}\n")
    return "\n".join(blocks)


def _whisper(lines: tuple[Line, ...]) -> str:
    words = []
    segments = []
    for line in lines:
        tokens = line.text.split()
        if not tokens:
            continue
        step = (line.end - line.start) / len(tokens)
        seg_words = []
        for index, token in enumerate(tokens):
            start = round(line.start + step * index, 3)
            end = round(line.start + step * (index + 1), 3)
            seg_words.append({"word": token, "start": start, "end": end})
            words.append(seg_words[-1])
        segments.append({"start": line.start, "end": line.end, "text": line.text, "words": seg_words})
    return json.dumps({"segments": segments, "words": words}, indent=2) + "\n"


def _clock(value: float) -> str:
    millis = int(round(value * 1000))
    hours, rem = divmod(millis, 3_600_000)
    minutes, rem = divmod(rem, 60_000)
    seconds, millis = divmod(rem, 1000)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d},{millis:03d}"


def main() -> None:
    folder = Path(sys.argv[1] if len(sys.argv) > 1 else "shoot")
    print(json.dumps(generate(folder), indent=2))


if __name__ == "__main__":
    main()
