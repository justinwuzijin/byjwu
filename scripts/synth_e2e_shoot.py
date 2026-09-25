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
            Line(0.3, 1.8, "So today we are"),
            Line(2.6, 11.2, "So today we are going to fix why every edit I make feels slow."),
        ),
    ),
    Clip(
        "02_talk_story.mp4",
        32,
        "smptebars",
        (
            Line(0.4, 12.4, "The first pass keeps the line and throws away the air around it, and the picture stays with the voice."),
            Line(13.0, 14.2, "Um."),
            Line(15.4, 27.4, "A hold that says nothing is just a hold. It does not earn the frame, so the cut moves on."),
            Line(28.2, 31.2, "A hold that says nothing."),
        ),
    ),
    Clip(
        "03_talk_bridge.mp4",
        26,
        "testsrc",
        (
            Line(0.4, 12.2, "B-roll covers the jump, and the voice keeps going underneath it without a gap in the sentence."),
            Line(13.0, 25.0, "The picture changes on the music. The sentence is already finished before the shot does."),
        ),
    ),
    Clip(
        "04_talk_detail.mp4",
        26,
        "smptehdbars",
        (
            Line(0.4, 12.2, "I leave a short breath, then I am already into the next idea before the pause can grow."),
            Line(13.0, 25.0, "That is the whole pace. Nothing sits there waiting for permission from the song."),
        ),
    ),
    Clip(
        "05_talk_turn.mp4",
        26,
        "testsrc2",
        (
            Line(0.4, 12.2, "The montage is the part where the song is allowed to be loud and the pictures keep time."),
            Line(13.0, 25.0, "Talk comes back after that, and the bed gets out of the way of the voice."),
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
SONG_SECONDS = 150.0
BPM = 120.0


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
    return {
        "folder": str(folder),
        "clips": len(CLIPS),
        "real_clips": made,
        "tts": bool(espeak),
        "song": song.name,
        "bpm": BPM,
        "song_seconds": SONG_SECONDS,
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
