"""Internal edit plan: the timeline conductor judges before it writes FCPXML.

Applied cuts and assembled sequences both become an :class:`EditPlan`. The
linter and the critic read that plan. Serialising FCPXML happens only after
the gate accepts it.

Ideas: Cardboard's "no linter for video", CutClaw's reviewer gate, EditDuet's
editor/critic pair, VlogReward's plan-not-render rubric. Reimplemented from
public descriptions. No code copied. See ``docs/CREDITS.md``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from fractions import Fraction
from urllib.parse import unquote, urlparse

from .fcpxml import Clip, Document, Sequence, local
from .timeutil import seconds

SPINE = "spine"
CUTAWAY = "cutaway"
MUSIC = "music"
TITLE = "title"
SUBTITLE = "subtitle"
GAP = "gap"

_TITLE_ROLES = ("title", "subtitle", "caption", "lower")
_MUSIC_ROLES = ("music", "bed", "score", "song")


@dataclass
class PlanClip:
    """One placed range. Times are sequence seconds as exact fractions."""

    id: str
    role: str
    name: str
    timeline_start: Fraction
    timeline_end: Fraction
    source_start: Fraction | None = None
    source_end: Fraction | None = None
    asset_id: str | None = None
    asset_src: str | None = None
    text: str = ""
    lane: int | None = None
    has_audio: bool = False
    fade_in: Fraction = Fraction(0)
    fade_out: Fraction = Fraction(0)
    #: Title anchor in frame pixels, origin at the centre. None means centred.
    position: tuple[float, float] | None = None
    intended_gap: bool = False
    element: object | None = field(default=None, repr=False, compare=False)

    @property
    def duration(self) -> Fraction:
        return self.timeline_end - self.timeline_start

    def to_summary(self) -> dict:
        row = {
            "id": self.id,
            "role": self.role,
            "name": self.name,
            "timeline_start": _num(self.timeline_start),
            "timeline_end": _num(self.timeline_end),
            "duration": _num(self.duration),
            "text": self.text,
        }
        if self.asset_id:
            row["asset_id"] = self.asset_id
            row["source_start"] = _num(self.source_start) if self.source_start is not None else None
            row["source_end"] = _num(self.source_end) if self.source_end is not None else None
        if self.role == MUSIC:
            row["fade_in"] = _num(self.fade_in)
            row["fade_out"] = _num(self.fade_out)
        return row


@dataclass
class PlanWord:
    text: str
    start: Fraction
    end: Fraction
    sequence: str = ""

    def to_summary(self) -> dict:
        return {"text": self.text, "start": _num(self.start), "end": _num(self.end)}


@dataclass
class PlanCut:
    """A cut the critic may veto. ``id`` is the candidate id when one exists."""

    id: str
    start: Fraction
    end: Fraction
    reason: str = ""

    def to_summary(self) -> dict:
        return {
            "id": self.id,
            "start": _num(self.start),
            "end": _num(self.end),
            "reason": self.reason,
        }


@dataclass
class EditPlan:
    name: str
    frame_duration: Fraction
    width: int
    height: int
    duration: Fraction
    clips: list[PlanClip] = field(default_factory=list)
    words: list[PlanWord] = field(default_factory=list)
    cuts: list[PlanCut] = field(default_factory=list)
    #: Moments that must not fall inside a word. Empty means "every spine boundary".
    cut_edges: list[Fraction] = field(default_factory=list)
    #: When set, a music bed is required to cover the picture.
    expects_music: bool = False

    def spine(self) -> list[PlanClip]:
        return [clip for clip in self.clips if clip.role == SPINE]

    def cutaways(self) -> list[PlanClip]:
        return [clip for clip in self.clips if clip.role == CUTAWAY]

    def music(self) -> list[PlanClip]:
        return [clip for clip in self.clips if clip.role == MUSIC]

    def titles(self) -> list[PlanClip]:
        return [clip for clip in self.clips if clip.role in {TITLE, SUBTITLE}]

    def summary(self) -> dict:
        """Compact JSON for the critic. No pixels, no render."""
        sentences = [clip.text.strip() for clip in self.spine() if clip.text.strip()]
        return {
            "name": self.name,
            "frame_duration": _num(self.frame_duration),
            "duration": _num(self.duration),
            "width": self.width,
            "height": self.height,
            "clips": [clip.to_summary() for clip in self.clips if clip.role != GAP],
            "kept_sentences": sentences,
            "cuts": [cut.to_summary() for cut in self.cuts],
            "words": [word.to_summary() for word in self.words[:80]],
            "word_count": len(self.words),
        }


def plan_from_document(
    document: Document,
    *,
    sequence: Sequence | None = None,
    words: list | None = None,
    cuts: list | None = None,
    expects_music: bool = False,
) -> EditPlan:
    """Read one sequence into a plan. The tree is not modified."""
    chosen = sequence or (document.sequences[0] if document.sequences else None)
    if chosen is None:
        raise ValueError("the document has no sequence")
    width = chosen.width or 1920
    height = chosen.height or 1080
    clips: list[PlanClip] = []
    for clip in chosen.spine:
        clips.append(_clip(document, clip, spine=True))
        for child in clip.connected_clips:
            clips.append(_clip(document, child, spine=False))
    duration = chosen.duration
    if duration is None:
        ends = [item.timeline_end for item in clips] or [Fraction(0)]
        duration = max(ends)
    plan_words = [
        PlanWord(text=word.text, start=word.start, end=word.end, sequence=getattr(word, "sequence", ""))
        for word in (words or [])
        if not getattr(word, "sequence", "") or word.sequence == chosen.name
    ]
    plan_cuts = [
        PlanCut(
            id=str(cut.get("candidate_id") or cut.get("id") or ""),
            start=_fraction(cut.get("start", cut.get("timeline_start", 0))),
            end=_fraction(cut.get("end", cut.get("timeline_end", 0))),
            reason=str(cut.get("reason") or cut.get("action") or ""),
        )
        if isinstance(cut, dict)
        else PlanCut(id=cut.id, start=cut.start, end=cut.end, reason=getattr(cut, "reason", ""))
        for cut in (cuts or [])
    ]
    return EditPlan(
        name=chosen.name,
        frame_duration=chosen.frame_duration,
        width=width,
        height=height,
        duration=duration,
        clips=clips,
        words=plan_words,
        cuts=plan_cuts,
        expects_music=expects_music,
    )


def media_path(src: str | None) -> str | None:
    """File path behind an asset ``src``. ``file://`` URLs are unquoted."""
    if not src:
        return None
    if src.startswith("file:"):
        parsed = urlparse(src)
        path = unquote(parsed.path or "")
        return path or None
    return src


def _clip(document: Document, clip: Clip, *, spine: bool) -> PlanClip:
    asset = document.assets.get(clip.ref or "") if clip.ref else None
    src = asset.src if asset is not None else None
    role = _role(clip, spine=spine, asset_has_audio=asset.has_audio if asset else None)
    start = clip.timeline_start
    text = (clip.name or "").strip()
    return PlanClip(
        id=clip.id or clip.name or role,
        role=role,
        name=clip.name or "",
        timeline_start=start,
        timeline_end=start + clip.duration,
        source_start=clip.start if clip.ref else None,
        source_end=(clip.start + clip.duration) if clip.ref else None,
        asset_id=clip.ref,
        asset_src=src,
        text=text,
        lane=clip.lane,
        has_audio=_has_audio(clip, role, asset.has_audio if asset else None),
        fade_in=_fade(clip, "in"),
        fade_out=_fade(clip, "out"),
        position=_position(clip),
        intended_gap=_intended(clip),
        element=clip.element,
    )


def _role(clip: Clip, *, spine: bool, asset_has_audio: bool | None) -> str:
    kind = (clip.kind or "").lower()
    roles = " ".join(
        part.lower()
        for part in (clip.audio_role, clip.video_role, *clip.roles, clip.name)
        if part
    )
    if kind == "gap":
        return GAP
    if kind in {"title", "caption"} or any(token in roles for token in ("subtitle", "caption")):
        return SUBTITLE if "subtitle" in roles or "caption" in roles else TITLE
    if any(token in roles for token in _TITLE_ROLES) and kind == "title":
        return TITLE
    if "title" in roles and not spine:
        return TITLE
    if kind == "audio" or any(token in roles for token in _MUSIC_ROLES):
        if kind == "audio" or "music" in roles or "bed" in roles or "score" in roles:
            return MUSIC
    if spine:
        return SPINE
    return CUTAWAY


def _has_audio(clip: Clip, role: str, asset_has_audio: bool | None) -> bool:
    if role == GAP:
        return False
    if role == MUSIC:
        return True
    if clip.audio_role:
        return True
    if asset_has_audio is False:
        return False
    if role == SPINE and clip.kind != "gap":
        return asset_has_audio is not False
    return bool(clip.audio_role)


def _fade(clip: Clip, which: str) -> Fraction:
    element = clip.element
    if element is None:
        return Fraction(0)
    attr = "fadeIn" if which == "in" else "fadeOut"
    raw = element.get(attr)
    if raw:
        from .timeutil import parse_time

        return parse_time(raw, Fraction(0))
    for child in element:
        tag = local(child.tag)
        if tag in {f"fade-{which}", f"audio-fade-{which}", "fade"}:
            amount = child.get("duration") or child.get(which)
            if amount:
                from .timeutil import parse_time

                return parse_time(amount, Fraction(0))
    return Fraction(0)


def _position(clip: Clip) -> tuple[float, float] | None:
    element = clip.element
    if element is None:
        return None
    for child in element:
        if local(child.tag) != "adjust-transform":
            continue
        raw = child.get("position")
        if not raw:
            return None
        parts = raw.replace(",", " ").split()
        if len(parts) < 2:
            return None
        try:
            return (float(parts[0]), float(parts[1]))
        except ValueError:
            return None
    return None


def _intended(clip: Clip) -> bool:
    element = clip.element
    if element is None:
        return False
    return (element.get("audioGap") or "").lower() == "intended"


def _fraction(value) -> Fraction:
    if isinstance(value, Fraction):
        return value
    return Fraction(str(value))


def _num(value: Fraction | None) -> float | None:
    if value is None:
        return None
    return round(seconds(value), 6)
