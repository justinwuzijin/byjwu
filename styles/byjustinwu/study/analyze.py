"""Recompute the numeric byjustinwu style parameters from the study bundle.

Usage:
    python styles/byjustinwu/study/analyze.py [bundle] [--out measurements.json]

Study data never goes in git. It lives in ``study/data/`` beside this script,
which is gitignored: unzip the bundle's data part into ``study/data/bundle/``
(so ``study/data/bundle/data/<video_id>/summary.json`` exists), or pass
another folder as ``bundle``. Frames are not needed. Output is
``study/data/measurements.json`` unless ``--out`` is given. Every number in
``styles/byjustinwu/profile.json`` whose evidence starts with ``stat:`` names
a key in that file.

Everything here is deterministic arithmetic over the bundle's CSV/JSON. The
interpretation (what counts as a montage, what a card looks like) is in
``STYLE.md``; this only measures.

Window classes, per 0.5 s spectral window:
  speech  a real caption word (not ``[music]``/``Heat.``) starts within
          [t - 0.3, t + 0.8)
  music   not speech and mid level above -45 dBFS
  quiet   everything else

Section classes, per shot at scene threshold 0.3:
  talking   speech in >= 50% of the shot's windows
  broll     speech in < 50%
  montage   a run of >= 4 consecutive broll shots whose median length <= 2.5 s
  intro     shots starting in the first 30 s
  outro     shots starting in the last 30 s
Intro and outro override the other classes. YouTube auto-captions are the
only speech signal, so a source with no music tags, and sung lyrics, can
blur the split; STYLE.md lists that under limits.
"""

from __future__ import annotations

import argparse
import csv
import json
import statistics
from pathlib import Path

import numpy as np

DATA_DIR = Path(__file__).resolve().parent / "data"
LONG_FORM_S = 120.0
NON_WORDS = {"[music]", "heat.", "heat", "[applause]", "[laughter]", ""}
INTRO_S = 30.0
OUTRO_S = 30.0
MONTAGE_RUN = 4
MONTAGE_MEDIAN_S = 2.5
FLOOR_LUFS = -70.0


def _rows(path: Path) -> list[dict]:
    with path.open(newline="") as fh:
        return list(csv.DictReader(fh))


def _stats(values) -> dict | None:
    vals = sorted(float(v) for v in values)
    if not vals:
        return None
    arr = np.array(vals)
    return {
        "n": len(vals),
        "median": round(float(np.median(arr)), 3),
        "mean": round(float(arr.mean()), 3),
        "p10": round(float(np.percentile(arr, 10)), 3),
        "p90": round(float(np.percentile(arr, 90)), 3),
        "min": round(vals[0], 3),
        "max": round(vals[-1], 3),
    }


class Video:
    def __init__(self, root: Path, vid: str):
        d = root / "data" / vid
        self.id = vid
        self.summary = json.loads((d / "summary.json").read_text())
        meta = json.loads((d / "metadata.json").read_text())
        self.duration = float(self.summary["duration_s"])
        self.chapters = [float(c["start_time"]) for c in meta.get("chapters") or []]
        self.fps = _fps(meta, json.loads((d / "ffprobe.json").read_text()))
        self.shots = [
            (float(r["start_s"]), float(r["length_s"])) for r in _rows(d / "shots_thr0p3.csv")
        ]
        self.shots_fine = [
            (float(r["start_s"]), float(r["length_s"])) for r in _rows(d / "shots_thr0p15.csv")
        ]
        loud = _rows(d / "loudness_ebur128_100ms.csv")
        self.lt = np.array([float(r["time_s"]) for r in loud])
        self.mom = _floor([r["momentary_lufs_400ms"] for r in loud])
        self.st = _floor([r["shortterm_lufs_3s"] for r in loud])
        spec = _rows(d / "spectral_features_500ms.csv")
        self.wt = np.array([float(r["time_s"]) for r in spec])
        self.mid = np.array([float(r["rms_dbfs_mid"]) for r in spec])
        self.side = self.mid + np.array([float(r["side_to_mid_db"]) for r in spec])
        words_path = d / "transcript_words.csv"
        self.words = (
            np.array(
                [
                    float(r["start_s"])
                    for r in _rows(words_path)
                    if r["word"].replace("&gt;", "").strip().lower() not in NON_WORDS
                ]
            )
            if words_path.exists()
            else np.array([])
        )
        self.silences = json.loads((d / "silence_map.json").read_text())["silences"]
        self.speech = np.array(
            [bool(np.any((self.words >= t - 0.3) & (self.words < t + 0.8))) for t in self.wt]
        )
        self.music = ~self.speech & (self.mid > -45.0)

    def window_class(self, t0: float, t1: float) -> float:
        """Speech fraction of the 0.5 s windows inside [t0, t1)."""
        m = (self.wt >= t0) & (self.wt < max(t1, t0 + 0.5))
        return float(self.speech[m].mean()) if m.any() else 0.0

    def mom_at(self, t: float) -> float:
        """Momentary LUFS centred on t (the 400 ms block ending at t + 0.2)."""
        i = int(np.clip(np.searchsorted(self.lt, t + 0.2), 0, len(self.lt) - 1))
        return float(self.mom[i])


def _floor(values) -> np.ndarray:
    """LUFS column with digital silence ('nan', -120.7, -139) clamped to the floor."""
    arr = np.array([float(x) for x in values])
    return np.maximum(np.nan_to_num(arr, nan=FLOOR_LUFS), FLOOR_LUFS)


def _fps(meta: dict, probe: dict) -> float:
    for s in probe.get("streams", []):
        if s.get("codec_type") == "video" and s.get("avg_frame_rate", "0/0") != "0/0":
            n, d = s["avg_frame_rate"].split("/")
            return round(float(n) / float(d), 3)
    return float(meta.get("fps") or 0)


def classify_shots(v: Video) -> list[dict]:
    out = []
    for start, length in v.shots:
        frac = v.window_class(start, start + length)
        out.append({"start": start, "length": length, "speech": frac,
                    "cls": "talking" if frac >= 0.5 else "broll"})
    i = 0
    while i < len(out):
        j = i
        while j < len(out) and out[j]["cls"] in ("broll", "montage"):
            j += 1
        run = out[i:j]
        if len(run) >= MONTAGE_RUN and statistics.median(s["length"] for s in run) <= MONTAGE_MEDIAN_S:
            for s in run:
                s["cls"] = "montage"
        i = max(j, i + 1)
    for s in out:
        if s["start"] < INTRO_S:
            s["cls"] = "intro"
        elif s["start"] >= v.duration - OUTRO_S:
            s["cls"] = "outro"
    return out


def cut_rhythm(videos: list[Video]) -> dict:
    per_video, pooled = {}, {}
    montage_runs = []
    for v in videos:
        shots = classify_shots(v)
        by = {}
        for s in shots:
            by.setdefault(s["cls"], []).append(s["length"])
            pooled.setdefault(s["cls"], []).append(s["length"])
        per_video[v.id] = {
            "cuts_per_minute_thr0p3": v.summary["shots"]["0p3"]["cuts_per_minute"],
            "cuts_per_minute_thr0p15": v.summary["shots"]["0p15"]["cuts_per_minute"],
            "shot_length_s_by_section": {k: _stats(x) for k, x in sorted(by.items())},
        }
        run = []
        for s in shots + [{"cls": "end"}]:
            if s["cls"] == "montage":
                run.append(s)
            elif run:
                montage_runs.append({
                    "video": v.id, "start_s": round(run[0]["start"], 2),
                    "end_s": round(run[-1]["start"] + run[-1]["length"], 2),
                    "shots": len(run),
                    "median_shot_s": round(statistics.median(x["length"] for x in run), 3),
                    "fastest_shot_s": round(min(x["length"] for x in run), 3),
                })
                run = []
    return {
        "per_video": per_video,
        "pooled_long_form_shot_length_s_by_section": {k: _stats(x) for k, x in sorted(pooled.items())},
        "montage_runs": montage_runs,
    }


def _onsets(v: Video) -> np.ndarray:
    """Times of rising-loudness peaks: >= 1.5 LU rise per 100 ms, 200 ms apart."""
    rise = np.diff(v.mom, prepend=v.mom[0])
    peaks = []
    for i in range(1, len(rise) - 1):
        if rise[i] >= 1.5 and rise[i] >= rise[i - 1] and rise[i] > rise[i + 1]:
            t = v.lt[i] - 0.2
            if not peaks or t - peaks[-1] >= 0.2:
                peaks.append(t)
    return np.array(peaks)


def beat_alignment(videos: list[Video]) -> dict:
    """Share of music-section cuts with a loudness onset within +-100 ms, vs chance.

    Chance is the same cuts shifted by random offsets in [1, 5] s (fixed
    seed), so it has the same density of onsets around it.
    """
    rng = np.random.default_rng(7)
    out = {}
    hits_all = base_all = n_all = 0
    for v in videos:
        on = _onsets(v)
        cuts = [s["start"] for s in classify_shots(v)
                if s["start"] > 0 and s["cls"] in ("montage", "broll", "intro", "outro")
                and v.window_class(s["start"] - 1.0, s["start"] + 1.0) < 0.25]
        if not cuts or not len(on):
            continue
        cuts = np.array(cuts)
        # momentary is a trailing window; search a lag that best explains it
        best = max(
            ((lag, float(np.mean([np.any(np.abs(on - (c + lag)) <= 0.1) for c in cuts])))
             for lag in np.round(np.arange(-0.3, 0.31, 0.05), 2)),
            key=lambda x: x[1],
        )
        lag = best[0]
        shifted = cuts[None, :] + rng.uniform(1, 5, size=(200, len(cuts))) * rng.choice([-1, 1], size=(200, len(cuts)))
        chance = float(np.mean([np.any(np.abs(on - (c + lag)) <= 0.1) for c in shifted.ravel()]))
        out[v.id] = {"music_cuts": len(cuts), "onset_lag_s": lag,
                     "hit_rate": round(best[1], 3), "chance_rate": round(chance, 3),
                     "lift": round(best[1] / chance, 2) if chance else None}
        hits_all += best[1] * len(cuts)
        base_all += chance * len(cuts)
        n_all += len(cuts)
    out["pooled"] = {"music_cuts": n_all, "hit_rate": round(hits_all / n_all, 3),
                     "chance_rate": round(base_all / n_all, 3),
                     "lift": round(hits_all / base_all, 2)}
    return out


def fastest_broll(videos: list[Video], k: int = 5) -> dict:
    """Fastest run of k consecutive non-talking shots (median length), per video."""
    out = {}
    for v in videos:
        shots = [s for s in classify_shots(v) if s["cls"] != "talking"]
        best = None
        for i in range(len(shots) - k + 1):
            run = shots[i:i + k]
            if any(b["start"] - (a["start"] + a["length"]) > 0.05 for a, b in zip(run, run[1:])):
                continue
            med = statistics.median(s["length"] for s in run)
            if best is None or med < best[0]:
                best = (med, run[0]["start"], run[-1]["start"] + run[-1]["length"])
        if best:
            out[v.id] = {"median_shot_s": round(best[0], 3), "start_s": round(best[1], 2),
                         "end_s": round(best[2], 2)}
    return out


def talking_edits(videos: list[Video]) -> dict:
    """Jump-cut padding and split-edit hints at cuts inside talking sections.

    lead_in: cut to the first word after it. tail: last word before the cut to
    the cut. A cut with a word less than 0.15 s either side is 'mid-speech':
    audio runs across the picture change, the J/L-cut or b-roll-over-voice
    signature.
    """
    lead, tail, mid_speech, n = [], [], 0, 0
    for v in videos:
        if not len(v.words):
            continue
        shots = classify_shots(v)
        for a, b in zip(shots, shots[1:]):
            if a["cls"] != "talking" and b["cls"] != "talking":
                continue
            c = b["start"]
            after = v.words[v.words >= c]
            before = v.words[v.words < c]
            if not len(after) or not len(before):
                continue
            n += 1
            la, tb = float(after[0] - c), float(c - before[-1])
            if la < 0.15 or tb < 0.15:
                mid_speech += 1
            if la < 2.0:
                lead.append(la)
            if tb < 2.0:
                tail.append(tb)
    return {"cuts": n, "lead_in_s": _stats(lead), "last_word_to_cut_s": _stats(tail),
            "mid_speech_share": round(mid_speech / n, 3) if n else None,
            "note": "ASR word starts are +-~0.1 s; last_word_to_cut includes the word's own length"}


def music_under_speech(videos: list[Video]) -> dict:
    """How the music bed behaves when he starts and stops talking.

    Level gap: median mid level of music windows minus speech windows.
    Handoff step: the stereo side channel is the music proxy (his voice is
    near-mono). Duck events are >= 3 s of music windows then >= 2 s of speech;
    release events the reverse. Per event the step is the side level 1.5-3 s
    before the boundary minus 1-2.5 s after (0.5 s windows). Onset is the
    first window, relative to the first/last word, where the median curve has
    covered half the step.
    """
    out, duck_steps, rel_steps, duck_on, rel_on = {}, [], [], [], []
    for v in videos:
        if not len(v.words):
            continue
        sp, mu = v.speech, v.music
        ducks, rels = [], []
        for i in range(8, len(sp) - 8):
            if mu[i - 6:i].all() and sp[i:i + 4].all():
                ducks.append(i)
            if sp[i - 4:i].all() and mu[i:i + 6].all():
                rels.append(i)
        talking = [s for s in classify_shots(v) if s["cls"] == "talking"]
        s40 = v.silences.get("-40dB_0.5s", [])
        dry = sum(1 for s in talking
                  if any(s["start"] <= x["start_s"] < s["start"] + s["length"] for x in s40))
        entry = {
            "speech_windows_median_mid_dbfs": round(float(np.median(v.mid[sp])), 1),
            "music_windows_median_mid_dbfs": round(float(np.median(v.mid[mu])), 1),
            "speech_windows_median_side_dbfs": round(float(np.median(v.side[sp])), 1),
            "music_windows_median_side_dbfs": round(float(np.median(v.side[mu])), 1),
            "talking_shots_with_a_-40dB_silence": f"{dry}/{len(talking)}",
            "duck_events": len(ducks), "release_events": len(rels),
        }
        entry["music_minus_speech_mid_db"] = round(entry["music_windows_median_mid_dbfs"] - entry["speech_windows_median_mid_dbfs"], 1)
        entry["side_drop_under_speech_db"] = round(entry["music_windows_median_side_dbfs"] - entry["speech_windows_median_side_dbfs"], 1)
        grid = np.arange(-8, 8) * 0.5
        for label, idx, steps, onsets, word_ref in (
            ("duck", ducks, duck_steps, duck_on, lambda i: v.words[v.words >= v.wt[i] - 0.3][0]),
            ("release", rels, rel_steps, rel_on, lambda i: v.words[v.words < v.wt[i]][-1]),
        ):
            if not idx:
                continue
            curve = np.median(np.array([v.side[i - 8:i + 8] for i in idx]), axis=0)
            pre = float(np.median(curve[(grid >= -3) & (grid <= -1.5)]))
            post = float(np.median(curve[(grid >= 1) & (grid <= 2.5)]))
            step = pre - post if label == "duck" else post - pre
            half = (pre + post) / 2
            crossed = np.nonzero(curve <= half if label == "duck" else curve >= half)[0]
            crossed = crossed[grid[crossed] >= -3]
            offset = float(np.median([v.wt[i] - word_ref(i) for i in idx]))
            onset = round(float(grid[crossed[0]]) + offset, 1) if len(crossed) else None
            entry[f"{label}_side_step_db"] = round(step, 1)
            entry[f"{label}_half_step_s_rel_word"] = onset
            steps.append(step)
            if onset is not None:
                onsets.append(onset)
        out[v.id] = entry
    out["pooled"] = {
        "music_minus_speech_mid_db": _stats([out[k]["music_minus_speech_mid_db"] for k in out]),
        "side_drop_under_speech_db": _stats([out[k]["side_drop_under_speech_db"] for k in out]),
        "duck_side_step_db": _stats(duck_steps),
        "release_side_step_db": _stats(rel_steps),
        "duck_half_step_s_rel_first_word": _stats(duck_on),
        "release_half_step_s_rel_last_word": _stats(rel_on),
    }
    return out


def fades(videos: list[Video]) -> dict:
    """Head and tail ramps plus the music level of montages.

    head: first block above -50 LUFS to within 3 LU of the 5-15 s short-term
    median. tail: last time within 3 LU of the final 30-10 s median, to the
    last block above -50 LUFS.
    """
    out = {}
    for v in videos:
        m, t = v.mom, v.lt
        live = np.nonzero(m > -50)[0]
        first, last = t[live[0]], t[live[-1]]
        head_ref = float(np.median(v.st[(t >= 5) & (t <= 15)]))
        head_reach = t[(t >= first) & (m >= head_ref - 3)]
        end = v.duration
        tail_ref = float(np.median(v.st[(t >= end - 30) & (t <= end - 10)]))
        tail_hold = t[(t <= last) & (m >= tail_ref - 3)]
        out[v.id] = {
            "first_audio_s": round(float(first), 1),
            "head_ramp_s": round(float(head_reach[0] - first), 1) if len(head_reach) else None,
            "tail_ramp_s": round(float(last - tail_hold[-1]), 1) if len(tail_hold) else None,
            "last_audio_s": round(float(last), 1),
            "duration_s": v.duration,
            "integrated_lufs": v.summary["loudness_summary"]["integrated_lufs"],
            "lra_lu": v.summary["loudness_summary"]["lra_lu"],
            "true_peak_dbfs": v.summary["loudness_summary"]["true_peak_dbfs"],
        }
        mont = [s for s in classify_shots(v) if s["cls"] == "montage"]
        vals = [float(np.median(v.st[(t >= s["start"]) & (t < s["start"] + s["length"])]))
                for s in mont if np.any((t >= s["start"]) & (t < s["start"] + s["length"]))]
        spk = v.st[np.interp(t, v.wt, v.speech.astype(float)) > 0.5]
        out[v.id]["montage_shortterm_lufs_median"] = round(float(np.median(vals)), 1) if vals else None
        out[v.id]["speech_shortterm_lufs_median"] = round(float(np.median(spk)), 1) if len(spk) else None
        mus = v.st[np.interp(t, v.wt, v.music.astype(float)) > 0.5]
        out[v.id]["music_only_shortterm_lufs_median"] = round(float(np.median(mus)), 1) if len(mus) else None
    return out


def silences(videos: list[Video]) -> dict:
    out = {}
    for v in videos:
        s40 = v.silences.get("-40dB_0.5s", [])
        near = sum(1 for c in v.chapters[1:]
                   if any(s["start_s"] - 3 <= c <= s["end_s"] + 3 for s in s40))
        out[v.id] = {
            "silences_-40dB_0.5s": len(s40),
            "per_minute": round(len(s40) / (v.duration / 60), 2),
            "duration_s": _stats([s["duration_s"] for s in s40]),
            "chapter_starts_within_3s_of_silence": f"{near}/{max(len(v.chapters) - 1, 0)}",
        }
    return out


def digital_silences(v: Video, min_s: float = 0.5) -> list[tuple[float, float]]:
    """Spans where every 400 ms momentary block is digital silence (at the floor).

    A block ending at t covers [t - 0.4, t], so a run of floor blocks from t0
    to t1 means silence over [t0 - 0.4, t1]. The first second is skipped
    because ebur128 reports the floor until its first window fills.
    """
    spans, start, prev = [], None, None
    for t, m in zip(v.lt, v.mom):
        if m <= FLOOR_LUFS and t > 1.0:
            start = t if start is None else start
            prev = t
        elif start is not None:
            if prev - start + 0.4 >= min_s:
                spans.append((round(float(start - 0.4), 2), round(float(prev), 2)))
            start = None
    return spans


def chapter_cards(videos: list[Video]) -> dict:
    """Fine-threshold shots near each chapter start, plus digital silences.

    Shots listed start 3 s before to 4 s after the chapter time (the wipe and
    the card). ``silent_during`` is the digital-silence span that overlaps
    those shots, if any.
    """
    out = {}
    for v in videos:
        rows = []
        silent = digital_silences(v)
        for c in v.chapters:
            near = [(round(s, 3), round(l, 3), round(l * v.fps)) for s, l in v.shots_fine if c - 3 <= s <= c + 4]
            lo = min((s for s, _, _ in near), default=c)
            hi = max((s + l for s, l, _ in near), default=c + 4)
            hit = [sp for sp in silent if sp[0] < hi and sp[1] > lo]
            rows.append({"chapter_s": c, "shots_start_len_frames": near,
                         "silent_during": hit})
        out[v.id] = {"fps": v.fps, "digital_silences": silent, "chapters": rows}
    lengths = []
    for v in videos:
        starts = list(v.chapters) + [v.duration]
        lengths += [b - a for a, b in zip(starts, starts[1:])]
    out["chapter_length_s"] = _stats(lengths)
    return out


def speech_rate(videos: list[Video]) -> dict:
    out = {}
    for v in videos:
        if not len(v.words):
            continue
        speech_s = float(v.speech.sum() * 0.5)
        out[v.id] = {"words": int(len(v.words)), "speech_seconds": speech_s,
                     "words_per_speech_second": round(len(v.words) / speech_s, 2) if speech_s else None,
                     "speech_share": round(float(v.speech.mean()), 3)}
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("bundle", type=Path, nargs="?", default=DATA_DIR / "bundle",
                    help="unzipped study bundle (contains data/<video_id>/); default study/data/bundle")
    ap.add_argument("--out", type=Path, default=DATA_DIR / "measurements.json")
    args = ap.parse_args(argv)
    found = sorted(p.name for p in (args.bundle / "data").iterdir() if (p / "summary.json").is_file())
    if not found:
        raise SystemExit(f"no data/<id>/summary.json under {args.bundle}")
    all_videos = [Video(args.bundle, vid) for vid in found]
    long_form = [v for v in all_videos if v.duration >= LONG_FORM_S]
    result = {
        "bundle_videos": found,
        "method": {"intro_s": INTRO_S, "outro_s": OUTRO_S, "montage_run": MONTAGE_RUN,
                   "montage_median_s": MONTAGE_MEDIAN_S, "shot_threshold": 0.3},
        "cut_rhythm": cut_rhythm(long_form),
        "short_form": {v.id: v.summary["shots"] for v in all_videos if v.duration < LONG_FORM_S},
        "beat_alignment": beat_alignment(all_videos),
        "fastest_broll_5_shots": fastest_broll(long_form),
        "talking_edits": talking_edits(long_form),
        "music_under_speech": music_under_speech(long_form),
        "fades_and_levels": fades(all_videos),
        "silences": silences(all_videos),
        "chapter_cards": chapter_cards(long_form),
        "speech_rate": speech_rate(long_form),
    }
    args.out.write_text(json.dumps(result, indent=2) + "\n")
    print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
