"""Source time to timeline time for spine items, connected items, and compounds.

FCPXML stores ``offset`` on the parent timeline, ``start`` as the in point in
the asset (or in a compound's own sequence), and ``duration`` as the length
on the parent timeline. A ``conform-rate`` with ``scaleEnabled="1"`` plays
every source frame as one sequence frame, so a second of media is not a
second of timeline. A ``timeMap`` is the explicit retiming and wins over
that constant scale. A ``ref-clip`` shows a window of a ``<media>`` sequence;
inner clips are mapped through that window, including a nested compound.

Audio slip (``audioStart`` / ``audioDuration``) is the map used for silence
and words. The picture's ``start`` is the map used when a cut rewrites the
element. With no slip and no time map, the two maps are the same.

A connected item's ``offset`` is on its parent's local clock, which begins at
the parent's ``start``. A ``timeMap`` point's ``time`` is on the same local
clock and its ``value`` is asset time. Asset time begins at the asset's own
``start`` (camera timecode), so ``AudibleSpan`` pieces subtract it and carry
seconds into the file, which is what ffmpeg seeks.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
from urllib.parse import unquote, urlparse

from .fcpxml import CLIP_TAGS, Document, local
from .timeutil import parse_time

#: Final Cut writes these names for NTSC rates. The value is the real rate.
_NTSC = {
    "23.98": Fraction(24000, 1001),
    "23.976": Fraction(24000, 1001),
    "29.97": Fraction(30000, 1001),
    "59.94": Fraction(60000, 1001),
}


@dataclass(frozen=True)
class MapPiece:
    """A straight map. Timeline always runs forward. Media may run backward."""

    timeline_start: Fraction
    timeline_end: Fraction
    media_start: Fraction
    media_end: Fraction

    def media_at(self, timeline: Fraction) -> Fraction:
        span = self.timeline_end - self.timeline_start
        if span == 0:
            return self.media_start
        t = (Fraction(timeline) - self.timeline_start) / span
        return self.media_start + t * (self.media_end - self.media_start)

    def timeline_at(self, media: Fraction) -> Fraction:
        span = self.media_end - self.media_start
        if span == 0:
            return self.timeline_start
        t = (Fraction(media) - self.media_start) / span
        return self.timeline_start + t * (self.timeline_end - self.timeline_start)

    def contains_timeline(self, timeline: Fraction) -> bool:
        return self.timeline_start <= timeline <= self.timeline_end

    def contains_media(self, media: Fraction) -> bool:
        lo = min(self.media_start, self.media_end)
        hi = max(self.media_start, self.media_end)
        return lo <= media <= hi


@dataclass(frozen=True)
class AudibleSpan:
    """One heard region. Piece timelines are the sequence clock; media is seconds into the file."""

    sequence: str
    clip_id: str
    clip_name: str
    connected: bool
    kind: str
    src: str | None
    path: Path | None
    reachable: bool
    pieces: tuple[MapPiece, ...]
    error: str | None = None
    role: str | None = None

    def media_bounds(self) -> tuple[Fraction, Fraction] | None:
        if not self.pieces:
            return None
        lo = min(min(piece.media_start, piece.media_end) for piece in self.pieces)
        hi = max(max(piece.media_start, piece.media_end) for piece in self.pieces)
        if hi <= lo:
            return None
        return lo, hi

    def timeline_bounds(self) -> tuple[Fraction, Fraction] | None:
        if not self.pieces:
            return None
        return (
            min(piece.timeline_start for piece in self.pieces),
            max(piece.timeline_end for piece in self.pieces),
        )


def sequence_fps(frame: Fraction) -> Fraction:
    frame = Fraction(frame)
    if frame <= 0:
        return Fraction(24)
    return Fraction(1) / frame


def path_from_file_url(src: str | None) -> Path | None:
    """A local ``file://`` path. Anything else is not opened."""
    if not src or not src.startswith("file:"):
        return None
    parsed = urlparse(src)
    if parsed.scheme != "file":
        return None
    raw = unquote(parsed.path or "")
    if not raw:
        return None
    return Path(raw)


def parse_frame_rate(value: str | None) -> Fraction | None:
    if not value:
        return None
    text = value.strip()
    if text in _NTSC:
        return _NTSC[text]
    if "/" in text:
        num, den = text.split("/", 1)
        if num.lstrip("-").isdigit() and den.lstrip("-").isdigit() and int(den) != 0:
            rate = Fraction(int(num), int(den))
            return rate if rate > 0 else None
    try:
        rate = Fraction(text)
    except (ValueError, ZeroDivisionError):
        return None
    return rate if rate > 0 else None


def video_pieces(elem: ET.Element, timeline_start: Fraction, fps: Fraction) -> list[MapPiece]:
    """Picture map. ``timeline_start`` is where this element begins in the current clock."""
    mapped = _time_map_pieces(elem, timeline_start)
    if mapped:
        return mapped
    duration = parse_time(elem.get("duration"), Fraction(0))
    if duration <= 0:
        return []
    media_start = parse_time(elem.get("start"), Fraction(0))
    media_len = _source_length(elem, duration, fps)
    return [
        MapPiece(
            timeline_start,
            timeline_start + duration,
            media_start,
            media_start + media_len,
        )
    ]


def audio_pieces(elem: ET.Element, timeline_start: Fraction, fps: Fraction) -> list[MapPiece]:
    """What is heard. A time map retimes the whole clip. Otherwise audio may slip."""
    mapped = _time_map_pieces(elem, timeline_start)
    if mapped:
        return mapped
    duration = parse_time(elem.get("duration"), Fraction(0))
    if duration <= 0:
        return []
    if elem.get("audioStart") is not None:
        media_start = parse_time(elem.get("audioStart"), Fraction(0))
        if elem.get("audioDuration") is not None:
            media_len = parse_time(elem.get("audioDuration"), Fraction(0))
        else:
            media_len = _source_length(elem, duration, fps)
        if media_len < 0:
            media_len = Fraction(0)
        return [
            MapPiece(
                timeline_start,
                timeline_start + duration,
                media_start,
                media_start + media_len,
            )
        ]
    return video_pieces(elem, timeline_start, fps)


def has_time_map(elem: ET.Element | None) -> bool:
    if elem is None:
        return False
    return _child(elem, "timeMap") is not None


def kept_media(
    elem: ET.Element,
    local_start: Fraction,
    local_end: Fraction,
    fps: Fraction,
    *,
    audio: bool,
) -> tuple[Fraction, Fraction]:
    """Media times at the two ends of a kept timeline-local range."""
    pieces = audio_pieces(elem, Fraction(0), fps) if audio else video_pieces(elem, Fraction(0), fps)
    start = media_at(pieces, local_start)
    end = media_at(pieces, local_end)
    if start is None or end is None:
        start = local_start
        end = local_end
    return start, end


def local_window(
    elem: ET.Element, local_start: Fraction, local_end: Fraction, fps: Fraction
) -> tuple[Fraction, Fraction]:
    """A kept timeline-local range on the element's own clock.

    That clock is what ``start``, inner markers, and connected offsets use.
    With a time map it is the map's ``time`` axis, so the map itself is left
    as it is and only ``start`` moves.
    """
    if has_time_map(elem):
        origin = parse_time(elem.get("start"), Fraction(0))
        return origin + local_start, origin + local_end
    return kept_media(elem, local_start, local_end, fps, audio=False)


def anchor_time(parent: ET.Element, parent_start: Fraction, offset: Fraction, fps: Fraction) -> Fraction:
    """Where a connected item begins, given its ``offset`` on the parent's local clock."""
    if local(parent.tag) == "spine":
        return parent_start + offset
    delta = offset - parse_time(parent.get("start"), Fraction(0))
    if has_time_map(parent):
        return parent_start + delta
    return parent_start + delta * _conform_scale(parent, fps)


def media_at(pieces: list[MapPiece], timeline: Fraction) -> Fraction | None:
    """Media time at a timeline instant. The last piece covers its end point."""
    if not pieces:
        return None
    for piece in pieces:
        if piece.timeline_start <= timeline < piece.timeline_end:
            return piece.media_at(timeline)
    last = pieces[-1]
    if timeline == last.timeline_end:
        return last.media_end
    if timeline < pieces[0].timeline_start:
        return pieces[0].media_start
    return last.media_end


def timeline_intervals(
    pieces: tuple[MapPiece, ...] | list[MapPiece],
    media_start: Fraction,
    media_end: Fraction,
) -> list[tuple[Fraction, Fraction]]:
    """Where a media interval is heard. Reversed pieces still come out forward."""
    if media_end < media_start:
        media_start, media_end = media_end, media_start
    found: list[tuple[Fraction, Fraction]] = []
    for piece in pieces:
        lo = min(piece.media_start, piece.media_end)
        hi = max(piece.media_start, piece.media_end)
        left = max(lo, media_start)
        right = min(hi, media_end)
        if right <= left:
            continue
        t0 = piece.timeline_at(left)
        t1 = piece.timeline_at(right)
        start, end = (t0, t1) if t0 <= t1 else (t1, t0)
        if end > start:
            found.append((start, end))
    found.sort()
    return _merge(found, Fraction(0))


def project_span(inner: MapPiece, window: MapPiece) -> MapPiece | None:
    """Move a piece whose timeline is ``window``'s media clock onto ``window``'s timeline.

    Used to push a clip inside a compound out to the parent sequence. The
    inner timeline must be in the same units as ``window.media_*``.
    """
    win_lo = min(window.media_start, window.media_end)
    win_hi = max(window.media_start, window.media_end)
    seg_lo = min(inner.timeline_start, inner.timeline_end)
    seg_hi = max(inner.timeline_start, inner.timeline_end)
    left = max(seg_lo, win_lo)
    right = min(seg_hi, win_hi)
    if right <= left:
        return None
    asset_left = inner.media_at(left)
    asset_right = inner.media_at(right)
    outer_left = window.timeline_at(left)
    outer_right = window.timeline_at(right)
    if outer_right < outer_left:
        outer_left, outer_right = outer_right, outer_left
        asset_left, asset_right = asset_right, asset_left
    if outer_right <= outer_left:
        return None
    return MapPiece(outer_left, outer_right, asset_left, asset_right)


@dataclass(frozen=True)
class _Context:
    document: Document
    media_index: dict[str, tuple[ET.Element, Fraction]]
    has_audio: dict[str, str | None]
    sequence: str


def audible_spans(document: Document, sequences) -> list[AudibleSpan]:
    """Every asset a sequence can hear, in that sequence's timeline.

    Spine items, their audio components, connected items (including those on
    a gap), secondary storylines, and compounds are walked. ``enabled="0"``
    items are not heard. Connected items and storylines carry
    ``connected=True``; only the rest can become a mechanical trim.
    """
    index = _media_index(document)
    has_audio = _asset_audio_flags(document)
    found: list[AudibleSpan] = []
    for sequence in sequences:
        context = _Context(document, index, has_audio, sequence.name)
        fps = sequence_fps(sequence.frame_duration)
        for clip in sequence.spine:
            if clip.element is None:
                continue
            found.extend(
                _walk(
                    context,
                    clip.element,
                    start=clip.timeline_start,
                    windows=None,
                    fps=fps,
                    clip_id=clip.id,
                    name=clip.name,
                    mechanical=True,
                    role=None,
                    own_ids=True,
                )
            )
    return found


def _walk(
    context: _Context,
    elem: ET.Element,
    *,
    start: Fraction,
    windows: list[MapPiece] | None,
    fps: Fraction,
    clip_id: str,
    name: str,
    mechanical: bool,
    role: str | None,
    own_ids: bool,
) -> list[AudibleSpan]:
    """``start`` is where ``elem`` begins on the current clock.

    ``windows`` is None on a project sequence. Inside a compound it maps the
    compound's own clock onto the project sequence.
    """
    if elem.get("enabled") == "0":
        return []
    kind = local(elem.tag)
    label = elem.get("name") or name
    role = elem.get("audioRole") or (elem.get("role") if kind == "audio" else None) or role
    ref = elem.get("ref")
    found: list[AudibleSpan] = []
    if kind == "mc-clip":
        found.append(_error_span(context, clip_id, label, kind, mechanical, role, "multicam audio is not read"))
    elif kind == "ref-clip" and ref:
        media = context.media_index.get(ref)
        if media is None:
            found.append(
                _error_span(
                    context, clip_id, label, kind, mechanical, role, "compound clip has no media resource in this XML"
                )
            )
        else:
            inner_windows = _project(audio_pieces(elem, start, fps), windows)
            seq_el, inner_fps = media
            spine = _child(seq_el, "spine")
            if inner_windows and spine is not None:
                for child in spine:
                    if local(child.tag) not in CLIP_TAGS:
                        continue
                    found.extend(
                        _walk(
                            context,
                            child,
                            start=parse_time(child.get("offset"), Fraction(0)),
                            windows=inner_windows,
                            fps=inner_fps,
                            clip_id=clip_id,
                            name=label,
                            mechanical=mechanical,
                            role=role,
                            own_ids=False,
                        )
                    )
    elif kind != "gap" and ref and ref in context.document.assets:
        heard = _project(audio_pieces(elem, start, fps), windows)
        if heard:
            span = _span_from_pieces(context, heard, clip_id, label, kind, not mechanical, ref, role)
            if span is not None:
                found.append(span)

    index = 0
    for child in elem:
        tag = local(child.tag)
        if tag in CLIP_TAGS:
            component = _component(kind, _lane(child.get("lane")), tag)
            child_id = clip_id if component or not own_ids else f"{clip_id}k{index}"
            index += 1
            found.extend(
                _walk(
                    context,
                    child,
                    start=anchor_time(elem, start, parse_time(child.get("offset"), Fraction(0)), fps),
                    windows=windows,
                    fps=fps,
                    clip_id=child_id,
                    name=label,
                    mechanical=mechanical and component,
                    role=role if component else None,
                    own_ids=own_ids and not component,
                )
            )
        elif tag == "spine":
            if child.get("enabled") == "0":
                continue
            story = anchor_time(elem, start, parse_time(child.get("offset"), Fraction(0)), fps)
            items = [item for item in child if local(item.tag) in CLIP_TAGS]
            for position, item in enumerate(items):
                found.extend(
                    _walk(
                        context,
                        item,
                        start=story + parse_time(item.get("offset"), Fraction(0)),
                        windows=windows,
                        fps=fps,
                        clip_id=f"{clip_id}y{position}" if own_ids else clip_id,
                        name=label,
                        mechanical=False,
                        role=None,
                        own_ids=own_ids,
                    )
                )
    return found


def _project(pieces: list[MapPiece], windows: list[MapPiece] | None) -> list[MapPiece]:
    if windows is None:
        return pieces
    projected: list[MapPiece] = []
    for piece in pieces:
        for window in windows:
            hit = project_span(piece, window)
            if hit is not None:
                projected.append(hit)
    return projected


def _error_span(context: _Context, clip_id, clip_name, kind, mechanical, role, error) -> AudibleSpan:
    return AudibleSpan(
        sequence=context.sequence,
        clip_id=clip_id,
        clip_name=clip_name,
        connected=not mechanical,
        kind=kind,
        src=None,
        path=None,
        reachable=False,
        pieces=(),
        error=error,
        role=role,
    )


def _span_from_pieces(context: _Context, pieces, clip_id, clip_name, kind, connected, ref, role) -> AudibleSpan | None:
    if context.has_audio.get(ref) == "0":
        return None
    asset = context.document.assets.get(ref)
    src = asset.src if asset is not None else None
    origin = asset.start if asset is not None else Fraction(0)
    path = path_from_file_url(src)
    reachable = bool(path is not None and path.is_file())
    error = None
    if src and path is None:
        error = "media src is not a local file URL"
    elif path is not None and not reachable:
        error = "media file is not on this machine"
    in_file = tuple(
        MapPiece(piece.timeline_start, piece.timeline_end, piece.media_start - origin, piece.media_end - origin)
        for piece in pieces
    )
    return AudibleSpan(
        sequence=context.sequence,
        clip_id=clip_id,
        clip_name=clip_name,
        connected=connected,
        kind=kind,
        src=src,
        path=path,
        reachable=reachable,
        pieces=in_file,
        error=error,
        role=role,
    )


def _asset_audio_flags(document: Document) -> dict[str, str | None]:
    """Raw ``hasAudio`` per asset. Only an explicit ``0`` means silent."""
    found: dict[str, str | None] = {}
    for elem in document.tree.getroot().iter():
        if local(elem.tag) == "asset" and elem.get("id"):
            found[elem.get("id") or ""] = elem.get("hasAudio")
    return found


def _media_index(document: Document) -> dict[str, tuple[ET.Element, Fraction]]:
    root = document.tree.getroot()
    found: dict[str, tuple[ET.Element, Fraction]] = {}
    for elem in root.iter():
        if local(elem.tag) != "media" or not elem.get("id"):
            continue
        seq = _child(elem, "sequence")
        if seq is None:
            continue
        frame = Fraction(1, 24)
        format_id = seq.get("format")
        if format_id and format_id in document.formats:
            frame = document.formats[format_id].frame_duration
        found[elem.get("id") or ""] = (seq, sequence_fps(frame))
    return found


def _component(parent_kind: str, lane: int | None, child_kind: str) -> bool:
    """A nested ``<audio>`` or ``<video>`` with no lane is the clip, not a storyline."""
    if lane is not None:
        return False
    if parent_kind not in {"clip", "sync-clip"}:
        return False
    return child_kind in {"audio", "video", "asset-clip", "ref-clip"}


def _source_length(elem: ET.Element, timeline_duration: Fraction, fps: Fraction) -> Fraction:
    scale = _conform_scale(elem, fps)
    if scale == 0:
        return timeline_duration
    return timeline_duration / scale


def _conform_scale(elem: ET.Element, fps: Fraction) -> Fraction:
    """Timeline seconds per media second. 1 when the clip is not time-conformed."""
    node = _child(elem, "conform-rate")
    if node is None or node.get("scaleEnabled", "0") != "1":
        return Fraction(1)
    src = parse_frame_rate(node.get("srcFrameRate"))
    if src is None or fps == 0:
        return Fraction(1)
    return src / fps


def _time_map_pieces(elem: ET.Element, timeline_start: Fraction) -> list[MapPiece] | None:
    """The part of the map this item plays, from its ``start`` for ``duration``."""
    node = _child(elem, "timeMap")
    if node is None:
        return None
    points: list[tuple[Fraction, Fraction]] = []
    for child in node:
        if local(child.tag) != "timept":
            continue
        points.append(
            (
                parse_time(child.get("time"), Fraction(0)),
                parse_time(child.get("value"), Fraction(0)),
            )
        )
    if len(points) < 2:
        return None
    points.sort()
    origin = parse_time(elem.get("start"), Fraction(0))
    duration = parse_time(elem.get("duration"), Fraction(0))
    limit = timeline_start + duration if duration > 0 else None
    pieces: list[MapPiece] = []
    for (t0, v0), (t1, v1) in zip(points, points[1:]):
        if t1 == t0:
            continue
        whole = MapPiece(timeline_start + t0 - origin, timeline_start + t1 - origin, v0, v1)
        left = max(whole.timeline_start, timeline_start)
        right = whole.timeline_end if limit is None else min(whole.timeline_end, limit)
        if right > left:
            pieces.append(MapPiece(left, right, whole.media_at(left), whole.media_at(right)))
    return pieces or None


def _merge(
    ranges: list[tuple[Fraction, Fraction]], gap: Fraction
) -> list[tuple[Fraction, Fraction]]:
    merged: list[tuple[Fraction, Fraction]] = []
    for start, end in ranges:
        if end <= start:
            continue
        if merged and start <= merged[-1][1] + gap:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def _child(elem: ET.Element, name: str) -> ET.Element | None:
    for child in elem:
        if local(child.tag) == name:
            return child
    return None


def _lane(value: str | None) -> int | None:
    if value and value.lstrip("-").isdigit():
        return int(value)
    return None
