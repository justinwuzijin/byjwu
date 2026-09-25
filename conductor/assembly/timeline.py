"""The assembled timeline, before it is written as FCPXML.

Times are :class:`fractions.Fraction` seconds. ``offset`` is always timeline
time. ``start`` is the item's own local time at its first frame (source time
for media, 0 for gaps, stills and titles). The renderer converts timeline
times to the parent-local coordinates FCPXML wants.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from fractions import Fraction


@dataclass(frozen=True)
class Media:
    key: str
    kind: str
    name: str
    src: str
    uid: str
    duration: Fraction
    width: int | None = None
    height: int | None = None
    frame_duration: Fraction | None = None
    has_video: bool = True
    has_audio: bool = False
    audio_channels: int = 2
    audio_rate: int = 48000


@dataclass
class Note:
    at: Fraction
    value: str
    note: str
    todo: bool = False
    chapter: bool = False


@dataclass
class Keyframes:
    """Parameter animation. Times are seconds from the item's first frame."""

    scale: list[tuple[Fraction, tuple[float, float]]] = field(default_factory=list)
    position: list[tuple[Fraction, tuple[float, float]]] = field(default_factory=list)
    rotation: list[tuple[Fraction, float]] = field(default_factory=list)


@dataclass
class Transform:
    scale: tuple[float, float] | None = None
    position: tuple[float, float] | None = None
    rotation: float | None = None
    keyframes: Keyframes | None = None
    corners: dict[str, tuple[float, float]] | None = None

    def is_identity(self) -> bool:
        return (
            (self.scale is None or self.scale == (1.0, 1.0))
            and (self.position is None or self.position == (0.0, 0.0))
            and not self.rotation
            and not self.corners
            and (
                self.keyframes is None
                or not (self.keyframes.scale or self.keyframes.position or self.keyframes.rotation)
            )
        )


@dataclass(frozen=True)
class TextStyle:
    font: str
    face: str
    size: float
    color: tuple[float, float, float, float]
    alignment: str = "center"
    kerning: float = 0.0
    line_spacing: float = 0.0
    stroke_color: tuple[float, float, float, float] | None = None
    stroke_width: float | None = None
    shadow_color: tuple[float, float, float, float] | None = None
    shadow_distance: float | None = None
    shadow_angle: float | None = None
    shadow_blur: float | None = None


@dataclass
class TitleSpec:
    text: str
    style: TextStyle
    position: tuple[float, float]
    kind: str
    treatment: str = "none"


@dataclass
class VolumeKey:
    at: Fraction
    db: float
    interp: str = "linear"


@dataclass
class Item:
    kind: str
    lane: int
    offset: Fraction
    duration: Fraction
    section: str
    name: str
    media: Media | None = None
    start: Fraction = Fraction(0)
    role: str | None = None
    src_enable: str | None = None
    audio_start: Fraction | None = None
    audio_duration: Fraction | None = None
    volume_db: float | None = None
    volume_keys: list[VolumeKey] = field(default_factory=list)
    transform: Transform | None = None
    blend: float | None = None
    conform: str | None = None
    title: TitleSpec | None = None
    notes: list[Note] = field(default_factory=list)
    tags: dict = field(default_factory=dict)

    @property
    def end(self) -> Fraction:
        return self.offset + self.duration

    def audio_span(self) -> tuple[Fraction, Fraction]:
        """Timeline range the item's audio covers, including split-edit extensions."""
        if self.audio_start is None and self.audio_duration is None:
            return self.offset, self.end
        audio_start = self.audio_start if self.audio_start is not None else self.start
        audio_duration = self.audio_duration if self.audio_duration is not None else self.duration
        begin = self.offset + (audio_start - self.start)
        return begin, begin + audio_duration


@dataclass
class Section:
    kind: str
    index: int
    label: str
    start: Fraction
    end: Fraction
    budget: Fraction
    notes: list[str] = field(default_factory=list)

    @property
    def duration(self) -> Fraction:
        return self.end - self.start


@dataclass
class Timeline:
    name: str
    frame: Fraction
    width: int
    height: int
    spine: list[Item] = field(default_factory=list)
    connected: list[Item] = field(default_factory=list)
    sections: list[Section] = field(default_factory=list)

    @property
    def duration(self) -> Fraction:
        if not self.spine:
            return Fraction(0)
        return self.spine[-1].end

    def section_at(self, t: Fraction) -> Section | None:
        for section in self.sections:
            if section.start <= t < section.end:
                return section
        return self.sections[-1] if self.sections and t >= self.sections[-1].end else None
