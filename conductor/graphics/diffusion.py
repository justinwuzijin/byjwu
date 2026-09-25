"""Timing maths borrowed from Diffusion Studio core.

https://github.com/diffusionstudio/core (@diffusionstudio/core, MPL-2.0).
That package is a browser-only WebCodecs compositor and watermarks output
unless it is licensed. This module copies two pieces of its data model and
nothing else: ``CaptionSource.groupBy`` and the keyframe lerp (their ``yc``).
It is not a dependency, and no frame is rendered through it. Final Cut
expresses the result as titles, keyframes, and generator clips.

``groupBy`` takes exactly one limit. Words are appended to the open group.
A word that would exceed the limit starts a new group, then is appended.
Spoken duration is the sum of each word's ``end - start``, not the wall-clock
span of the group. Empty groups are dropped.

Keyframe sample clamps to the first value before the first frame and to the
last value after the last frame. Between two frames,
``t = (time - start) / (end - start)``, an optional ease is applied, then
the numbers are lerped.
"""

from __future__ import annotations

from collections.abc import Sequence


def group_by(words: Sequence, *, count: int | None = None, duration: float | None = None, length: int | None = None) -> list[list]:
    """One of ``count``, spoken ``duration`` (seconds), or character ``length``."""
    chosen = (count is not None) + (duration is not None) + (length is not None)
    if chosen != 1:
        raise ValueError("group_by takes exactly one of count, duration, length")
    groups: list[list] = [[]]
    for word in words:
        current = groups[-1]
        chars = sum(len(item.text) for item in current)
        spoken = sum(float(item.end - item.start) for item in current)
        overflow = (
            (count is not None and len(current) + 1 > count)
            or (duration is not None and spoken + float(word.end - word.start) > duration)
            or (length is not None and chars + len(word.text) > length)
        )
        if overflow:
            groups.append([])
        groups[-1].append(word)
    return [group for group in groups if group]


def sample_keyframes(frames: Sequence[tuple[float, float]], time: float, easing: str | None = None) -> float:
    """Clamp-and-lerp a ``(time_seconds, value)`` list. ``easing`` is ``smooth`` or None."""
    if not frames:
        raise ValueError("sample_keyframes needs at least one frame")
    ordered = sorted(frames, key=lambda frame: frame[0])
    if time <= ordered[0][0]:
        return ordered[0][1]
    if time >= ordered[-1][0]:
        return ordered[-1][1]
    for (start, left), (end, right) in zip(ordered, ordered[1:]):
        if start <= time <= end:
            span = end - start
            t = 0.0 if span == 0 else (time - start) / span
            if easing == "smooth":
                t = t * t * (3 - 2 * t)
            return left + (right - left) * t
    return ordered[-1][1]
