"""Recompute the byjustinwu profile's numbers from the style-study bundle.

Usage::

    python styles/byjustinwu/study/analyze.py --bundle /path/to/unzipped/bundle
    python styles/byjustinwu/study/analyze.py --bundle B --out styles/byjustinwu/study/measured.json

``--bundle`` is the folder both zips were unpacked into (it holds ``data/``
and ``frames/``). The only interpretive input is ``sections.json`` beside
this file: hand labels of section type per time range, card timings, and the
frames used for caption, palette and slam-text measurement. Everything else
is arithmetic on the bundle's CSV/JSON. Output is deterministic.

numpy is required. Pillow is optional; without it the frame-derived blocks
(``captions.geometry``, ``graphics.palette``, ``typography.slam``) are
written as ``null``.

Measurement limits worth knowing before trusting a number:

- Loudness is the mixed programme at 100 ms steps with a 400 ms window, so
  onsets are smeared by up to ~0.4 s and music cannot be separated from
  speech. Beat alignment and duck timing are coarse.
- Scene cuts are ffmpeg scene scores; jump cuts on a locked-off talking head
  often score ~0.1 and are counted separately with a spike detector.
- ASR word times come from YouTube auto-captions and transcribe song lyrics
  as words. Only words outside ``[music]``/``>>`` tokens are used.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import sys
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
LONG_VIDEOS = ("iK5xtVEnSvU", "UhPZ4HeJQ6c", "36ssmIOLffw")
MUSIC_TYPES = frozenset({"broll", "montage", "monologue", "chapter_card", "end_card"})
SPEECH_TYPES = frozenset({"talking", "confessional", "vlog"})
RHYTHM_TYPES = ("talking", "confessional", "vlog", "broll", "montage", "monologue")
SEED = 20260925


def _rows(path: Path) -> list[dict]:
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def _col(rows: list[dict], key: str) -> np.ndarray:
    return np.array([float(row[key]) if row[key] not in ("", None) else np.nan for row in rows])


def _r(value, digits: int = 3):
    if value is None:
        return None
    if isinstance(value, (list, tuple)):
        return [_r(item, digits) for item in value]
    value = float(value)
    if math.isnan(value) or math.isinf(value):
        return None
    return round(value, digits)


def _dist(values) -> dict | None:
    arr = np.asarray([v for v in values if v is not None and not math.isnan(v)], dtype=float)
    if arr.size == 0:
        return None
    return {
        "n": int(arr.size),
        "median": _r(np.median(arr)),
        "mean": _r(arr.mean()),
        "p10": _r(np.percentile(arr, 10)),
        "p25": _r(np.percentile(arr, 25)),
        "p75": _r(np.percentile(arr, 75)),
        "p90": _r(np.percentile(arr, 90)),
        "min": _r(arr.min()),
        "max": _r(arr.max()),
    }


class Video:
    def __init__(self, bundle: Path, vid: str, labels: dict):
        data = bundle / "data" / vid
        self.id = vid
        self.frames = bundle / "frames" / vid
        self.summary = json.loads((data / "summary.json").read_text())
        self.chapters = json.loads((data / "metadata.json").read_text()).get("chapters") or []
        self.duration = float(self.summary["duration_s"])
        self.sections = labels["sections"]
        self.weight = labels.get("weight", 1.0)
        self.cuts = {}
        for thr in ("0p3", "0p15"):
            starts = _col(_rows(data / f"shots_thr{thr}.csv"), "start_s")
            self.cuts[thr] = starts[1:]
        loud = _rows(data / "loudness_ebur128_100ms.csv")
        self.lt = _col(loud, "time_s")
        self.lm = _col(loud, "momentary_lufs_400ms")
        self.ls = _col(loud, "shortterm_lufs_3s")
        spec = _rows(data / "spectral_features_500ms.csv")
        self.st = _col(spec, "time_s")
        self.bass = _col(spec, "band_0_150hz")
        scene = _rows(data / "scene_scores_per_frame.csv")
        self.ft = _col(scene, "time_s")
        self.fs = _col(scene, "scene_score")
        self.silences = json.loads((data / "silence_map.json").read_text())["silences"]
        words_path = data / "transcript_words.csv"
        self.words = []
        if words_path.is_file():
            for row in _rows(words_path):
                token = row["word"]
                if "[" in token or "&gt;" in token or ">>" in token:
                    continue
                self.words.append(float(row["start_s"]))
        self.words = np.array(sorted(self.words))

    def section_of(self, t: float) -> dict | None:
        for sec in self.sections:
            if sec["start"] <= t < sec["end"]:
                return sec
        return None

    def shots_in(self, sec: dict, thr: str = "0p3") -> list[float]:
        """Shot lengths inside a section, clipped to its edges."""
        edges = [sec["start"]] + [c for c in self.cuts[thr] if sec["start"] < c < sec["end"]] + [sec["end"]]
        return [b - a for a, b in zip(edges, edges[1:]) if b - a > 1e-6]

    def mom(self, a: float, b: float) -> np.ndarray:
        k = (self.lt > a) & (self.lt <= b) & (self.lm > -70)
        return self.lm[k]

    def smooth(self, width: int = 5) -> np.ndarray:
        """Running median of momentary loudness (width x 100 ms)."""
        pad = width // 2
        x = np.clip(self.lm, -70, None)
        padded = np.pad(x, pad, mode="edge")
        return np.array([np.median(padded[i:i + width]) for i in range(len(x))])


# ---------------------------------------------------------------- rhythm


def jump_cut_spikes(v: Video, a: float, b: float, floor: float = 0.08, ratio: float = 4.0) -> list[float]:
    """Isolated one-frame scene-score spikes: jump cuts on a static frame."""
    k = np.where((v.ft >= a) & (v.ft < b))[0]
    out = []
    for i in k:
        s = v.fs[i]
        if s < floor or s > 0.3:
            continue
        lo, hi = max(0, i - 12), min(len(v.fs), i + 13)
        neighbours = np.concatenate([v.fs[lo:i], v.fs[i + 1:hi]])
        if s >= ratio * max(np.median(neighbours), 0.005) and np.sum(neighbours > 0.5 * s) <= 1:
            if not out or v.ft[i] - out[-1] > 0.5:
                out.append(float(v.ft[i]))
    return out


def rhythm(videos: list[Video]) -> dict:
    out: dict = {"by_section": {}, "per_video": {}, "talking": {}, "montage": {}}
    for kind in RHYTHM_TYPES:
        for thr in ("0p3", "0p15"):
            lengths, secs, cut_count = [], 0.0, 0
            for v in videos:
                for sec in v.sections:
                    if sec["type"] != kind:
                        continue
                    shots = v.shots_in(sec, thr)
                    lengths += shots
                    secs += sec["end"] - sec["start"]
                    cut_count += max(len(shots) - 1, 0)
            if not lengths:
                continue
            entry = _dist(lengths)
            entry["cuts_per_min"] = _r(60.0 * cut_count / secs) if secs else None
            entry["seconds_labelled"] = _r(secs, 1)
            out["by_section"].setdefault(kind, {})[f"thr{thr}"] = entry
    for v in videos:
        out["per_video"][v.id] = {
            thr: {
                "median_shot_s": v.summary["shots"][thr]["shot_length_s"]["median"],
                "cuts_per_min": v.summary["shots"][thr]["cuts_per_minute"],
            }
            for thr in ("0p3", "0p15")
        }

    holds, effective, spikes_per_min, secs = [], [], [], 0.0
    for v in videos:
        for sec in v.sections:
            if sec["type"] not in ("talking", "confessional"):
                continue
            holds += v.shots_in(sec)
            spikes = jump_cut_spikes(v, sec["start"], sec["end"])
            cuts = sorted(set([c for c in v.cuts["0p3"] if sec["start"] < c < sec["end"]] + spikes))
            edges = [sec["start"]] + cuts + [sec["end"]]
            effective += [b - a for a, b in zip(edges, edges[1:])]
            dur = sec["end"] - sec["start"]
            secs += dur
            spikes_per_min.append(60.0 * len(spikes) / dur)
    out["talking"] = {
        "shot_hold_thr0p3": _dist(holds),
        "hold_with_jump_cuts": _dist(effective),
        "jump_cut_spikes_per_min": _dist(spikes_per_min),
        "longest_hold_s": _r(max(holds)) if holds else None,
        "seconds_labelled": _r(secs, 1),
    }

    fastest, bursts = [], []
    for v in videos:
        for sec in v.sections:
            if sec["type"] != "montage":
                continue
            shots = v.shots_in(sec)
            for i in range(0, max(len(shots) - 7, 0)):
                fastest.append((float(np.median(shots[i:i + 8])), v.id, sec["start"]))
            cuts15 = [c for c in v.cuts["0p15"] if sec["start"] < c < sec["end"]]
            i = 0
            while i < len(cuts15):
                j = i
                while j + 1 < len(cuts15) and cuts15[j + 1] - cuts15[j] <= 0.35:
                    j += 1
                if j - i + 1 >= 4:
                    bursts.append({"video": v.id, "start": _r(cuts15[i]), "end": _r(cuts15[j]), "cuts": j - i + 1})
                i = j + 1
    fastest.sort()
    out["montage"] = {
        "fastest_8_shot_median_s": _r(fastest[0][0]) if fastest else None,
        "fastest_8_shot_where": {"video": fastest[0][1], "section_start": fastest[0][2]} if fastest else None,
        "flash_bursts": bursts,
    }
    return out


def beat_alignment(videos: list[Video]) -> dict:
    """Do montage cuts land on loudness onsets more than chance?

    A ratio near 1 means cuts sit on onsets no more often than shifted cuts
    do. Heavily limited masters flatten a 400 ms loudness window, so a null
    here is "not detectable", not "off the beat".
    """
    rng = np.random.default_rng(SEED)
    per_video, all_obs, all_null = {}, [], []
    tempos = []
    for v in videos:
        onset = np.concatenate([[0.0], np.clip(np.diff(np.clip(v.lm, -70, None)), 0, None)])
        cuts, spans = [], []
        for sec in v.sections:
            if sec["type"] != "montage":
                continue
            spans.append((sec["start"], sec["end"]))
            cuts += [c for c in v.cuts["0p3"] if sec["start"] + 0.5 < c < sec["end"] - 0.5]
            tempo = _tempo(v, onset, sec["start"], sec["end"])
            if tempo:
                tempos.append({"video": v.id, "start": sec["start"], "bpm": tempo})
        if len(cuts) < 5:
            continue

        def stat(times):
            vals = []
            for t in times:
                k = (v.lt >= t) & (v.lt <= t + 0.3)
                vals.append(onset[k].max() if k.any() else 0.0)
            return float(np.mean(vals))

        observed = stat(cuts)
        null = []
        for _ in range(400):
            shift = rng.uniform(0.5, 2.0) * rng.choice([-1, 1])
            shifted = [c + shift for c in cuts if any(a < c + shift < b for a, b in spans)]
            if shifted:
                null.append(stat(shifted))
        null = np.array(null)
        per_video[v.id] = {
            "montage_cuts": len(cuts),
            "onset_at_cut_db": _r(observed),
            "onset_null_mean_db": _r(null.mean()),
            "ratio": _r(observed / null.mean()) if null.mean() else None,
            "p_value": _r((np.sum(null >= observed) + 1) / (len(null) + 1)),
        }
        all_obs.append(observed)
        all_null.append(null.mean())
    return {
        "method": "mean positive momentary-loudness rise within +0..0.3 s of each thr0.3 montage cut, vs the same cuts shifted 0.5-2 s (400 draws)",
        "per_video": per_video,
        "pooled_ratio": _r(np.mean(all_obs) / np.mean(all_null)) if all_obs else None,
        "montage_tempo_estimates": tempos,
    }


def _tempo(v: Video, onset: np.ndarray, a: float, b: float) -> float | None:
    k = (v.lt >= a) & (v.lt < b)
    x = onset[k]
    if x.size < 150:
        return None
    x = x - x.mean()
    ac = np.correlate(x, x, mode="full")[x.size - 1:]
    lags = np.arange(4, 11)  # 0.4-1.0 s at 100 ms: 60-150 BPM
    best = lags[np.argmax(ac[lags])]
    return _r(60.0 / (best * 0.1), 1)


# ---------------------------------------------------------------- audio


def levels(videos: list[Video]) -> dict:
    out: dict = {"by_section": {}, "programme": {}}
    for kind in sorted({s["type"] for v in videos for s in v.sections}):
        med, p10, gaps, bass, silences, secs = [], [], [], [], 0, 0.0
        for v in videos:
            for sec in v.sections:
                if sec["type"] != kind:
                    continue
                m = v.mom(sec["start"], sec["end"])
                if m.size:
                    med.append(np.median(m))
                    p10.append(np.percentile(m, 10))
                gaps += _gap_levels(v, sec["start"], sec["end"])
                k = (v.st >= sec["start"]) & (v.st < sec["end"])
                if k.any():
                    bass.append(float(np.median(v.bass[k])))
                silences += sum(
                    1 for s in v.silences["-40dB_0.5s"] if sec["start"] <= s["start_s"] < sec["end"]
                )
                secs += sec["end"] - sec["start"]
        out["by_section"][kind] = {
            "momentary_median_lufs": _r(np.median(med), 1) if med else None,
            "momentary_p10_lufs": _r(np.median(p10), 1) if p10 else None,
            "word_gap_level_lufs": _r(np.median(gaps), 1) if gaps else None,
            "word_gaps": len(gaps),
            "bass_fraction_median": _r(np.median(bass)) if bass else None,
            "silences_40db_per_min": _r(60.0 * silences / secs, 2) if secs else None,
        }
    for v in videos:
        out["programme"][v.id] = v.summary["loudness_summary"]
    ints = [v.summary["loudness_summary"]["integrated_lufs"] for v in videos]
    peaks = [v.summary["loudness_summary"]["true_peak_dbfs"] for v in videos]
    out["integrated_lufs_median"] = _r(np.median(ints), 1)
    out["true_peak_dbfs_max"] = _r(max(peaks), 1)
    return out


def _gap_levels(v: Video, a: float, b: float) -> list[float]:
    """Momentary loudness inside speech pauses: what plays under the talk."""
    w = v.words[(v.words >= a) & (v.words < b)]
    out = []
    for x, y in zip(w, w[1:]):
        lo, hi = x + 0.45, y - 0.1
        if 0.6 <= hi - lo < 4.0:
            m = v.mom(lo + 0.4, hi)
            if m.size:
                out.append(float(np.median(m)))
    return out


def transitions(videos: list[Video]) -> dict:
    """Music in/out at boundaries between music-led and speech sections."""
    events = []
    for v in videos:
        sm = v.smooth(5)
        for left, right in zip(v.sections, v.sections[1:]):
            lm_, rm_ = left["type"] in MUSIC_TYPES, right["type"] in MUSIC_TYPES
            if lm_ == rm_:
                continue
            if left.get("bed") or right.get("bed"):
                continue
            b = right["start"]
            music_side = v.mom(*((b - 3.5, b - 0.5) if lm_ else (b + 0.5, b + 3.5)))
            other_side = v.mom(*((b + 1.0, b + 5.0) if lm_ else (b - 5.0, b - 1.0)))
            if not music_side.size or not other_side.size:
                continue
            Lm, Lo = float(np.median(music_side)), float(np.median(other_side))
            if Lm - Lo < 6.0:
                continue
            k = np.where((v.lt >= b - 6) & (v.lt <= b + 6))[0]
            hi_t, lo_t = _crossings(v.lt[k], sm[k], Lm - 1.0, Lo + 1.0, falling=lm_)
            if hi_t is None:
                continue
            events.append({
                "video": v.id,
                "at": _r(b),
                "direction": "music_out" if lm_ else "music_in",
                "music_lufs": _r(Lm, 1),
                "other_lufs": _r(Lo, 1),
                "ramp_s": _r(abs(lo_t - hi_t), 2),
            })
    ramps_out = [e["ramp_s"] for e in events if e["direction"] == "music_out"]
    ramps_in = [e["ramp_s"] for e in events if e["direction"] == "music_in"]
    return {
        "events": events,
        "music_out_ramp_s": _dist(ramps_out),
        "music_in_ramp_s": _dist(ramps_in),
        "hard_share": _r(np.mean([e["ramp_s"] <= 0.5 for e in events])) if events else None,
    }


def _crossings(t, y, hi_level, lo_level, falling: bool):
    """Time of the last hi_level and first lo_level crossing on a ramp."""
    if falling:
        above = np.where(y >= hi_level)[0]
        if not above.size:
            return None, None
        i = above[-1] if above[-1] < len(y) - 1 else above[0]
        below = np.where((np.arange(len(y)) > i) & (y <= lo_level))[0]
        if not below.size:
            return None, None
        return float(t[i]), float(t[below[0]])
    below = np.where(y <= lo_level)[0]
    if not below.size:
        return None, None
    i = below[-1] if below[-1] < len(y) - 1 else below[0]
    above = np.where((np.arange(len(y)) > i) & (y >= hi_level))[0]
    if not above.size:
        return None, None
    return float(t[above[0]]), float(t[i])


def ducking(videos: list[Video]) -> dict:
    """Bed under speech: depth and timing where a bed exists."""
    events, beds = [], []
    for v in videos:
        montage_levels = [np.median(v.mom(s["start"], s["end"])) for s in v.sections if s["type"] == "montage"]
        montage_ref = float(np.median(montage_levels)) if montage_levels else None
        for i, sec in enumerate(v.sections):
            if sec["type"] == "monologue" or sec.get("bed"):
                gaps = _gap_levels(v, sec["start"], sec["end"])
                beds.append({
                    "video": v.id,
                    "section": sec["type"],
                    "start": sec["start"],
                    "bed_in_word_gaps_lufs": _r(np.median(gaps), 1) if gaps else None,
                    "montage_level_lufs": _r(montage_ref, 1),
                    "bed_below_montage_lu": _r(montage_ref - np.median(gaps), 1) if gaps and montage_ref is not None else None,
                })
            if not sec.get("bed") or i == 0:
                continue
            start = sec["start"]
            words = v.words[(v.words >= start - 3) & (v.words < start + 10)]
            if not words.size:
                continue
            first_word = float(words[0])
            before = v.mom(first_word - 4.0, first_word - 1.5)
            if not before.size:
                continue
            pre = float(np.median(before))
            k = np.where((v.lt >= first_word - 3.0) & (v.lt <= first_word + 2.0))[0]
            drop = next((float(v.lt[j]) for j in k if v.lm[j] <= pre - 6.0), None)
            if drop is None:
                continue
            settle = next((float(v.lt[j]) for j in k if v.lt[j] >= drop and v.lm[j] <= pre - 8.0), drop)
            bed = float(np.percentile(v.mom(first_word, first_word + 25.0), 10))
            events.append({
                "video": v.id,
                "first_word": _r(first_word),
                "pre_lufs": _r(pre, 1),
                "drop_at": _r(drop),
                "lead_before_speech_s": _r(first_word - drop, 2),
                "attack_s_upper_bound": _r(settle - drop + 0.4, 2),
                "bed_p10_lufs": _r(bed, 1),
                "depth_db": _r(pre - bed, 1),
            })
    return {"duck_events": events, "beds": beds}


def edges(videos: list[Video]) -> dict:
    """How each video starts and ends in audio."""
    out = {}
    for v in videos:
        sm = v.smooth(5)
        tail = v.sections[-1]
        ref = float(np.median(v.mom(tail["start"], tail["start"] + 4.0)))
        k = np.where(v.lt >= tail["start"])[0]
        loud = [j for j in k if sm[j] >= ref - 3.0]
        fade = None
        if loud:
            j0 = loud[-1]
            quiet = next((j for j in k if j > j0 and sm[j] <= ref - 20.0), k[-1])
            fade = float(v.lt[quiet] - v.lt[j0])
        first_music = next((s["start"] for s in v.sections if s["type"] in MUSIC_TYPES), None)
        out[v.id] = {
            "opens_with": v.sections[0]["type"],
            "first_music_section_s": _r(first_music, 3),
            "tail_level_lufs": _r(ref, 1),
            "tail_fade_s": _r(fade, 2),
            "end_card_s": _r(tail["end"] - tail["start"], 2),
        }
    return out


def speech_across_cuts(videos: list[Video]) -> dict:
    """Share of cuts with ASR speech within 0.3 s on both sides (L/J or VO hint)."""
    out = {}
    for kind in ("talking", "confessional", "vlog", "broll", "monologue"):
        hits, total = 0, 0
        for v in videos:
            for sec in v.sections:
                if sec["type"] != kind:
                    continue
                for c in v.cuts["0p3"]:
                    if not sec["start"] < c < sec["end"]:
                        continue
                    total += 1
                    before = np.any((v.words > c - 0.6) & (v.words <= c))
                    after = np.any((v.words > c) & (v.words <= c + 0.6))
                    hits += bool(before and after)
        if total:
            out[kind] = {"cuts": total, "speech_spans_cut_share": _r(hits / total)}
    return out


def chapters(videos: list[Video], plug_under_s: float = 15.0) -> dict:
    """YouTube chapter lengths. A trailing chapter under 15 s is a link plug."""
    lengths, per_10min, titles = [], [], []
    for v in videos:
        real = [c for c in v.chapters if c["end_time"] - c["start_time"] >= plug_under_s]
        lengths += [c["end_time"] - c["start_time"] for c in real]
        per_10min.append(600.0 * len(real) / v.duration)
        titles += [c["title"] for c in v.chapters]
    return {
        "length_s": _dist(lengths),
        "per_10_min": _dist(per_10min),
        "lowercase_title_share": _r(np.mean([t == t.lower() for t in titles])),
    }


ENGINE_ROLES = {
    "talking": ("talking", "confessional", "vlog"),
    "montage": ("montage", "broll"),
    "title_card": ("chapter_card",),
    "end_card": ("end_card",),
}


def _role(v: Video, index: int) -> str:
    """Map a labelled section onto the assembly engine's section kinds.

    The first section is the intro, the last section before the end card is
    the outro, and the rest pool by ENGINE_ROLES. A monologue that is not the
    outro counts as montage (music-led).
    """
    sec = v.sections[index]
    last_body = max(i for i, s in enumerate(v.sections) if s["type"] != "end_card")
    if index == 0:
        return "intro"
    if index == last_body:
        return "outro"
    for role, kinds in ENGINE_ROLES.items():
        if sec["type"] in kinds:
            return role
    return "montage"


def roles(videos: list[Video]) -> dict:
    """Shot lengths, shares and levels per assembly-engine section kind."""
    shots: dict[str, list[float]] = {}
    seconds: dict[str, float] = {}
    lengths: dict[str, list[float]] = {}
    levels_: dict[str, list[float]] = {}
    total = sum(v.duration for v in videos)
    montage_ref = []
    for v in videos:
        m = [np.median(v.mom(s["start"], s["end"])) for s in v.sections if s["type"] == "montage"]
        montage_ref.append(float(np.median(m)))
        for i, sec in enumerate(v.sections):
            role = _role(v, i)
            seconds[role] = seconds.get(role, 0.0) + sec["end"] - sec["start"]
            lengths.setdefault(role, []).append(sec["end"] - sec["start"])
            cuts = [c for c in v.cuts["0p3"] if sec["start"] < c < sec["end"]]
            if sec["type"] in ("talking", "confessional"):
                cuts = sorted(set(cuts + jump_cut_spikes(v, sec["start"], sec["end"])))
            edges_ = [sec["start"]] + cuts + [sec["end"]]
            shots.setdefault(role, []).extend(b - a for a, b in zip(edges_, edges_[1:]))
            m = v.mom(sec["start"], sec["end"])
            if m.size:
                levels_.setdefault(role, []).append(float(np.median(m)) - montage_ref[-1])
    out = {}
    for role in ("intro", "talking", "montage", "outro", "title_card", "end_card"):
        dist = _dist(shots.get(role, []))
        out[role] = {
            "shot_length": dist,
            "share": _r(seconds.get(role, 0.0) / total),
            "seconds": _r(seconds.get(role, 0.0), 1),
            "section_seconds": _dist(lengths.get(role, [])),
            "level_vs_montage_lu": _r(np.median(levels_[role]), 1) if levels_.get(role) else None,
        }
    out["mapping"] = {
        "intro": "first labelled section",
        "outro": "last section before the end card",
        **{role: list(kinds) for role, kinds in ENGINE_ROLES.items()},
    }
    out["target_seconds_median"] = _r(np.median([v.duration for v in videos]), 1)
    return out


def summary(result: dict) -> dict:
    edges_ = [e for vid, e in result["edges"].items() if vid in LONG_VIDEOS]
    return {
        "end_card_s_median": _r(np.median([e["end_card_s"] for e in edges_]), 2),
        "tail_fade_s_median": _r(np.median([e["tail_fade_s"] for e in edges_ if e["tail_fade_s"] is not None]), 2),
        "talking_open_share": _r(np.mean([e["opens_with"] == "talking" for e in edges_])),
        "cuts_per_min_median": _r(np.median([p["0p3"]["cuts_per_min"] for p in result["rhythm"]["per_video"].values()]), 2),
        "shot_median_s_median": _r(np.median([p["0p3"]["median_shot_s"] for p in result["rhythm"]["per_video"].values()]), 2),
    }


# ---------------------------------------------------------------- frames


def _image():
    try:
        from PIL import Image
    except ImportError:
        return None
    return Image


def caption_box(path: Path, y0: float = 0.78, y1: float = 0.97) -> dict | None:
    Image = _image()
    im = np.asarray(Image.open(path).convert("RGB")).astype(int)
    H, W, _ = im.shape
    band = im[int(y0 * H):int(y1 * H)]
    white = (band.min(2) > 200) & (band.max(2) - band.min(2) < 40)
    trans = np.abs(np.diff(white.astype(int), axis=1)).sum(1)
    rows = np.where(trans >= 12)[0]
    if len(rows) < 3:
        return None
    runs, s, p = [], rows[0], rows[0]
    for r in rows[1:]:
        if r - p > 2:
            runs.append((s, p))
            s = r
        p = r
    runs.append((s, p))
    r0, r1 = max(runs, key=lambda x: x[1] - x[0])
    cols = np.where(white[r0:r1 + 1].sum(0) > 0)[0]
    if r1 - r0 < 2 or r1 - r0 > 0.07 * H or cols.size < 10:
        return None
    x0, x1 = np.percentile(cols, 1), np.percentile(cols, 99)
    box = {
        "y_top": (r0 + int(y0 * H)) / H,
        "y_bottom": (r1 + int(y0 * H)) / H,
        "x0": x0 / W,
        "x1": x1 / W,
        "text_h": (r1 - r0 + 1) / H,
    }
    box["cx"] = (box["x0"] + box["x1"]) / 2
    box["width"] = box["x1"] - box["x0"]
    ok = 0.84 <= box["y_top"] <= 0.87 and 0.012 <= box["text_h"] <= 0.032 and 0.4 <= box["cx"] <= 0.6
    return box if ok else None


def captions(bundle: Path, videos: list[Video], labels: dict) -> dict:
    by_id = {v.id: v for v in videos}
    out: dict = {"geometry": None, "presence": None}
    runs = labels["caption_runs_observed"]["runs"]
    durations, cps, chars = [], [], []
    for run in runs:
        est = run["last"] - run["first"] + 2.0
        durations.append(est)
        chars.append(len(run["text"]))
        cps.append(len(run["text"]) / est)
        v = by_id[run["video"]]
        run["asr_words"] = int(np.sum((v.words >= run["first"] - 1) & (v.words <= run["last"] + 1)))
    out["narration_runs"] = {
        "duration_s_estimate": _dist(durations),
        "chars": _dist(chars),
        "chars_per_s": _dist(cps),
        "asr_words_during_runs": [r["asr_words"] for r in runs],
        "sampling_note": "2 s frame spacing: each duration is +/-2 s",
    }
    if _image() is None:
        return out
    geo: dict = {}
    for item in labels["caption_frames"]:
        box = caption_box(bundle / "frames" / item["video"] / item["frame"])
        if box is None:
            continue
        box["chars"] = len(item["text"])
        box["width_per_char"] = box["width"] / len(item["text"])
        geo.setdefault(item["mode"], []).append(box)
    out["geometry"] = {
        mode: {key: _r(np.median([b[key] for b in boxes]), 4) for key in ("y_top", "y_bottom", "text_h", "cx", "width", "width_per_char", "chars")}
        | {"n": len(boxes), "width_max": _r(max(b["width"] for b in boxes), 4)}
        for mode, boxes in geo.items()
    }
    presence: dict = {}
    for v in videos:
        for folder in ("intro_every2s", "outro_every2s"):
            for path in sorted((v.frames / folder).glob("*.jpg")):
                t = float(path.stem.split("_t")[1].rstrip("s"))
                sec = v.section_of(t)
                if sec is None:
                    continue
                entry = presence.setdefault(sec["type"], [0, 0])
                entry[1] += 1
                entry[0] += caption_box(path) is not None
    out["presence"] = {k: {"frames": n, "with_caption_share": _r(hit / n)} for k, (hit, n) in sorted(presence.items())}
    return out


def cards(videos: list[Video], labels: dict) -> dict:
    by_id = {v.id: v for v in videos}
    items = labels["cards"]
    lead = [c["card_start"] - c["glitch_start"] for c in items]
    hold = [c["end"] - c["card_start"] for c in items]
    total = [c["end"] - c["glitch_start"] for c in items]
    audio, before = [], []
    for c in items:
        v = by_id[c["video"]]
        audio.append(float(np.median(v.mom(c["glitch_start"], c["end"]))))
        before.append(float(np.median(v.mom(c["glitch_start"] - 4.0, c["glitch_start"] - 0.5))))
    return {
        "chapter_card_glitch_lead_s": _dist(lead),
        "chapter_card_hold_s": _dist(hold),
        "chapter_card_total_s": _dist(total),
        "chapter_card_audio_lufs": _dist(audio),
        "audio_before_card_lufs": _dist(before),
        "n": len(items),
    }


def palette(bundle: Path, labels: dict) -> dict | None:
    Image = _image()
    if Image is None:
        return None
    out: dict = {"rect": [], "field": []}
    for item in labels["rect_samples"]:
        im = Image.open(bundle / "frames" / item["video"] / item["frame"]).convert("RGB")
        W, H = im.size
        for x, y, role in item["points"]:
            px = [
                im.getpixel((min(W - 1, max(0, int(x * W) + dx)), min(H - 1, max(0, int(y * H) + dy))))
                for dx in range(-3, 4)
                for dy in range(-3, 4)
            ]
            rgb = [int(np.median([p[c] for p in px])) for c in range(3)]
            out[role].append("#%02x%02x%02x" % tuple(rgb))
    return out


def slam(bundle: Path, labels: dict) -> list | None:
    Image = _image()
    if Image is None:
        return None
    out = []
    for item in labels["slam_text"]:
        im = np.asarray(Image.open(bundle / "frames" / item["video"] / item["frame"]).convert("RGB")).astype(int)
        H, W, _ = im.shape
        mask = np.abs(im - np.array(item["rgb"])).max(2) < item["tol"]
        ys, xs = np.where(mask)
        y0, y1 = np.percentile(ys, 0.5), np.percentile(ys, 99.5)
        x0, x1 = np.percentile(xs, 0.5), np.percentile(xs, 99.5)
        colour = np.median(im[mask], 0).astype(int)
        out.append({
            "text": item["text"],
            "video": item["video"],
            "height_frac": _r((y1 - y0) / H),
            "width_frac": _r((x1 - x0) / W),
            "glyph_height_over_advance": _r(((y1 - y0) / item["lines"]) / ((x1 - x0) / item["chars"]), 2),
            "colour": "#%02x%02x%02x" % tuple(colour),
        })
    return out


# ---------------------------------------------------------------- main


def analyze(bundle: Path, labels_path: Path = HERE / "sections.json") -> dict:
    labels = json.loads(labels_path.read_text())
    videos = [Video(bundle, vid, labels["videos"][vid]) for vid in LONG_VIDEOS]
    short = Video(bundle, "phS28hhJSP8", labels["videos"]["phS28hhJSP8"])
    result = {
        "schema": "byjustinwu.measured/1",
        "videos": {v.id: {"duration_s": v.duration} for v in videos + [short]},
        "pooled_over": list(LONG_VIDEOS),
        "rhythm": rhythm(videos),
        "beat_alignment": beat_alignment(videos),
        "levels": levels(videos),
        "transitions": transitions(videos),
        "ducking": ducking(videos),
        "edges": edges(videos + [short]),
        "speech_across_cuts": speech_across_cuts(videos),
        "captions": captions(bundle, videos, labels),
        "cards": cards(videos, labels),
        "palette": palette(bundle, labels),
        "slam": slam(bundle, labels),
        "short_video": {
            "phS28hhJSP8": {
                "median_shot_s": short.summary["shots"]["0p3"]["shot_length_s"]["median"],
                "cuts_per_min": short.summary["shots"]["0p3"]["cuts_per_minute"],
                "loudness": short.summary["loudness_summary"],
            }
        },
        "chapters": chapters(videos),
        "roles": roles(videos),
    }
    result["summary"] = summary(result)
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--bundle", required=True, type=Path)
    parser.add_argument("--labels", type=Path, default=HERE / "sections.json")
    parser.add_argument("--out", type=Path)
    args = parser.parse_args(argv)
    if not (args.bundle / "data").is_dir():
        print(f"no data/ folder under {args.bundle}", file=sys.stderr)
        return 2
    result = analyze(args.bundle, args.labels)
    text = json.dumps(result, indent=1, sort_keys=True) + "\n"
    if args.out:
        args.out.write_text(text, encoding="utf-8")
    else:
        sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
