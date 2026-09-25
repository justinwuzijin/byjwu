"""Internal timeline layers, before FCPXML serialisation.

Packing, silence trim, the volume ramp, and the dissolve window follow
``@diffusionstudio/core`` 4.0.3 (https://github.com/diffusionstudio/core,
MPL-2.0). That library is a browser compositor and watermarks unlicensed
output, so this module does not import it and does not render through it.
The algorithms are reimplemented here and written as FCPXML.

SEQUENTIAL layers pack each clip so its timeline start equals the previous
clip's end (``SequentialInsertStrategy.update``). PARALLEL layers (their
DEFAULT mode) keep each clip's own start and reject overlaps (``Eh``).

A dissolve is centred on the cut: the midpoint is
``max(floor((left.end + right.start) / 2), left.end)``, and the window is
midpoint ± duration/2 (``getTransitionMidpoint`` / start / end). Abutted
clips put the midpoint on the cut. FCPXML writes that window as a
``transition`` whose duration overlaps both sides.

Silence removal uses their RMS detector (mono samples, hop 1024, amplitude
threshold 0.02, minimum run 500 ms) and ``removeSilences`` padding (default
0.5 s on the tail of a gap, none on the lead-in). The ffmpeg path in
``media.ffmpeg_silences`` (-35 dB, about 0.0178 amplitude, minimum 0.35 s)
stays the detector for real media. ``detector_comparison`` records the gap.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from enum import Enum
from fractions import Fraction

from .timeline import Item, Timeline, VolumeKey

# Their ramp eases in log-gain from 0.001 (about -60 dB) to 1. FCPXML writes
# decibels, and the profile floor is -96, so the same two-point ramp is
# expressed in dB. Ducking under dialogue is added on top; their ramp is
# fade-in and fade-out only.
RAMP_FLOOR_GAIN = 0.001
SILENCE_THRESHOLD = 0.02
SILENCE_HOP = 1024
SILENCE_MIN_SECONDS = 0.5
SILENCE_PADDING = 0.5
# ffmpeg silencedetect defaults used by media.ffmpeg_silences.
FFMPEG_NOISE_DB = -35.0
FFMPEG_MIN_SECONDS = 0.35


class LayerMode(Enum):
    SEQUENTIAL = "SEQUENTIAL"
    PARALLEL = "PARALLEL"


@dataclass
class SourceRange:
    """Media in-point and out-point, in source seconds. Their clip ``range``."""

    start: Fraction
    end: Fraction

    def __post_init__(self) -> None:
        if self.end < self.start:
            raise ValueError("source range end is before start")

    @property
    def duration(self) -> Fraction:
        return self.end - self.start


@dataclass
class LayerClip:
    item: Item
    source: SourceRange
    transition: str | None = None
    transition_duration: Fraction | None = None


@dataclass
class Layer:
    mode: LayerMode
    name: str
    clips: list[LayerClip] = field(default_factory=list)

    @property
    def end(self) -> Fraction:
        if not self.clips:
            return Fraction(0)
        return max(clip.item.end for clip in self.clips)


def frame_index(time: Fraction, frame: Fraction) -> int:
    """Their ``B(time)``: round seconds to a frame index."""
    if frame <= 0:
        return int(round(float(time)))
    return int(round(float(time) / float(frame)))


def pack_sequential(clips: list[LayerClip], frame: Fraction) -> None:
    """Pack clips back to back. A start that is already on the cursor stays."""
    cursor = Fraction(0)
    for clip in clips:
        if frame_index(clip.item.offset, frame) != frame_index(cursor, frame):
            clip.item.offset = cursor
        cursor = clip.item.end


def overlaps(left: Item, right: Item, frame: Fraction) -> bool:
    """Their overlap test: different clips whose frame ranges intersect."""
    if left is right:
        return False
    return frame_index(left.offset, frame) < frame_index(right.end, frame) and frame_index(
        right.offset, frame
    ) < frame_index(left.end, frame)


def transition_window(
    left_end: Fraction, right_start: Fraction, duration: Fraction
) -> tuple[Fraction, Fraction, Fraction]:
    """Return ``(start, midpoint, end)`` for a dissolve centred on the cut.

    Midpoint matches ``Math.max(Math.floor((left.end + right.start) / 2), left.end)``.
    ``floor`` is in seconds, as in their time base. The window is midpoint ± duration/2.
    """
    if duration <= 0:
        raise ValueError("transition duration must be positive")
    midpoint = max(math.floor((float(left_end) + float(right_start)) / 2), float(left_end))
    half = float(duration) / 2
    return Fraction(midpoint) - Fraction(half), Fraction(midpoint), Fraction(midpoint) + Fraction(half)


def layers_from_timeline(timeline: Timeline) -> list[Layer]:
    """Spine is one SEQUENTIAL layer. Each connected lane is a PARALLEL layer."""
    spine = Layer(LayerMode.SEQUENTIAL, "spine")
    for item in timeline.spine:
        spine.clips.append(LayerClip(item, _source_range(item)))
    pack_sequential(spine.clips, timeline.frame)
    lanes: dict[int, Layer] = {}
    for item in timeline.connected:
        layer = lanes.setdefault(item.lane, Layer(LayerMode.PARALLEL, f"lane {item.lane}"))
        layer.clips.append(LayerClip(item, _source_range(item)))
    parallel = [lanes[lane] for lane in sorted(lanes)]
    for layer in parallel:
        ordered = sorted(layer.clips, key=lambda clip: (clip.item.offset, clip.item.name))
        for index, clip in enumerate(ordered):
            for other in ordered[index + 1 :]:
                if overlaps(clip.item, other.item, timeline.frame):
                    raise ValueError(
                        f"parallel layer {layer.name} overlaps {clip.item.name!r} and {other.item.name!r}"
                    )
        layer.clips = ordered
    return [spine, *parallel]


def mark_dissolves(layer: Layer, sections: set[str], duration: Fraction, frame: Fraction) -> None:
    """Attach a dissolve where the outgoing clip's section is listed and both sides can hold it."""
    if layer.mode is not LayerMode.SEQUENTIAL or duration <= 0:
        return
    clips = layer.clips
    for index, clip in enumerate(clips[:-1]):
        nxt = clips[index + 1]
        if clip.item.section not in sections:
            continue
        if clip.item.kind == "gap" or nxt.item.kind == "gap":
            continue
        if clip.item.duration <= duration or nxt.item.duration <= duration:
            continue
        start, _mid, end = transition_window(clip.item.end, nxt.item.offset, duration)
        if end - start > clip.item.duration or end - start > nxt.item.duration:
            continue
        # Keep the window on the frame grid so the FCPXML times match the spine.
        clip.transition = "dissolve"
        clip.transition_duration = Fraction(round(float(duration) / float(frame))) * frame


def detect_silences(
    samples,
    sample_rate: int,
    *,
    threshold: float = SILENCE_THRESHOLD,
    hop: int = SILENCE_HOP,
    min_seconds: float = SILENCE_MIN_SECONDS,
) -> list[tuple[float, float]]:
    """RMS silence runs, matching ``source.silences`` (mono channel, hop, min run)."""
    if sample_rate <= 0 or hop <= 0:
        raise ValueError("sample rate and hop must be positive")
    count = len(samples)
    if count == 0:
        return []
    min_samples = int(min_seconds * sample_rate)
    found: list[tuple[float, float]] = []
    run_start: int | None = None
    run = 0
    for index in range(0, count, hop):
        stop = min(index + hop, count)
        energy = 0.0
        for sample in samples[index:stop]:
            energy += float(sample) * float(sample)
        rms = math.sqrt(energy / (stop - index))
        if rms < threshold:
            run += hop
            if run_start is None:
                run_start = index
        else:
            if run_start is not None and run >= min_samples:
                found.append((run_start / sample_rate, index / sample_rate))
            run_start = None
            run = 0
    if run_start is not None and run >= min_samples:
        found.append((run_start / sample_rate, count / sample_rate))
    return found


def _range_hits(start: Fraction, end: Fraction, source: SourceRange) -> bool:
    return (source.start <= start <= source.end) or (source.start <= end <= source.end)


def remove_silences(
    source: SourceRange,
    silences: list[tuple[Fraction, Fraction]],
    *,
    padding: Fraction = Fraction(1, 2),
) -> list[SourceRange]:
    """Split a source range the way ``AudioClip.removeSilences`` does.

    A silence strictly inside the range keeps ``padding`` after the speech
    and the next piece starts at the silence end. A leading silence moves
    the in-point to the silence end. A trailing silence keeps ``padding``
    then ends.
    """
    pieces: list[SourceRange] = [SourceRange(source.start, source.end)]
    for start, end in sorted(silences, key=lambda span: span[0]):
        current = pieces[-1]
        if not _range_hits(start, end, current):
            continue
        tail = min(start + padding, end)
        if start > current.start and end < current.end:
            out_point = current.end
            current.end = tail
            pieces.append(SourceRange(end, out_point))
        elif start <= current.start:
            current.start = min(end, current.end)
        elif end >= current.end:
            current.end = tail
    return [piece for piece in pieces if piece.end > piece.start]


def detector_comparison() -> dict[str, float]:
    """Numeric gap between the RMS detector and ``ffmpeg_silences`` defaults."""
    ffmpeg_amplitude = 10 ** (FFMPEG_NOISE_DB / 20)
    return {
        "rms_threshold": SILENCE_THRESHOLD,
        "ffmpeg_noise_db": FFMPEG_NOISE_DB,
        "ffmpeg_amplitude": ffmpeg_amplitude,
        "threshold_delta": SILENCE_THRESHOLD - ffmpeg_amplitude,
        "rms_min_seconds": SILENCE_MIN_SECONDS,
        "ffmpeg_min_seconds": FFMPEG_MIN_SECONDS,
        "padding_seconds": SILENCE_PADDING,
    }


def audio_ramp_db(
    duration: Fraction,
    fade_in: Fraction,
    fade_out: Fraction,
    *,
    bed_db: float,
    floor_db: float,
) -> list[VolumeKey]:
    """Two-point fade in and fade out, as ``updateAudioRampingKeyframes``.

    Their frames are linear gain ``0.001 → 1`` with ``log-linear`` easing.
    These keys are the same shape in decibels (``floor_db`` → ``bed_db``),
    which ``render._volume`` writes as ``adjust-volume`` keyframes. The
    ``easeIn`` / ``easeOut`` interp is the FCPXML stand-in for log-linear.
    """
    fade_in = min(fade_in, duration)
    fade_out = min(fade_out, max(Fraction(0), duration - fade_in))
    if fade_in == 0 and fade_out == 0:
        return []
    keys: list[VolumeKey] = []
    if fade_in > 0:
        keys.append(VolumeKey(Fraction(0), floor_db, "easeIn"))
        keys.append(VolumeKey(fade_in, bed_db, "linear"))
    if fade_out > 0:
        keys.append(VolumeKey(duration - fade_out, bed_db, "easeOut"))
        keys.append(VolumeKey(duration, floor_db, "linear"))
    return keys


def _source_range(item: Item) -> SourceRange:
    if item.kind == "clip":
        return SourceRange(item.start, item.start + item.duration)
    return SourceRange(Fraction(0), item.duration)
