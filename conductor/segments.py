"""Cached transcript segments and one-call Jev picking.

A clip's word timings are split once, at sentence ends and at topic
shifts. A topic shift is a drop in lexical cohesion between neighbouring
sentence windows (the TextTiling idea: compare a left window with a right
window, then keep the deep gaps), plus a long pause. Each segment is at
most ``max_seconds`` (default 45). The index is cached by media hash beside
the other analysis caches.

Picking is one Jev call (``segment_pick``): a numbered list and a query go
in, and matching segment ids come back with scores. Above Jev's option cap
the list is pre-filtered with the B-roll bag-of-words cosine. Dry-run uses
that same ranker and does not call a model.

The cohesion scorer is written from the published description of TextTiling.
No segmentation source was copied.

Clip Fast (a Jev clipping demo) is the usage pattern: one cached index,
then one pick for a hook, a chapter, or a search.
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path

from cutmcp.jev import MAX_OPTIONS

from .assembly.broll import cosine, embed
from .hygiene import snap_span
from .router import Router
from .signals import default_cache_dir
from .style import StyleProfile

_SENTENCE_END = ".!?"
_INDEX_VERSION = 1
DEFAULT_MAX_SECONDS = 45.0
DEFAULT_PAUSE_SECONDS = 0.8
DEFAULT_CHAPTER_THRESHOLD = 0.15
DEFAULT_HOOK_MIN = 3.0
DEFAULT_HOOK_MAX = 8.0
_WINDOW = 2
_KEYWORD_CAP = 6


@dataclass(frozen=True)
class Segment:
    id: str
    source: str
    start: Fraction
    end: Fraction
    text: str
    keywords: tuple[str, ...]
    topic_depth: float = 0.0

    @property
    def duration(self) -> Fraction:
        return self.end - self.start

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "source": self.source,
            "start": _seconds(self.start),
            "end": _seconds(self.end),
            "text": self.text,
            "keywords": list(self.keywords),
            "topic_depth": round(float(self.topic_depth), 4),
        }


@dataclass(frozen=True)
class SegmentSettings:
    max_seconds: float = DEFAULT_MAX_SECONDS
    pause_seconds: float = DEFAULT_PAUSE_SECONDS
    chapter_threshold: float = DEFAULT_CHAPTER_THRESHOLD
    hook_enabled: bool = True
    chapters_enabled: bool = True
    hook_min_seconds: float = DEFAULT_HOOK_MIN
    hook_max_seconds: float = DEFAULT_HOOK_MAX
    hook_query: str = ""
    broll_keywords: bool = False

    def cache_token(self) -> dict:
        return {
            "max_seconds": self.max_seconds,
            "pause_seconds": self.pause_seconds,
        }


def settings_from_profile(profile: StyleProfile | None) -> SegmentSettings:
    block: dict = {}
    if profile is not None:
        found = profile.get("segments", None)
        if isinstance(found, dict):
            block = found
    hook = block.get("hook") if isinstance(block.get("hook"), dict) else {}
    chapters = block.get("chapters") if isinstance(block.get("chapters"), dict) else {}
    return SegmentSettings(
        max_seconds=float(block.get("max_seconds", DEFAULT_MAX_SECONDS)),
        pause_seconds=float(block.get("pause_seconds", DEFAULT_PAUSE_SECONDS)),
        chapter_threshold=float(block.get("chapter_threshold", DEFAULT_CHAPTER_THRESHOLD)),
        hook_enabled=bool(hook.get("enabled", True)),
        chapters_enabled=bool(chapters.get("enabled", True)),
        hook_min_seconds=float(hook.get("min_seconds", DEFAULT_HOOK_MIN)),
        hook_max_seconds=float(hook.get("max_seconds", DEFAULT_HOOK_MAX)),
        hook_query=str(hook.get("query") or ""),
        broll_keywords=bool(block.get("broll_keywords", False)),
    )


def index_words(
    words,
    *,
    source: str,
    media_hash: str,
    cache_dir: str | Path | None = None,
    settings: SegmentSettings | None = None,
) -> tuple[list[Segment], bool]:
    """Segment ``words`` once. A cache hit returns the stored rows."""
    settings = settings or SegmentSettings()
    cache = _cache_file(cache_dir, media_hash)
    cached = _read_cache(cache, media_hash, settings)
    if cached is not None:
        return cached, True
    built = segment_words(words, source=source, settings=settings)
    _write_cache(cache, media_hash, settings, built)
    return built, False


def segment_words(words, *, source: str, settings: SegmentSettings | None = None) -> list[Segment]:
    """Split word timings at sentence ends, cohesion drops, pauses, and the length cap."""
    settings = settings or SegmentSettings()
    ordered = [word for word in words if str(word.text).strip() and word.end > word.start]
    ordered.sort(key=lambda word: (word.start, word.end, word.text))
    if not ordered:
        return []
    sentences = _sentences(ordered, settings.pause_seconds)
    gaps = _gap_scores(sentences)
    split_after = _topic_splits(sentences, gaps, settings.pause_seconds)
    groups = _pack(sentences, split_after, settings.max_seconds)
    segments: list[Segment] = []
    for index, group in enumerate(groups, start=1):
        start, end = snap_span(group[0].start, group[-1].end, ordered)
        if end <= start:
            start, end = group[0].start, group[-1].end
        text = " ".join(str(word.text).strip() for word in group if str(word.text).strip())
        depth = float(group[0].topic_depth) if hasattr(group[0], "topic_depth") else 0.0
        segments.append(
            Segment(
                id=f"{_slug(source)}-s{index:04d}",
                source=source,
                start=start,
                end=end,
                text=text,
                keywords=tuple(_keywords(text)),
                topic_depth=depth,
            )
        )
    return segments


def limit_candidates(query: str, rows: list[dict], cap: int = MAX_OPTIONS) -> tuple[list[dict], bool]:
    """Keep the ``cap`` rows closest to ``query``. Uses the B-roll cosine."""
    if len(rows) <= cap:
        return list(rows), False
    query_vec = embed(query)
    ranked = sorted(
        rows,
        key=lambda row: (
            -cosine(query_vec, embed(_row_text(row))),
            str(row.get("id") or ""),
        ),
    )
    return ranked[:cap], True


def pick(query: str, segments: list[Segment], router: Router, *, brief: str = "") -> dict:
    """One ``segment_pick`` call. Pre-filters when the list exceeds Jev's cap."""
    rows = [_row(segment) for segment in segments]
    kept, prefiltered = limit_candidates(query, rows)
    result = router.pick_segments(query, kept, brief=brief, prefiltered=prefiltered)
    result["prefiltered"] = prefiltered
    return result


def _slug(source: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9]+", "-", source).strip("-")
    return cleaned or "clip"


def keyword_match_text(segments: list[Segment]) -> str:
    """Keywords from the index, for an optional B-roll query expansion."""
    seen: list[str] = []
    for segment in segments:
        for word in segment.keywords:
            if word not in seen:
                seen.append(word)
    return " ".join(seen)


def hook_slice(segment: Segment, words, settings: SegmentSettings) -> Segment | None:
    """A 3–8 s line inside ``segment``, snapped to word edges. None if it cannot fit."""
    low = Fraction(str(settings.hook_min_seconds))
    high = Fraction(str(settings.hook_max_seconds))
    inside = [word for word in words if word.end > segment.start and word.start < segment.end]
    inside.sort(key=lambda word: (word.start, word.end))
    if not inside:
        return None
    if low <= segment.duration <= high:
        start, end = snap_span(segment.start, segment.end, inside)
        return _slice(segment, start, end, inside)
    start_word = inside[0]
    chosen: list = []
    for word in inside:
        if word.end - start_word.start > high and chosen:
            break
        chosen.append(word)
        if word.end - start_word.start >= low:
            break
    if not chosen:
        return None
    start, end = snap_span(chosen[0].start, chosen[-1].end, inside)
    if end - start < low or end - start > high:
        return None
    return _slice(segment, start, end, chosen)


def choose_hook(
    segments: list[Segment],
    words_by_source: dict,
    router: Router,
    settings: SegmentSettings,
    *,
    brief: str = "",
) -> Segment | None:
    """The strongest line in the hook window. Dry-run picks the best lexical match."""
    if not settings.hook_enabled:
        return None
    slices: list[Segment] = []
    for segment in segments:
        words = words_by_source.get(segment.source) or []
        piece = hook_slice(segment, words, settings)
        if piece is not None and piece.end > piece.start:
            slices.append(piece)
    if not slices:
        return None
    query = settings.hook_query or brief or "strongest opening line"
    result = pick(query, slices, router, brief=brief)
    by_id = {segment.id: segment for segment in slices}
    for segment_id in result["ids"]:
        if segment_id in by_id:
            return by_id[segment_id]
    return slices[0]


def chapter_titles(segments: list[Segment], settings: SegmentSettings) -> list[Segment]:
    """Topic boundaries whose cohesion drop is at least the threshold."""
    if not settings.chapters_enabled:
        return []
    return [
        segment
        for segment in segments
        if segment.topic_depth >= settings.chapter_threshold and segment.keywords
    ]


def find_bit(query: str, segments: list[Segment], router: Router, *, brief: str = "") -> list[dict]:
    """Markers a room bot can drop on the segments that match ``query``."""
    result = pick(query, segments, router, brief=brief)
    scores = result["scores"]
    markers = []
    for segment in segments:
        if segment.id not in result["ids"]:
            continue
        markers.append(
            {
                "value": title_from(segment),
                "note": result["reason"],
                "source": segment.source,
                "start": segment.start,
                "end": segment.end,
                "score": scores.get(segment.id, 0.0),
                "segment_id": segment.id,
            }
        )
    return markers


def title_from(segment: Segment) -> str:
    words = list(segment.keywords)[:4] or segment.text.split()[:4]
    title = " ".join(words).strip()
    return title[:48] or "chapter"


def place_hook(timeline, hook: Segment, media) -> None:
    """Put ``hook`` on the spine before the intro and shift everything after it."""
    from .assembly.timeline import Item

    frame = timeline.frame
    duration = _frame_span(hook.end - hook.start, frame)
    if duration <= 0:
        return
    _shift(timeline, duration)
    for segment in getattr(timeline, "music_segments", ()):
        segment.start += duration
        segment.end += duration
    item = Item(
        kind="clip",
        lane=0,
        offset=Fraction(0),
        duration=duration,
        section="intro",
        name=media.name,
        media=media,
        start=_frame_span(hook.start, frame),
        role="dialogue",
        tags={"hook": True, "segment": hook.id, "speech": True, "reason": "cold open before the intro"},
    )
    timeline.spine.insert(0, item)
    if timeline.sections:
        timeline.sections[0].start = Fraction(0)


def mark_chapters(timeline, segments: list[Segment], settings: SegmentSettings) -> int:
    """One chapter marker per topic boundary, titled from the segment keywords."""
    placed = 0
    for segment in chapter_titles(segments, settings):
        if _add_note(timeline, segment, title_from(segment), "topic boundary", chapter=True):
            placed += 1
    return placed


def mark_findings(timeline, markers: list[dict]) -> int:
    """Attach ``find_bit`` markers to the spine item that owns each source range."""
    from .assembly.timeline import Note

    placed = 0
    for marker in markers:
        segment = Segment(
            id=str(marker.get("segment_id") or "find"),
            source=str(marker.get("source") or ""),
            start=Fraction(marker["start"]),
            end=Fraction(marker["end"]),
            text="",
            keywords=(),
        )
        if _add_note(timeline, segment, str(marker["value"]), str(marker.get("note") or ""), chapter=False):
            placed += 1
    del Note
    return placed


@dataclass
class IndexedFootage:
    segments: list[Segment]
    words_by_source: dict
    settings: SegmentSettings
    cache_hits: int = 0


def index_footage(footage, profile: StyleProfile | None, *, cache_dir: str | Path | None = None) -> IndexedFootage:
    """Build or load a segment index for every clip that has word timings."""
    settings = settings_from_profile(profile)
    built: list[Segment] = []
    words_by_source: dict[str, list] = {}
    hits = 0
    for item in footage:
        words = list(getattr(item.signals, "words", ()) or [])
        if not words:
            continue
        source = item.clip.stem
        media_hash = item.clip.blake2b or source
        segments, hit = index_words(
            words,
            source=source,
            media_hash=media_hash,
            cache_dir=cache_dir,
            settings=settings,
        )
        hits += int(hit)
        built.extend(segments)
        words_by_source[source] = words
    return IndexedFootage(built, words_by_source, settings, hits)


def finish_timeline(
    timeline,
    indexed: IndexedFootage,
    router: Router,
    *,
    brief: str = "",
    music_segments=(),
    footage=(),
) -> dict:
    """Prepend the hook and write chapter markers. No-op without segments."""
    summary = {
        "segments": len(indexed.segments),
        "cache_hits": indexed.cache_hits,
        "hook": False,
        "chapters": 0,
        "match_text": keyword_match_text(indexed.segments) if indexed.settings.broll_keywords else "",
    }
    if not indexed.segments:
        return summary
    timeline.music_segments = list(music_segments)
    hook = choose_hook(indexed.segments, indexed.words_by_source, router, indexed.settings, brief=brief)
    if hook is not None:
        media = _media_for(timeline, footage, hook.source)
        if media is not None:
            place_hook(timeline, hook, media)
            summary["hook"] = True
    summary["chapters"] = mark_chapters(timeline, indexed.segments, indexed.settings)
    return summary


def apply_to_assembly(
    timeline,
    footage,
    profile: StyleProfile,
    router: Router,
    *,
    brief: str = "",
    music_segments=(),
    cache_dir: str | Path | None = None,
) -> dict:
    """Index each clip, optionally prepend a hook, and drop chapter markers."""
    indexed = index_footage(footage, profile, cache_dir=cache_dir)
    return finish_timeline(
        timeline,
        indexed,
        router,
        brief=brief,
        music_segments=music_segments,
        footage=footage,
    )


def _media_for(timeline, footage, source: str):
    for item in timeline.spine:
        if item.media is not None and item.media.name == source:
            return item.media
    for item in footage:
        if item.clip.stem != source:
            continue
        from .assembly.timeline import Media

        clip = item.clip
        return Media(
            key=clip.stem,
            kind="video",
            name=clip.stem,
            src=clip.stem,
            uid=clip.uid,
            duration=clip.duration,
            width=clip.width,
            height=clip.height,
            frame_duration=clip.frame_duration,
            has_video=clip.has_video,
            has_audio=clip.has_audio,
        )
    return None


def _add_note(timeline, segment: Segment, value: str, note: str, *, chapter: bool) -> bool:
    from .assembly.timeline import Note

    for item in timeline.spine:
        if item.media is None:
            continue
        if item.name != segment.source and item.media.name != segment.source:
            continue
        if item.start >= segment.end or item.start + item.duration <= segment.start:
            continue
        at = item.offset + max(Fraction(0), segment.start - item.start)
        item.notes.append(Note(at=at, value=value, note=note, chapter=chapter))
        return True
    return False


def _shift(timeline, delta: Fraction) -> None:
    for item in list(timeline.spine) + list(timeline.connected):
        item.offset += delta
        for key in item.volume_keys:
            key.at += delta
        for note in item.notes:
            note.at += delta
    for section in timeline.sections:
        section.start += delta
        section.end += delta


def _slice(segment: Segment, start, end, words) -> Segment | None:
    start, end = snap_span(start, end, words)
    if end <= start:
        return None
    text = " ".join(str(word.text).strip() for word in words if word.start >= start and word.end <= end)
    if not text:
        text = segment.text
    return Segment(
        id=segment.id,
        source=segment.source,
        start=start,
        end=end,
        text=text,
        keywords=segment.keywords or tuple(_keywords(text)),
        topic_depth=segment.topic_depth,
    )


class _Word:
    """A sentence word plus the topic depth of the boundary that opened it."""

    def __init__(self, word, topic_depth: float = 0.0):
        self.start = word.start
        self.end = word.end
        self.text = word.text
        self.topic_depth = topic_depth


def _sentences(words, pause_seconds: float) -> list[list]:
    groups: list[list] = [[_Word(words[0])]]
    pause = Fraction(str(pause_seconds))
    for previous, word in zip(words, words[1:]):
        gap = word.start - previous.end
        ended = str(previous.text).rstrip()[-1:] in _SENTENCE_END
        if ended or gap >= pause:
            groups.append([_Word(word)])
        else:
            groups[-1].append(_Word(word))
    return groups


def _gap_scores(sentences: list[list]) -> list[float]:
    scores: list[float] = []
    for index in range(len(sentences) - 1):
        left = sentences[max(0, index - _WINDOW + 1) : index + 1]
        right = sentences[index + 1 : index + 1 + _WINDOW]
        scores.append(cosine(embed(_flat(left)), embed(_flat(right))))
    return scores


def _topic_splits(sentences: list[list], scores: list[float], pause_seconds: float) -> dict[int, float]:
    """Map a sentence index to the cohesion depth of the gap after it."""
    splits: dict[int, float] = {}
    if not scores:
        return splits
    depths = []
    for index, score in enumerate(scores):
        left_peak = max(scores[max(0, index - 1) : index + 1])
        right_peak = max(scores[index : min(len(scores), index + 2)])
        depths.append(max(0.0, left_peak - score) + max(0.0, right_peak - score))
    mean = sum(depths) / len(depths)
    variance = sum((depth - mean) ** 2 for depth in depths) / len(depths)
    cutoff = mean + 0.5 * math.sqrt(variance)
    pause = Fraction(str(pause_seconds))
    for index, depth in enumerate(depths):
        gap = sentences[index + 1][0].start - sentences[index][-1].end
        if depth > cutoff and depth > 0:
            splits[index] = depth
        elif gap >= pause:
            splits.setdefault(index, depth)
    return splits


def _pack(sentences: list[list], split_after: dict[int, float], max_seconds: float) -> list[list]:
    cap = Fraction(str(max_seconds))
    groups: list[list] = []
    buf: list = []

    def flush() -> None:
        if buf:
            groups.append(list(buf))
            buf.clear()

    for index, sentence in enumerate(sentences):
        depth = split_after.get(index - 1, 0.0) if not buf else 0.0
        pieces = _split_long(sentence, cap, depth if not buf else 0.0)
        for piece_index, piece in enumerate(pieces):
            if buf and piece[-1].end - buf[0].start > cap:
                flush()
                if piece and piece[0].topic_depth == 0:
                    piece[0].topic_depth = 0.0
            if not buf and piece:
                opening = split_after.get(index - 1, 0.0) if index else 0.0
                if piece_index == 0 and opening and piece[0].topic_depth == 0:
                    piece[0].topic_depth = opening
            buf.extend(piece)
            if piece_index < len(pieces) - 1:
                flush()
        if index in split_after:
            flush()
    flush()
    return groups


def _split_long(sentence: list, cap: Fraction, depth: float) -> list[list]:
    pieces: list[list] = []
    buf: list = []
    for word in sentence:
        if buf and word.end - buf[0].start > cap:
            pieces.append(buf)
            buf = []
        wrapped = _Word(word, depth if not buf and not pieces else 0.0)
        if not buf and not pieces:
            wrapped.topic_depth = depth
        buf.append(wrapped)
    if buf:
        pieces.append(buf)
    return pieces


def _flat(sentences: list[list]) -> str:
    return " ".join(str(word.text) for sentence in sentences for word in sentence)


def _keywords(text: str) -> list[str]:
    weights = embed(text)
    ranked = sorted(weights, key=lambda token: (-weights[token], token))
    return [token for token in ranked if len(token) > 2][:_KEYWORD_CAP]


def _row(segment: Segment) -> dict:
    return {
        "id": segment.id,
        "text": segment.text,
        "keywords": list(segment.keywords),
    }


def _row_text(row: dict) -> str:
    keywords = " ".join(str(word) for word in (row.get("keywords") or []))
    return f"{row.get('text') or ''} {keywords}"


def _cache_file(cache_dir: str | Path | None, media_hash: str) -> Path:
    root = Path(cache_dir) if cache_dir is not None else default_cache_dir()
    safe = re.sub(r"[^A-Za-z0-9_-]", "", media_hash) or "clip"
    return root / "segments" / f"{safe}.json"


def _read_cache(path: Path, media_hash: str, settings: SegmentSettings) -> list[Segment] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    if payload.get("version") != _INDEX_VERSION or payload.get("media_hash") != media_hash:
        return None
    if payload.get("settings") != settings.cache_token():
        return None
    rows = payload.get("segments")
    if not isinstance(rows, list):
        return None
    return [_from_row(row) for row in rows]


def _write_cache(path: Path, media_hash: str, settings: SegmentSettings, segments: list[Segment]) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        body = {
            "version": _INDEX_VERSION,
            "media_hash": media_hash,
            "settings": settings.cache_token(),
            "segments": [segment.to_dict() for segment in segments],
        }
        path.write_text(json.dumps(body, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    except OSError:
        return


def _from_row(row: dict) -> Segment:
    return Segment(
        id=str(row["id"]),
        source=str(row["source"]),
        start=Fraction(str(row["start"])),
        end=Fraction(str(row["end"])),
        text=str(row.get("text") or ""),
        keywords=tuple(str(word) for word in (row.get("keywords") or [])),
        topic_depth=float(row.get("topic_depth") or 0.0),
    )


def _seconds(value: Fraction) -> str:
    return format(float(value), ".3f")


def _frame_span(value: Fraction, frame: Fraction) -> Fraction:
    if frame <= 0:
        return value
    steps = int(round(Fraction(value) / frame))
    return steps * frame
