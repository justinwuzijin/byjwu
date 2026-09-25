"""Everything connected to the storyline, and the markers that explain it.

- subtitles from transcript cues, as Basic Title clips in the profile's SF Pro
  subtitle style; a decision picks which lines get a distortion treatment
- title and end cards on the card gaps, in the title style, with a treatment
- music beds with volume keyframes: fades from the profile, song-change
  transitions, section bed levels, and ducking under every dialogue span
- the abstract rectangle background: plate stills plus floating rectangles,
  drifting and pulsing on the beat, and the A-roll inset over them
- a marker on every placed item with the decision, confidence and rule that
  produced it; low-confidence calls are to-do markers
"""

from __future__ import annotations

import math
import random
import re
from dataclasses import dataclass, replace
from fractions import Fraction
from pathlib import Path

from ..ingest import file_url
from ..style import StyleProfile, hex_rgba
from ..taste import Taste
from .background import ArtSource, Still, render_floater, render_plate
from .layout import Adjustments, MusicSegment, section_note
from .media import Material
from ..router import Router
from .select import Decision, Question, Unit, decide
from .subtitles import Card, apply_break, apply_case, break_choices, cards_from_cues
from .timeline import Item, Keyframes, Media, Note, TextStyle, TitleSpec, Timeline, Transform, VolumeKey

EMPHASIS = re.compile(
    r"!|\b(never|always|insane|crazy|best|worst|huge|literally|actually|biggest|every|nobody|everyone|secret|wild)\b",
    re.IGNORECASE,
)
_INTERP = {"linear": "linear", "easeIn": "easeIn", "easeOut": "easeOut", "easeInOut": "ease", "ease": "ease"}


@dataclass
class Lanes:
    cutaway: int
    title: int
    subtitle: int
    plate: int
    floaters: list[int]
    music: tuple[int, int]


def lanes_for(profile: StyleProfile) -> Lanes:
    floating = int(profile.get("background.motion.floating")) if profile.get("background.enabled") else 0
    title = int(profile.get("typography.title.lane"))
    subtitle = int(profile.get("typography.subtitle.lane"))
    if profile.get("background.placement") == "over" and profile.get("background.enabled"):
        plate = 2
        floaters = [plate + 1 + i for i in range(floating)]
        top = floaters[-1] if floaters else plate
        shift = max(0, top + 1 - min(title, subtitle))
        return Lanes(1, title + shift, subtitle + shift, plate, floaters, (-1, -2))
    plate = min(int(profile.get("background.lane")), -(floating + 1))
    floaters = [plate + 1 + i for i in range(floating)]
    return Lanes(1, title, subtitle, plate, floaters, (plate - 1, plate - 2))


def dress(
    timeline: Timeline,
    *,
    material: Material,
    profile: StyleProfile,
    units: list[Unit],
    decisions: dict[str, Decision],
    segments: list[MusicSegment],
    brief: str,
    taste: Taste,
    router: Router,
    adjustments: Adjustments,
    assets_dir: Path,
    prior: dict[str, Decision] | None = None,
) -> tuple[dict[str, Decision], list[dict], list[str]]:
    """Add connected items and notes in place. Returns dress decisions, receipts, warnings."""
    warnings: list[str] = []
    lanes = lanes_for(profile)
    for item in timeline.connected:
        if item.tags.get("cutaway"):
            item.lane = lanes.cutaway
    by_unit = {unit.id: unit for unit in units}
    cards = _subtitle_cards(timeline, by_unit, profile, adjustments)
    title_cards = [item for item in timeline.spine if item.tags.get("card")]
    items, questions = _dress_questions(timeline, cards, title_cards, by_unit, profile, brief)
    dress_decisions, receipts = decide(
        "dress",
        items,
        questions,
        brief=brief,
        profile=profile,
        taste=taste,
        router=router,
        gates=taste.gates,
        prior=prior,
        signatures={item["id"]: item["signature"] for item in items},
    )
    _place_subtitles(timeline, cards, dress_decisions, profile, lanes)
    _place_cards(timeline, title_cards, dress_decisions, items, profile, lanes)
    _place_music(timeline, segments, profile, lanes)
    if profile.get("background.enabled"):
        warnings.extend(_place_background(timeline, segments, profile, lanes, assets_dir))
    _inset(timeline, profile)
    _notes(timeline, decisions, profile)
    return dress_decisions, receipts, warnings


# ------------------------------------------------------------------ subtitles


def _subtitle_cards(
    timeline: Timeline, by_unit: dict[str, Unit], profile: StyleProfile, adjustments: Adjustments
) -> list[Card]:
    spec = profile.get("typography.subtitle")
    if not spec.get("enabled"):
        return []
    from ..transcript import Cue

    cues: list[Cue] = []
    for item in timeline.spine:
        unit = by_unit.get(item.tags.get("unit", ""))
        if unit is None or not unit.cues:
            continue
        for cue in unit.cues:
            start = max(item.offset, item.offset + (cue.start - item.start))
            end = min(item.end, item.offset + (cue.end - item.start))
            if end > start:
                cues.append(Cue(start, end, cue.text))
    cards = cards_from_cues(
        cues,
        max_chars=int(spec["max_chars_per_line"]),
        max_lines=int(spec["max_lines"]),
        min_seconds=Fraction(str(spec["min_seconds"])),
        max_seconds=Fraction(str(spec["max_seconds"])),
        frame=timeline.frame,
        case=str(spec["case"]),
    )
    if adjustments.subtitle_fill:
        filled: list[Card] = []
        for index, card in enumerate(cards):
            end = card.end
            if index + 1 < len(cards):
                gap = cards[index + 1].start - card.end
                if Fraction(0) < gap <= Fraction(3, 5):
                    end = cards[index + 1].start
            filled.append(Card(card.start, end, card.text, card.lines))
        cards = filled
    return cards


def _dress_questions(
    timeline: Timeline,
    cards: list[Card],
    title_cards: list[Item],
    by_unit: dict[str, Unit],
    profile: StyleProfile,
    brief: str,
) -> tuple[list[dict], list[Question]]:
    items: list[dict] = []
    questions: list[Question] = []
    emphasis = profile.get("typography.subtitle.emphasis")
    treatments = list(emphasis.get("treatments") or [])
    rate = float(emphasis.get("rate") or 0.0)
    if treatments and rate > 0 and cards:
        scored = sorted(
            range(len(cards)),
            key=lambda i: (-_emphasis_score(cards[i].text), i),
        )
        chosen = set(scored[: max(0, int(round(rate * len(cards))))])
        options = {"none": "Plain subtitle."}
        options.update({name: profile.get(f"text_treatments.{name}.description", name) for name in treatments})
        for index, card in enumerate(cards):
            key = f"s{index + 1:04d}"
            picked = index in chosen and _emphasis_score(card.text) > 0
            value = treatments[index % len(treatments)] if picked else "none"
            reason = "punchy line, emphasis word or exclamation" if picked else "plain line"
            items.append(
                {
                    "id": key,
                    "signature": f"subtitle:{card.start}:{card.text}",
                    "kind": "subtitle",
                    "text": card.text,
                    "at_seconds": round(float(card.start), 3),
                    "duration_seconds": round(float(card.duration), 3),
                    "heuristic": {"treat": {"value": value, "confidence": 0.66 if picked else 0.74, "reason": reason}},
                }
            )
            questions.append(
                Question(
                    f"{key}_treat",
                    "typography",
                    options,
                    f"Subtitle {key}. Does this line get a distorted-text treatment? "
                    f"The style emphasises about {rate:.0%} of lines.",
                    lane="creative",
                )
            )
    title_treatments = list(profile.get("typography.title.treatments") or ["none"])
    options_treat = {name: profile.get(f"text_treatments.{name}.description", name) for name in title_treatments}
    for number, gap in enumerate(title_cards, start=1):
        key = f"tc{number:02d}"
        texts = _card_texts(gap, timeline, by_unit, profile, brief)
        text_value = "profile" if "profile" in texts else ("brief" if "brief" in texts else next(iter(texts)))
        items.append(
            {
                "id": key,
                "signature": f"card:{gap.tags['card']}:{number}:{'|'.join(texts.values())}",
                "kind": gap.tags["card"],
                "options": texts,
                "at_seconds": round(float(gap.offset), 3),
                "heuristic": {
                    "text": {"value": text_value, "confidence": 0.7, "reason": f"{text_value} text suits a {gap.tags['card']}"},
                    "treat": {
                        "value": title_treatments[(number - 1) % len(title_treatments)],
                        "confidence": 0.68,
                        "reason": "rotates through typography.title.treatments",
                    },
                },
            }
        )
        questions.append(
            Question(
                f"{key}_text",
                "typography",
                {name: f"“{text}”" for name, text in texts.items()},
                f"Card {key} ({gap.tags['card']}). Which text goes on it?",
                lane="creative",
            )
        )
        questions.append(
            Question(
                f"{key}_treat",
                "typography",
                options_treat,
                f"Card {key}: which title treatment?",
                lane="creative",
            )
        )
    _subtitle_breaks(items, questions, cards, profile)
    return items, questions


def _subtitle_breaks(items: list[dict], questions: list[Question], cards: list[Card], profile: StyleProfile) -> None:
    width = int(profile.get("typography.subtitle.max_chars_per_line"))
    by_id = {item["id"]: item for item in items}
    for index, card in enumerate(cards):
        greedy, options = break_choices(card.text, width)
        if not options:
            continue
        key = f"s{index + 1:04d}"
        item = by_id.get(key)
        if item is None:
            item = {
                "id": key,
                "signature": f"subtitle:{card.start}:{card.text}",
                "kind": "subtitle",
                "text": card.text,
                "at_seconds": round(float(card.start), 3),
                "heuristic": {},
            }
            items.append(item)
        item["heuristic"]["break"] = {
            "value": greedy,
            "confidence": 0.8,
            "reason": "longest first line that fits the character rule",
        }
        questions.append(
            Question(
                f"{key}_break",
                "subtitle_break",
                options,
                f"Subtitle {key}. Where does the first line break? Each option stays within {width} characters.",
                lane="linear",
            )
        )


def _card_texts(gap: Item, timeline: Timeline, by_unit: dict[str, Unit], profile: StyleProfile, brief: str) -> dict[str, str]:
    if gap.tags["card"] == "end_card":
        return {"profile": str(profile.get("structure.end_card.text"))}
    texts: dict[str, str] = {}
    phrase = _brief_phrase(brief)
    if phrase:
        texts["brief"] = phrase
    following = next((i for i in timeline.spine if i.offset >= gap.end and i.tags.get("unit")), None)
    if following is not None:
        texts["clip"] = _humanize(following.name)
        unit = by_unit.get(following.tags["unit"])
        if unit is not None and unit.text:
            texts["line"] = " ".join(unit.text.split()[:5])
    return texts or {"clip": timeline.name}


def _broken(card: Card, decision: Decision | None, spec: dict) -> str:
    if decision is None:
        return card.text
    width = int(spec["max_chars_per_line"])
    lines = apply_break(card.text, str(decision.value), width)
    if not lines or len(lines) > int(spec["max_lines"]) or any(len(line) > width for line in lines):
        return card.text
    return "\n".join(apply_case(line, str(spec["case"])) for line in lines)


def _place_subtitles(timeline: Timeline, cards: list[Card], decisions: dict[str, Decision], profile: StyleProfile, lanes: Lanes) -> None:
    spec = profile.get("typography.subtitle")
    style = _text_style(spec, timeline)
    position = _position(spec, timeline)
    for index, card in enumerate(cards):
        key = f"s{index + 1:04d}"
        decision = decisions.get(f"{key}_treat")
        treatment = str(decision.value) if decision else "none"
        text = _broken(card, decisions.get(f"{key}_break"), spec)
        item = Item(
            kind="title",
            lane=lanes.subtitle,
            offset=card.start,
            duration=card.duration,
            section=(timeline.section_at(card.start).kind if timeline.section_at(card.start) else "talking"),
            name=f"Subtitle {index + 1}",
            title=TitleSpec(text, style, position, "subtitle", treatment),
            tags={"subtitle": True, "decision": key},
        )
        _apply_treatment(item, treatment, profile, timeline)
        if treatment != "none" and decision is not None:
            item.notes.append(
                Note(
                    card.start,
                    f"jevid · subtitle · {treatment}",
                    _join(
                        [
                            f"why={decision.reason or 'emphasis'}",
                            f"decision={decision.label()}",
                            f"rule=typography.subtitle.emphasis rate={spec['emphasis']['rate']}",
                        ]
                    ),
                    todo=decision.review,
                )
            )
        timeline.connected.append(item)


def _place_cards(
    timeline: Timeline,
    gaps: list[Item],
    decisions: dict[str, Decision],
    items: list[dict],
    profile: StyleProfile,
    lanes: Lanes,
) -> None:
    spec = profile.get("typography.title")
    style = _text_style(spec, timeline)
    position = _position(spec, timeline)
    options = {item["id"]: item.get("options") or {} for item in items if item["id"].startswith("tc")}
    for number, gap in enumerate(gaps, start=1):
        key = f"tc{number:02d}"
        text_decision = decisions.get(f"{key}_text")
        treat_decision = decisions.get(f"{key}_treat")
        texts = options.get(key, {})
        text = texts.get(str(text_decision.value)) if text_decision else None
        text = apply_case(text or gap.name, str(spec["case"]))
        treatment = str(treat_decision.value) if treat_decision else "none"
        item = Item(
            kind="title",
            lane=lanes.title,
            offset=gap.offset,
            duration=gap.duration,
            section=gap.section,
            name=f"{gap.name}: {text}"[:60],
            title=TitleSpec(text, style, position, "title", treatment),
            tags={"card_title": key},
        )
        _apply_treatment(item, treatment, profile, timeline)
        timeline.connected.append(item)
        todo = any(d is not None and d.review for d in (text_decision, treat_decision))
        gap.notes.append(
            Note(
                gap.offset,
                f"jevid · {gap.section} · “{text}”",
                _join(
                    [
                        f"text={text_decision.label() if text_decision else 'default'}",
                        f"why={text_decision.reason if text_decision else ''}",
                        f"treatment={treat_decision.label() if treat_decision else 'none'}",
                        f"font={spec['font']} {spec['face']} {float(spec['size']) * timeline.height:.0f}px case={spec['case']}",
                        f"length={gap.tags.get('reason', '')}",
                    ]
                ),
                todo=todo,
            )
        )


# ---------------------------------------------------------------------- music


def _place_music(timeline: Timeline, segments: list[MusicSegment], profile: StyleProfile, lanes: Lanes) -> None:
    dialogue = _dialogue_spans(timeline)
    for segment in segments:
        song = segment.song
        media = Media(
            key=str(song.path),
            kind="audio",
            name=song.name,
            src=song.src,
            uid=song.uid,
            duration=song.duration,
            has_video=False,
            has_audio=True,
            audio_channels=song.channels,
            audio_rate=song.rate,
        )
        keys = music_keys(segment, timeline, dialogue, profile)
        lane = lanes.music[0] if segment.lane == -1 else lanes.music[1]
        item = Item(
            kind="clip",
            lane=lane,
            offset=segment.start,
            duration=segment.end - segment.start,
            section=(timeline.section_at(segment.start).kind if timeline.section_at(segment.start) else "intro"),
            name=song.name,
            media=media,
            start=segment.source_start,
            role=str(profile.get("music.role")),
            volume_keys=keys,
            tags={
                "music": True,
                "fade_in": float(segment.fade_in),
                "fade_out": float(segment.fade_out),
                "transition_in": segment.transition_in,
                "transition_out": segment.transition_out,
            },
        )
        beats = song.beats.to_state() if song.beats else None
        duck = profile.get("music.duck")
        item.notes.append(
            Note(
                segment.start,
                f"jevid · music · {song.name}",
                _join(
                    [
                        f"in={segment.transition_in} {float(segment.fade_in):.2f}s {profile.get('music.fade_in.curve')}",
                        f"out={segment.transition_out} {float(segment.fade_out):.2f}s {profile.get('music.fade_out.curve')}",
                        f"duck={duck['depth_db']}dB attack={duck['attack_seconds']}s release={duck['release_seconds']}s under {len(dialogue)} dialogue spans",
                        f"bed={profile.get('music.bed_db')}dB (per-section beds in music.sections)",
                        f"beats={beats['bpm']:.1f}bpm from {beats['source']}" if beats else "beats=none; cuts on frames",
                    ]
                ),
            )
        )
        timeline.connected.append(item)


def _dialogue_spans(timeline: Timeline) -> list[tuple[Fraction, Fraction]]:
    spans = sorted(item.audio_span() for item in timeline.spine if item.tags.get("speech"))
    return spans


def music_keys(
    segment: MusicSegment,
    timeline: Timeline,
    dialogue: list[tuple[Fraction, Fraction]],
    profile: StyleProfile,
) -> list[VolumeKey]:
    """Keyframes for one bed. Times are timeline seconds; the renderer localizes them."""
    frame = timeline.frame
    start, end = segment.start, segment.end
    floor = float(profile.get("music.floor_db"))
    duck = profile.get("music.duck")
    depth = float(duck["depth_db"])
    attack = Fraction(str(duck["attack_seconds"]))
    release = Fraction(str(duck["release_seconds"]))
    merge = Fraction(str(duck["merge_gap_seconds"]))
    spans = _merge(
        [(max(a, start), min(b, end)) for a, b in dialogue if b > start and a < end], merge
    )
    ducking = [
        (a, b)
        for a, b in spans
        if duck.get("enabled") and profile.music_section(_kind_at(timeline, (a + b) / 2))["duck"]
    ]
    fade_in_end = start + segment.fade_in
    fade_out_start = end - segment.fade_out

    def bed(t: Fraction) -> float:
        return float(profile.music_section(_kind_at(timeline, t))["bed_db"])

    def envelope(t: Fraction) -> float:
        best = 0.0
        for a, b in ducking:
            if a <= t <= b:
                return 1.0
            if attack > 0 and a - attack <= t < a:
                best = max(best, float((t - (a - attack)) / attack))
            if release > 0 and b < t <= b + release:
                best = max(best, float(((b + release) - t) / release))
        return best

    def gain(t: Fraction) -> float:
        value = 1.0
        if segment.fade_in > 0 and t < fade_in_end:
            value = min(value, float((t - start) / segment.fade_in))
        if segment.fade_out > 0 and t > fade_out_start:
            value = min(value, float((end - t) / segment.fade_out))
        return max(0.0, value)

    def level(t: Fraction) -> float:
        base = bed(t) + depth * envelope(t)
        g = gain(t)
        return round(floor + (base - floor) * g if g < 1.0 else base, 2)

    points: dict[Fraction, str] = {}
    fade_in_end_q = Fraction(round(fade_in_end / frame)) * frame
    fade_out_start_q = Fraction(round(fade_out_start / frame)) * frame

    def add(t: Fraction, interp: str, inner: bool = True) -> None:
        t = Fraction(round(t / frame)) * frame
        if inner and not fade_in_end_q < t < fade_out_start_q:
            return
        if start <= t <= end and t not in points:
            points[t] = interp

    add(start, _INTERP[str(profile.get("music.fade_in.curve"))], inner=False)
    add(fade_in_end, "linear", inner=False)
    add(fade_out_start, _INTERP[str(profile.get("music.fade_out.curve"))], inner=False)
    add(end, "linear", inner=False)
    duck_interp = _INTERP[str(duck["curve"])]
    for a, b in ducking:
        add(a - attack, duck_interp)
        add(a, "linear")
        add(b, duck_interp)
        add(b + release, "linear")
    for section in timeline.sections:
        if start < section.start < end:
            add(section.start - Fraction(1, 4), "linear")
            add(section.start + Fraction(1, 4), "linear")
    ordered = sorted(points)
    keys = [VolumeKey(t, level(t), points[t]) for t in ordered]
    pruned: list[VolumeKey] = []
    for index, key in enumerate(keys):
        if 0 < index < len(keys) - 1 and keys[index - 1].db == key.db == keys[index + 1].db:
            continue
        pruned.append(key)
    return pruned


# ----------------------------------------------------------------- background


def _place_background(
    timeline: Timeline, segments: list[MusicSegment], profile: StyleProfile, lanes: Lanes, assets_dir: Path
) -> list[str]:
    art = ArtSource(profile)
    frame_size = (timeline.width, timeline.height)
    beats = timeline_beats(segments)
    max_seconds = Fraction(str(profile.get("background.change.max_seconds")))
    motion = profile.get("background.motion")
    opacity = float(profile.get("background.opacity"))
    render_scale = float(profile.get("background.render_scale"))
    plate_index = 0
    for section in timeline.sections:
        if not profile.background_on(section.kind) or section.duration <= 0:
            continue
        pieces = max(1, math.ceil(section.duration / max_seconds))
        step = section.duration / pieces
        for piece in range(pieces):
            start = _snap(section.start + step * piece, timeline.frame)
            end = section.end if piece == pieces - 1 else _snap(section.start + step * (piece + 1), timeline.frame)
            if end <= start:
                continue
            plate = render_plate(plate_index, profile, art, assets_dir, frame_size)
            rng = random.Random(plate.seed)
            item = _still_item(plate, start, end, lanes.plate, section.kind, f"Background {plate_index + 1}")
            item.blend = opacity if opacity < 1.0 else None
            item.transform = _motion(rng, start, end, beats, motion, timeline, base_scale=1.0)
            item.notes.append(
                Note(
                    start,
                    f"jevid · background · plate {plate_index + 1}",
                    _join(
                        [
                            plate.describe(),
                            f"contortion stretch={profile.get('background.contortion.stretch')} shear={profile.get('background.contortion.shear_degrees')} slices={profile.get('background.contortion.slices')}",
                            f"opacity={opacity} placement={profile.get('background.placement')} inset={profile.inset(section.kind)}",
                            f"motion drift={motion['drift']} pulse={motion['pulse']} on_beat={motion['pulse_on_beat']}",
                        ]
                    ),
                )
            )
            timeline.connected.append(item)
            for number, lane in enumerate(lanes.floaters):
                floater = render_floater(plate_index * len(lanes.floaters) + number, profile, art, assets_dir, frame_size)
                frng = random.Random(floater.seed)
                f_item = _still_item(floater, start, end, lane, section.kind, f"Rect {plate_index + 1}.{number + 1}")
                f_item.conform = "none"
                f_item.transform = _motion(frng, start, end, beats, motion, timeline, base_scale=1.0 / render_scale, floater=True)
                timeline.connected.append(f_item)
            plate_index += 1
    return list(art.warnings)


def _still_item(still: Still, start: Fraction, end: Fraction, lane: int, section: str, name: str) -> Item:
    media = Media(
        key=str(still.path),
        kind="still",
        name=still.path.stem,
        src=file_url(still.path),
        uid=still.path.stem.upper(),
        duration=Fraction(0),
        width=still.width,
        height=still.height,
        has_video=True,
    )
    return Item(
        kind="still",
        lane=lane,
        offset=start,
        duration=end - start,
        section=section,
        name=name,
        media=media,
        tags={"background": still.kind, "rects": len(still.rects), "layout": still.layout},
    )


def _motion(
    rng: random.Random,
    start: Fraction,
    end: Fraction,
    beats: list[Fraction],
    motion: dict,
    timeline: Timeline,
    *,
    base_scale: float,
    floater: bool = False,
) -> Transform:
    duration = end - start
    seconds = float(duration)
    angle = rng.uniform(0, 2 * math.pi)
    distance = min(0.1 * timeline.width, float(motion["drift"]) * timeline.width * seconds)
    origin = (0.0, 0.0)
    if floater:
        origin = (rng.uniform(-0.38, 0.38) * timeline.width, rng.uniform(-0.36, 0.36) * timeline.height)
        distance *= 1.8
    target = (round(origin[0] + distance * math.cos(angle), 2), round(origin[1] + distance * math.sin(angle), 2))
    keys = Keyframes()
    if distance > 0:
        keys.position = [(Fraction(0), origin), (duration, target)]
    pulse = float(motion["pulse"])
    base = (round(base_scale, 4), round(base_scale, 4))
    if pulse > 0 and motion.get("pulse_on_beat") and beats:
        inside = [b - start for b in beats if start <= b < end]
        if len(inside) > 48:
            inside = inside[::4]
        release = Fraction(1, 5)
        peak = (round(base_scale * (1 + pulse), 4), round(base_scale * (1 + pulse), 4))
        frames: list[tuple[Fraction, tuple[float, float]]] = [(Fraction(0), base)]
        for beat in inside:
            if beat <= frames[-1][0]:
                continue
            frames.append((beat, peak))
            if beat + release < duration:
                frames.append((beat + release, base))
        keys.scale = frames if len(frames) > 1 else []
    rotation = None
    if floater:
        low, high = motion["rotation_degrees"]
        rotation = round(rng.uniform(low, high), 2) or None
    return Transform(
        scale=base if base != (1.0, 1.0) else None,
        position=origin if origin != (0.0, 0.0) and not keys.position else None,
        rotation=rotation,
        keyframes=keys,
    )


def timeline_beats(segments: list[MusicSegment]) -> list[Fraction]:
    beats: list[Fraction] = []
    for segment in segments:
        if not segment.song.beats:
            continue
        for value in segment.song.beats.beats:
            t = segment.start + Fraction(str(value)) - segment.source_start
            if segment.start <= t < segment.end:
                beats.append(t)
    return sorted(beats)


# ----------------------------------------------------------------- transforms


def _inset(timeline: Timeline, profile: StyleProfile) -> None:
    for item in timeline.spine:
        if item.kind != "clip":
            continue
        inset = profile.inset(item.section) if profile.background_on(item.section) else 1.0
        if inset >= 1.0:
            continue
        current = item.transform.scale if item.transform and item.transform.scale else (1.0, 1.0)
        scale = (round(current[0] * inset, 4), round(current[1] * inset, 4))
        if item.transform is None:
            item.transform = Transform(scale=scale)
        else:
            item.transform.scale = scale
        item.tags["inset"] = f"A-roll inset x{inset} over the rectangle background (background.aroll_inset.{item.section})"


def _apply_treatment(item: Item, name: str, profile: StyleProfile, timeline: Timeline) -> None:
    spec = profile.get(f"text_treatments.{name}", None) or {}
    if not spec or name == "none":
        return
    duration = item.duration
    keys = Keyframes()

    def at(t: float) -> Fraction:
        value = Fraction(str(t))
        if value < 0:
            value = duration + value
        return min(max(Fraction(0), _snap(value, timeline.frame)), duration)

    for t, value in spec.get("scale") or []:
        keys.scale.append((at(t), (float(value[0]), float(value[1]))))
    for t, value in spec.get("position") or []:
        keys.position.append((at(t), (float(value[0]) * timeline.width, float(value[1]) * timeline.height)))
    for t, value in spec.get("rotation") or []:
        keys.rotation.append((at(t), float(value)))
    for track in (keys.scale, keys.position, keys.rotation):
        track.sort(key=lambda frame: frame[0])
        deduped = []
        for frame in track:
            if deduped and deduped[-1][0] == frame[0]:
                deduped[-1] = frame
            else:
                deduped.append(frame)
        track[:] = deduped
    corners = None
    if spec.get("corners"):
        corners = {
            key: (round(float(v[0]) * timeline.width, 2), round(float(v[1]) * timeline.height, 2))
            for key, v in spec["corners"].items()
        }
    item.transform = Transform(keyframes=keys, corners=corners)
    if "tracking" in spec and item.title is not None:
        style = item.title.style
        item.title.style = replace(style, kerning=round(float(spec["tracking"]) * style.size, 2))


def _text_style(spec: dict, timeline: Timeline) -> TextStyle:
    size = round(float(spec["size"]) * timeline.height, 1)
    stroke = spec.get("stroke") or None
    shadow = spec.get("shadow") or None
    return TextStyle(
        font=str(spec["font"]),
        face=str(spec["face"]),
        size=size,
        color=hex_rgba(spec["color"]),
        alignment=str(spec.get("alignment", "center")),
        kerning=round(float(spec.get("tracking", 0.0)) * size, 2),
        line_spacing=float(spec.get("line_spacing", 0) or 0),
        stroke_color=hex_rgba(stroke["color"]) if stroke else None,
        stroke_width=float(stroke["width"]) if stroke else None,
        shadow_color=hex_rgba(shadow["color"]) if shadow else None,
        shadow_distance=float(shadow["distance"]) if shadow else None,
        shadow_angle=float(shadow["angle"]) if shadow else None,
        shadow_blur=float(shadow["blur"]) if shadow else None,
    )


def _position(spec: dict, timeline: Timeline) -> tuple[float, float]:
    x, y = spec["position"]
    return round(float(x) * timeline.width, 1), round(float(y) * timeline.height, 1)


# ---------------------------------------------------------------------- notes


def _notes(timeline: Timeline, decisions: dict[str, Decision], profile: StyleProfile) -> None:
    firsts: dict[int, Item] = {}
    for section_index, section in enumerate(timeline.sections):
        first = next((item for item in timeline.spine if item.offset >= section.start), None)
        if first is not None:
            first.notes.append(section_note(section, profile))
            firsts[section_index] = first
    for item in timeline.spine:
        unit = item.tags.get("unit")
        if not unit:
            continue
        related = [d for key, d in decisions.items() if key.startswith(f"{unit}_")]
        lines = [f"why={item.tags.get('reason', '')}"]
        for decision in related:
            lines.append(f"decision={decision.label()}")
            if decision.reason:
                lines.append(f"because={decision.reason}")
        for tag in ("split", "punch_in", "inset"):
            if item.tags.get(tag):
                lines.append(f"{tag}={item.tags[tag]}")
        pacing = profile.pacing(item.section) if item.section in profile.get("pacing") else None
        if pacing:
            lines.append(f"rule=pacing.{item.section} asl={pacing['asl_seconds']}s cut_on_beat={pacing['cut_on_beat']}")
        kind = "speech" if item.tags.get("speech") else "b-roll"
        item.notes.append(
            Note(
                item.offset,
                f"jevid · {item.section} · {kind}",
                _join(lines),
                todo=any(d.review for d in related),
            )
        )
    for item in timeline.connected:
        if item.tags.get("cutaway"):
            related = [d for key, d in decisions.items() if key.startswith(f"{item.tags['unit']}_")]
            item.notes.append(
                Note(
                    item.offset,
                    "jevid · cutaway",
                    _join([f"why={item.tags['reason']}"] + [f"decision={d.label()}" for d in related]),
                    todo=any(d.review for d in related),
                )
            )


# -------------------------------------------------------------------- helpers


def _kind_at(timeline: Timeline, t: Fraction) -> str:
    section = timeline.section_at(t)
    return section.kind if section else "talking"


def _merge(spans: list[tuple[Fraction, Fraction]], gap: Fraction) -> list[tuple[Fraction, Fraction]]:
    merged: list[tuple[Fraction, Fraction]] = []
    for a, b in sorted(spans):
        if b <= a:
            continue
        if merged and a - merged[-1][1] <= gap:
            merged[-1] = (merged[-1][0], max(merged[-1][1], b))
        else:
            merged.append((a, b))
    return merged


def _emphasis_score(text: str) -> int:
    score = len(EMPHASIS.findall(text))
    if len(text.split()) <= 3:
        score += 1
    return score


def _brief_phrase(brief: str) -> str:
    words = re.sub(r"[^\w\s'-]", " ", brief).split()
    skip = {"a", "an", "the"}
    while words and words[0].lower() in skip:
        words.pop(0)
    cleaned = [w for w in words if not re.fullmatch(r"\d+([-\s]?(s|sec|second|seconds|min|minute|minutes))?", w, re.I)]
    return " ".join(cleaned[:5])


def _humanize(name: str) -> str:
    text = re.sub(r"[_\-]+", " ", name)
    text = re.sub(r"\b(a|b)\s?roll\b", "", text, flags=re.IGNORECASE)
    text = re.sub(r"^\d+\s*", "", text.strip())
    return " ".join(text.split()) or name


def _join(lines: list[str]) -> str:
    return " | ".join(line for line in lines if line and not line.endswith("="))


def _snap(value: Fraction, frame: Fraction) -> Fraction:
    return Fraction(round(value / frame)) * frame
