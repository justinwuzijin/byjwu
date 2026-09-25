"""Lay decided units into sections on the primary storyline.

The profile's ``structure.order`` is walked left to right. Each section gets
a budget (share of the target, clamped to its min/max, talking takes the
rest) and is filled from the units the decision stage assigned to it.

Cuts land on beats when the section's ``cut_on_beat`` says so: ``always``
picks the beat nearest the drawn shot length; ``prefer`` moves a cut only
within ``cuts.beat_snap_tolerance_seconds`` and never into a word. Music is
laid as the spine is: the first song starts at 0, and a song changes at a
section boundary when it will not last the section, or at a cut when it is
about to run out. Beat times are always those of the song under the cut.

Everything is frame-quantized and seeded, so the same inputs give the same
timeline.
"""

from __future__ import annotations

import bisect
import hashlib
import math
import random
from dataclasses import dataclass, field
from fractions import Fraction

from ..style import CUT_SECTIONS, StyleProfile
from .media import Material, Song
from .select import Decision, Unit, _interleave
from .timeline import Item, Media, Note, Section, Timeline, Transform

SECTION_LABELS = {
    "intro": "Intro",
    "title_card": "Title",
    "talking": "Talking",
    "montage": "Montage",
    "outro": "Outro",
    "end_card": "End card",
}


@dataclass
class Adjustments:
    """What iterate rounds change. All neutral at v0."""

    asl_scale: dict[str, float] = field(default_factory=lambda: {name: 1.0 for name in CUT_SECTIONS})
    target_scale: float = 1.0
    snap_scale: float = 1.0
    cover_scale: float = 1.0
    subtitle_fill: bool = False

    def to_state(self) -> dict:
        return {
            "asl_scale": {k: round(v, 4) for k, v in sorted(self.asl_scale.items())},
            "target_scale": round(self.target_scale, 4),
            "snap_scale": round(self.snap_scale, 4),
            "cover_scale": round(self.cover_scale, 4),
            "subtitle_fill": self.subtitle_fill,
        }

    @classmethod
    def from_state(cls, data: dict | None) -> Adjustments:
        if not data:
            return cls()
        adj = cls()
        adj.asl_scale.update({k: float(v) for k, v in (data.get("asl_scale") or {}).items()})
        adj.target_scale = float(data.get("target_scale", 1.0))
        adj.snap_scale = float(data.get("snap_scale", 1.0))
        adj.cover_scale = float(data.get("cover_scale", 1.0))
        adj.subtitle_fill = bool(data.get("subtitle_fill", False))
        return adj


@dataclass
class MusicSegment:
    song: Song
    index: int
    start: Fraction
    source_start: Fraction
    end: Fraction
    transition_in: str
    fade_in: Fraction
    fade_out: Fraction = Fraction(0)
    transition_out: str = "end"
    lane: int = -1

    @property
    def media_end(self) -> Fraction:
        return self.start + (self.song.duration - self.source_start)


class Layout:
    def __init__(
        self,
        material: Material,
        profile: StyleProfile,
        units: list[Unit],
        decisions: dict[str, Decision],
        *,
        target: float,
        adjustments: Adjustments,
        name: str,
        router=None,
        brief: str = "",
    ):
        self.material = material
        self.profile = profile
        self.units = units
        self.decisions = decisions
        self.adj = adjustments
        self.frame = material.frame
        self.target = target * adjustments.target_scale
        self.timeline = Timeline(name, self.frame, material.width, material.height)
        self.t = Fraction(0)
        self.beats: list[Fraction] = []
        self.downbeats: list[Fraction] = []
        self.segments: list[MusicSegment] = []
        self.song_cursor = -1
        self.warnings: list[str] = []
        self.router = router
        self.brief = brief
        self.receipts: list[dict] = []
        self.seed = int(profile.get("background.art.seed"))
        self.min_shot = self._f(profile.get("cuts.min_shot_seconds"))
        self.snap_tol = self._f(float(profile.get("cuts.beat_snap_tolerance_seconds")) * adjustments.snap_scale)
        self.used: set[str] = set()
        self.cutaways: list[Item] = []
        self._media: dict[str, Media] = {}

    # ------------------------------------------------------------------ run

    def run(self) -> Timeline:
        order = list(self.profile.get("structure.order"))
        pools = self._pools()
        budgets = self._budgets(order, pools)
        talking_groups = _split(pools["talking"], order.count("talking"))
        montage_groups = _split(pools["montage"], order.count("montage"))
        cutaway_groups = _split(pools["cutaway"], order.count("talking"))
        counters = {kind: 0 for kind in order}
        if self.material.songs:
            self._start_song(Fraction(0), "start")
        for kind in order:
            index = counters[kind]
            counters[kind] += 1
            budget = budgets[kind][index] if isinstance(budgets[kind], list) else budgets[kind]
            start = self.t
            label = SECTION_LABELS[kind] + (f" {index + 1}" if order.count(kind) > 1 else "")
            section = Section(kind, index, label, start, start, self._f(budget))
            if self.segments:
                self._music_check(start, section.budget, boundary=True)
            spine_before = len(self.timeline.spine)
            if kind == "intro":
                self._intro(section, pools)
            elif kind == "talking":
                self._talking(section, talking_groups[index], cutaway_groups[index])
            elif kind == "montage":
                self._montage(section, montage_groups[index])
            elif kind == "outro":
                self._speech_run(section, pools["outro"], "outro")
            elif kind in ("title_card", "end_card"):
                self._card(section)
            section.end = self.t
            if len(self.timeline.spine) == spine_before:
                self.warnings.append(f"{label}: no material; section skipped.")
                continue
            self.timeline.sections.append(section)
        self._close_music()
        self.timeline.connected.extend(self.cutaways)
        if float(self.t) < self.target - float(self.profile.get("structure.tolerance_seconds")):
            self.warnings.append(
                f"material ran out: {float(self.t):.1f}s against a {self.target:.1f}s target."
            )
        return self.timeline

    # ------------------------------------------------------------ sections

    def _intro(self, section: Section, pools: dict[str, list[Unit]]) -> None:
        speech = pools["intro"]
        flashes = pools["intro_visual"]
        share = float(self.profile.get("structure.intro_flash_share"))
        flash_budget = section.budget * self._f(share) if speech else section.budget
        mode = self.profile.pacing("intro")["cut_on_beat"]
        draws = self._draws("intro", section.index)
        for unit in flashes:
            if self.t - section.start >= flash_budget - self.min_shot:
                break
            self._music_check(self.t, self.min_shot)
            end, on_beat = self._cut_point(next(draws), mode, unit.duration)
            self._place_visual(unit, section, end - self.t, on_beat, mode, "intro flash")
        for unit in speech:
            if self.t - section.start >= section.budget:
                break
            self._place_speech(unit, section, mode)
        self._split_edits(section)

    def _talking(self, section: Section, units: list[Unit], cutaways: list[Unit]) -> None:
        mode = self.profile.pacing("talking")["cut_on_beat"]
        slack = self._f(1)
        for unit in units:
            if self.t > section.start and self.t - section.start + unit.duration > section.budget + slack:
                break
            self._place_speech(unit, section, mode)
        self._split_edits(section)
        self._punch_ins(section)
        if self._keyword_cover(section, cutaways) is None:
            self._cover(section, cutaways)

    def _speech_run(self, section: Section, units: list[Unit], kind: str) -> None:
        mode = self.profile.pacing(kind)["cut_on_beat"]
        for unit in units:
            if self.t > section.start and self.t - section.start + unit.duration > section.budget + 1:
                break
            self._place_speech(unit, section, mode)
        self._split_edits(section)

    def _montage(self, section: Section, units: list[Unit]) -> None:
        mode = self.profile.pacing("montage")["cut_on_beat"]
        draws = self._draws("montage", section.index)
        for unit in units:
            if self.t - section.start >= section.budget - self.min_shot:
                break
            self._music_check(self.t, self.min_shot)
            end, on_beat = self._cut_point(next(draws), mode, unit.duration)
            self._place_visual(unit, section, end - self.t, on_beat, mode, "montage shot")

    def _card(self, section: Section) -> None:
        kind = section.kind
        seconds = self._f(self.profile.get(f"structure.{kind}.seconds"))
        bars = int(self.profile.get(f"structure.{kind}.bars"))
        desired = self.t + seconds
        reason = f"structure.{kind}.seconds={float(seconds)}"
        on_beat = None
        if bars and self.beats and len(self.beats) > 1:
            period = self._period()
            bar = period * int(self.profile.get("music.beats.beats_per_bar"))
            desired = self.t + bar * bars
            end = self._nearest(self.downbeats, desired, bar / 2) or self._nearest(self.beats, desired, period / 2)
            if end is not None and end - self.t >= self.min_shot:
                desired = end
                on_beat = True
                reason = f"{bars} bars of music (structure.{kind}.bars), ends on a downbeat"
        duration = max(self.frame, self._q(desired - self.t))
        item = Item(
            kind="gap",
            lane=0,
            offset=self.t,
            duration=duration,
            section=kind,
            name=SECTION_LABELS[kind],
            tags={"card": kind, "on_beat": on_beat, "beat_mode": "always" if bars else "off", "reason": reason},
        )
        self.timeline.spine.append(item)
        self.t += duration

    # ------------------------------------------------------------- placing

    def _place_speech(self, unit: Unit, section: Section, mode: str) -> None:
        self._music_check(self.t, min(unit.duration, self._f(3)))
        clip = unit.footage.clip
        src_start, src_end = unit.start, unit.end
        natural = self.t + (src_end - src_start)
        on_beat: bool | None = None
        how = "natural end of the line"
        if mode != "off" and self.beats:
            after = self._next_speech_start(unit)
            extend_room = max(Fraction(0), min(after, clip.duration) - src_end)
            last_word = unit.cues[-1].end if unit.cues else src_end
            trim_room = max(Fraction(0), src_end - last_word)
            tol = self.snap_tol * (2 if mode == "always" else 1)
            lo = natural - min(trim_room, tol)
            hi = natural + min(extend_room, tol)
            beat = self._nearest(self.beats, natural, tol, lo=max(lo, self.t + self.min_shot), hi=hi)
            on_beat = beat is not None
            if beat is not None:
                src_end = src_start + (beat - self.t)
                how = f"end moved {float(beat - natural):+.3f}s onto a beat (pacing.{section.kind}.cut_on_beat={mode})"
            else:
                how = f"no beat within {float(tol):.2f}s that keeps every word; natural end kept"
        duration = max(self.frame, self._q(src_end - src_start))
        duration = min(duration, self._q(clip.duration - src_start))
        item = Item(
            kind="clip",
            lane=0,
            offset=self.t,
            duration=duration,
            section=section.kind,
            name=clip.stem,
            media=self._clip_media(unit),
            start=src_start,
            role="dialogue",
            tags={
                "unit": unit.id,
                "speech": True,
                "on_beat": on_beat,
                "beat_mode": mode,
                "reason": how,
                "footage": unit.footage_index,
            },
        )
        self.timeline.spine.append(item)
        self.used.add(unit.id)
        self.t += duration

    def _place_visual(
        self, unit: Unit, section: Section, length: Fraction, on_beat: bool | None, mode: str, what: str
    ) -> None:
        clip = unit.footage.clip
        duration = max(self.frame, self._q(min(length, unit.duration)))
        duration = min(duration, self._q(clip.duration - unit.start))
        item = Item(
            kind="clip",
            lane=0,
            offset=self.t,
            duration=duration,
            section=section.kind,
            name=clip.stem,
            media=self._clip_media(unit),
            start=unit.start,
            role="effects",
            volume_db=float(self.profile.get("cuts.broll_nat_sound_db")) if clip.has_audio else None,
            tags={
                "unit": unit.id,
                "speech": False,
                "on_beat": on_beat,
                "beat_mode": mode,
                "reason": f"{what}; " + ("cut on a beat" if on_beat else "no beat in reach; drawn length kept"),
                "footage": unit.footage_index,
            },
        )
        self.timeline.spine.append(item)
        self.used.add(unit.id)
        self.t += duration

    def _keyword_cover(self, section: Section, pool: list[Unit]):
        """Keyword slots over talking. None means the pacing cover should run."""
        from .broll import cover_speech

        hosts = [item for item in self.timeline.spine if item.offset >= section.start and item.tags.get("speech")]
        if not hosts or not pool:
            return None
        result = cover_speech(
            hosts,
            self.units,
            pool,
            profile=self.profile,
            frame=self.frame,
            beats=self.beats,
            snap_tolerance=self.snap_tol,
            beat_mode=self.profile.pacing("talking")["cut_on_beat"],
            router=self.router,
            brief=self.brief,
            media_for=self._clip_media,
        )
        if not result.applied:
            return None
        self.cutaways.extend(result.items)
        self.decisions.update(result.decisions)
        self.receipts.extend(result.receipts)
        for item in result.items:
            if item.tags.get("unit"):
                self.used.add(item.tags["unit"])
        return result

    def _cover(self, section: Section, pool: list[Unit]) -> None:
        cover = float(self.profile.pacing("talking").get("broll_cover", 0.0)) * self.adj.cover_scale
        if cover <= 0 or not pool:
            return
        spec = self.profile.get("cuts.cutaway")
        low, high = self._f(spec["min_seconds"]), self._f(spec["max_seconds"])
        length_so_far = self.t - section.start
        wanted = length_so_far * self._f(min(cover, 0.9))
        placed = Fraction(0)
        mode = self.profile.pacing("talking")["cut_on_beat"]
        clips = [item for item in self.timeline.spine if item.offset >= section.start and item.tags.get("speech")]
        asl = float(self.profile.pacing("talking")["asl_seconds"]) * self.adj.asl_scale.get("talking", 1.0)
        allowed_shots = float(length_so_far) / max(0.1, asl)
        budget = max(0, int((allowed_shots - len(clips)) // 2))
        eligible = [host for host in clips if host.duration - self._f("0.8") >= low]
        count = min(budget, len(pool), len(eligible))
        if count <= 0:
            if pool and eligible:
                section.notes.append(
                    f"no cutaways: pacing.talking.asl_seconds={asl:.2f} leaves no room for extra shots"
                )
            return
        step = len(eligible) / count
        hosts = [eligible[int(i * step + step / 2)] for i in range(count)]
        pool = list(pool)
        for host in hosts:
            if placed >= wanted or not pool:
                break
            unit = pool.pop(0)
            start = host.offset + min(host.duration * Fraction(1, 4), self._f("0.6"))
            length = min(unit.duration, high, host.end - self._f("0.4") - start)
            if length < low:
                pool.insert(0, unit)
                continue
            end = start + length
            on_beat = None
            if mode != "off" and self.beats:
                snapped_start = self._nearest(self.beats, start, self.snap_tol, lo=host.offset, hi=host.end - low)
                if snapped_start is not None:
                    start = snapped_start
                end = start + length
                snapped_end = self._nearest(self.beats, end, self.snap_tol, lo=start + low, hi=min(start + unit.duration, host.end))
                if snapped_end is not None:
                    end = snapped_end
                on_beat = snapped_start is not None and snapped_end is not None
            start, end = self._q(start), self._q(end)
            if end - start < self.frame:
                continue
            item = Item(
                kind="clip",
                lane=1,
                offset=start,
                duration=end - start,
                section=section.kind,
                name=unit.footage.clip.stem,
                media=self._clip_media(unit),
                start=unit.start,
                src_enable="video",
                tags={
                    "unit": unit.id,
                    "cutaway": True,
                    "on_beat": on_beat,
                    "beat_mode": mode,
                    "reason": f"cutaway over {host.name} to reach pacing.talking.broll_cover={cover:.2f}",
                },
            )
            self.cutaways.append(item)
            self.used.add(unit.id)
            placed += end - start

    def _split_edits(self, section: Section) -> None:
        spec_j = self.profile.get("cuts.j_cut")
        spec_l = self.profile.get("cuts.l_cut")
        items = [i for i in self.timeline.spine if i.offset >= section.start and i.kind == "clip"]
        for previous, current in zip(items, items[1:]):
            if not (previous.tags.get("speech") and current.tags.get("speech")):
                continue
            roll = _unit_random(self.seed, current.tags["unit"])
            lead = self._q(self._f(spec_j["lead_seconds"]))
            tail = self._q(self._f(spec_l["tail_seconds"]))
            if roll < float(spec_j["probability"]) and current.start - lead >= 0 and lead > 0:
                current.audio_start = current.start - lead
                current.audio_duration = current.duration + lead
                current.tags["split"] = f"J-cut, audio leads {float(lead):.2f}s (cuts.j_cut)"
            elif roll < float(spec_j["probability"]) + float(spec_l["probability"]) and tail > 0:
                media_end = previous.media.duration if previous.media else previous.start + previous.duration
                audio_start = previous.audio_start if previous.audio_start is not None else previous.start
                audio_duration = previous.audio_duration if previous.audio_duration is not None else previous.duration
                if audio_start + audio_duration + tail <= media_end:
                    previous.audio_start = audio_start
                    previous.audio_duration = audio_duration + tail
                    previous.tags["split"] = f"L-cut, audio trails {float(tail):.2f}s (cuts.l_cut)"

    def _punch_ins(self, section: Section) -> None:
        spec = self.profile.get("cuts.punch_in")
        if not spec.get("enabled"):
            return
        scale = float(spec["scale"])
        every = int(spec["every"])
        run = 0
        previous: Item | None = None
        for item in self.timeline.spine:
            if item.offset < section.start or not item.tags.get("speech"):
                continue
            same = previous is not None and previous.tags.get("footage") == item.tags.get("footage")
            run = run + 1 if same else 0
            if run and run % every == 1 % every:
                item.transform = Transform(scale=(scale, scale))
                item.tags["punch_in"] = f"punch-in x{scale:.2f} hides the jump cut (cuts.punch_in)"
            previous = item

    # --------------------------------------------------------------- music

    def _start_song(self, at: Fraction, transition: str) -> None:
        songs = self.material.songs
        self.song_cursor += 1
        song = songs[self.song_cursor % len(songs)]
        if self.song_cursor >= len(songs):
            self.warnings.append(f"ran out of music; {song.name} is reused from {float(at):.1f}s.")
        source_start = Fraction(0)
        if self.profile.get("music.start_on_downbeat") and song.beats and song.beats.downbeats:
            source_start = self._q(Fraction(str(song.beats.downbeats[0])))
        fade_in = self._f(self.profile.get("music.fade_in.seconds"))
        if transition == "crossfade":
            fade_in = self._f(self.profile.get("music.song_change.crossfade_seconds"))
        elif transition == "cut":
            fade_in = self.frame * 2
        segment = MusicSegment(
            song=song,
            index=len(self.segments),
            start=at,
            source_start=source_start,
            end=at + (song.duration - source_start),
            transition_in=transition,
            fade_in=self._q(fade_in),
            lane=-1 if len(self.segments) % 2 == 0 else -2,
        )
        self.segments.append(segment)
        self.beats = [b for b in self.beats if b < at]
        self.downbeats = [b for b in self.downbeats if b < at]
        if song.beats:
            for value in song.beats.beats:
                t = Fraction(str(value))
                if t >= source_start:
                    self.beats.append(self._q(at + t - source_start))
            for value in song.beats.downbeats:
                t = Fraction(str(value))
                if t >= source_start:
                    self.downbeats.append(self._q(at + t - source_start))

    def _music_check(self, t: Fraction, upcoming: Fraction, boundary: bool = False) -> None:
        if not self.segments:
            return
        current = self.segments[-1]
        remaining = current.media_end - t
        margin = self._f(self.profile.get("music.fade_out.seconds"))
        at_boundary = boundary and self.profile.get("music.song_change.at") == "section_boundary"
        if at_boundary and remaining < upcoming + margin and (len(self.material.songs) > 1 or remaining < upcoming):
            self._change_song(t)
        elif remaining < upcoming + self.frame * 2:
            self._change_song(t)

    def _change_song(self, t: Fraction) -> None:
        current = self.segments[-1]
        transition = str(self.profile.get("music.song_change.transition"))
        if transition == "crossfade":
            overlap = self._q(self._f(self.profile.get("music.song_change.crossfade_seconds")))
            current.end = min(current.media_end, t + overlap)
            current.fade_out = max(self.frame, current.end - t)
        elif transition == "cut":
            current.end = min(current.media_end, t)
            current.fade_out = self.frame * 2
        else:
            current.end = min(current.media_end, t)
            current.fade_out = self._q(self._f(self.profile.get("music.fade_out.seconds")))
        current.transition_out = transition
        self._start_song(t, transition)

    def _close_music(self) -> None:
        if not self.segments:
            return
        end = self.t
        kept = [segment for segment in self.segments if segment.start < end]
        self.segments = kept
        last = kept[-1]
        last.end = min(last.media_end, end)
        last.fade_out = self._q(self._f(self.profile.get("music.fade_out.seconds")))
        last.transition_out = "end"
        for segment in kept:
            length = segment.end - segment.start
            segment.fade_in = min(segment.fade_in, self._q(length / 2))
            segment.fade_out = min(segment.fade_out, self._q(length / 2))

    # -------------------------------------------------------------- helpers

    def _pools(self) -> dict[str, list[Unit]]:
        threshold = float(self.profile.get("structure.keep_threshold"))
        pools: dict[str, list[Unit]] = {
            "intro": [],
            "talking": [],
            "outro": [],
            "intro_visual": [],
            "montage": [],
            "cutaway": [],
        }
        visual: dict[str, list[Unit]] = {"intro": [], "montage": [], "cutaway": []}
        for unit in self.units:
            if unit.kind == "speech":
                section = self._value(f"{unit.id}_section", "talking")
                keep = float(self._value(f"{unit.id}_keep", 1.0))
                if section == "drop" or keep < threshold:
                    continue
                pools[str(section)].append(unit)
            else:
                use = str(self._value(f"{unit.id}_use", "montage"))
                if use in visual:
                    visual[use].append(unit)
        pools["intro_visual"] = _interleave(visual["intro"])
        pools["montage"] = _interleave(visual["montage"])
        pools["cutaway"] = _interleave(visual["cutaway"])
        return pools

    def _budgets(self, order: list[str], pools: dict[str, list[Unit]]) -> dict:
        from .select import estimate_budgets

        base = estimate_budgets(self.profile, self.target)
        budgets: dict = {}
        freed = 0.0
        for kind in ("intro", "montage", "outro"):
            have = sum(float(u.duration) for u in pools[kind] + (pools["intro_visual"] if kind == "intro" else []))
            budgets[kind] = min(base[kind], have) if kind != "montage" else base[kind]
            if kind == "montage" and not pools["montage"]:
                budgets[kind] = 0.0
            freed += base[kind] - budgets[kind]
        talking_total = base["talking"] + freed
        talking_count = max(1, order.count("talking"))
        budgets["talking"] = [talking_total / talking_count] * talking_count
        montage_count = max(1, order.count("montage"))
        budgets["montage"] = [budgets["montage"] / montage_count] * montage_count
        for card in ("title_card", "end_card"):
            budgets[card] = float(self.profile.get(f"structure.{card}.seconds"))
        return budgets

    def _cut_point(self, target: Fraction, mode: str, max_len: Fraction) -> tuple[Fraction, bool | None]:
        lo = self.t + self.min_shot
        hi = self.t + max(self.min_shot, max_len)
        ideal = self.t + target
        if mode != "off" and self.beats:
            if mode == "always":
                beat = self._nearest(self.beats, ideal, None, lo=lo, hi=hi)
            else:
                beat = self._nearest(self.beats, ideal, self.snap_tol, lo=lo, hi=hi)
            if beat is not None:
                return beat, True
            return self._q(min(max(ideal, lo), hi)), False
        return self._q(min(max(ideal, lo), hi)), None

    def _draws(self, section: str, index: int):
        pacing = self.profile.pacing(section)
        shot = pacing["shot_length"]
        median, p10, p90 = float(shot["median"]), float(shot["p10"]), float(shot["p90"])
        sigma = max(1e-3, (math.log(p90) - math.log(p10)) / (2 * 1.2816))
        mean = math.exp(math.log(median) + sigma * sigma / 2)
        factor = float(pacing["asl_seconds"]) / mean * self.adj.asl_scale.get(section, 1.0)
        rng = random.Random(self.seed * 31 + index * 7 + CUT_SECTIONS.index(section))
        while True:
            value = rng.lognormvariate(math.log(median), sigma)
            value = min(max(value, p10 / 2), p90 * 1.5) * factor
            yield self._f(round(value, 4))

    def _nearest(
        self,
        grid: list[Fraction],
        t: Fraction,
        tol: Fraction | None,
        *,
        lo: Fraction | None = None,
        hi: Fraction | None = None,
    ) -> Fraction | None:
        if not grid:
            return None
        index = bisect.bisect_left(grid, t)
        best: Fraction | None = None
        for candidate in grid[max(0, index - 3) : index + 3]:
            if lo is not None and candidate < lo:
                continue
            if hi is not None and candidate > hi:
                continue
            if tol is not None and abs(candidate - t) > tol:
                continue
            if best is None or abs(candidate - t) < abs(best - t):
                best = candidate
        if best is None and tol is None and lo is not None and hi is not None:
            inside = [b for b in grid[bisect.bisect_left(grid, lo) : bisect.bisect_right(grid, hi)]]
            if inside:
                best = min(inside, key=lambda b: abs(b - t))
        return best

    def _period(self) -> Fraction:
        ahead = [b for b in self.beats if b >= self.t][:9]
        if len(ahead) >= 2:
            return (ahead[-1] - ahead[0]) / (len(ahead) - 1)
        return (self.beats[-1] - self.beats[0]) / max(1, len(self.beats) - 1)

    def _next_speech_start(self, unit: Unit) -> Fraction:
        later = [
            u.start
            for u in self.units
            if u.footage_index == unit.footage_index and u.kind == "speech" and u.start >= unit.end
        ]
        return min(later) if later else unit.footage.clip.duration

    def _value(self, key: str, default):
        decision = self.decisions.get(key)
        return decision.value if decision is not None else default

    def _clip_media(self, unit: Unit) -> Media:
        clip = unit.footage.clip
        key = str(clip.path)
        if key not in self._media:
            self._media[key] = Media(
                key=key,
                kind="video",
                name=clip.stem,
                src=clip.src,
                uid=clip.uid,
                duration=clip.duration,
                width=clip.width,
                height=clip.height,
                frame_duration=clip.frame_duration,
                has_video=clip.has_video,
                has_audio=clip.has_audio,
            )
        return self._media[key]

    def _q(self, value: Fraction) -> Fraction:
        return Fraction(round(Fraction(value) / self.frame)) * self.frame

    @staticmethod
    def _f(value) -> Fraction:
        return Fraction(str(value))


def section_note(section: Section, profile: StyleProfile) -> Note:
    value = section.label
    if section.kind == "end_card":
        value = str(profile.get("structure.end_card.text", None) or value)
    elif section.kind == "title_card":
        text = profile.get("structure.title_card.text", None)
        value = str(text) if text else value
    lines = [f"jevid.section={section.kind}", f"budget={float(section.budget):.2f}s"]
    if section.kind in CUT_SECTIONS:
        pacing = profile.pacing(section.kind)
        lines.append(f"asl_target={pacing['asl_seconds']}s")
        lines.append(f"cut_on_beat={pacing['cut_on_beat']}")
    lines.extend(section.notes)
    return Note(section.start, value, "; ".join(lines), chapter=True)


def _split(units: list[Unit], count: int) -> list[list[Unit]]:
    if count <= 0:
        return []
    if count == 1:
        return [list(units)]
    total = sum(float(u.duration) for u in units)
    groups: list[list[Unit]] = [[] for _ in range(count)]
    cumulative = 0.0
    for unit in units:
        slot = min(count - 1, int(cumulative / total * count)) if total > 0 else 0
        groups[slot].append(unit)
        cumulative += float(unit.duration)
    return groups


def _unit_random(seed: int, key: str) -> float:
    digest = hashlib.blake2b(f"{seed}:{key}".encode(), digest_size=8).digest()
    return int.from_bytes(digest, "big") / 2**64
