#!/usr/bin/env python3
"""Is Jev calibrated on *your* footage? Check against a cut you made by hand.

The review queue sorts by confidence ascending, which is only meaningful if
confidence means something: of the lines the model kept at 0.9, about 90%
should be lines you kept too. Until that holds, the queue ordering is an
assumption, not a feature.

A project you have already cut is a free labeled dataset and better evidence
than any vendor benchmark. Point this at the raw footage, the brief you would
have written, and the EDL of your finished cut:

    python eval/calibrate.py footage.mp4 "a 3-minute explainer" hand-cut.edl

Exits non-zero if any decile is off by more than `--tolerance` points.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from cutmcp import assemble, decide, extract  # noqa: E402

_TC = re.compile(r"\d{2}:\d{2}:\d{2}[:;]\d{2}")
_DECILES = [(i / 10, (i + 1) / 10) for i in range(10)]


def parse_edl(path: Path, fps: int) -> list[tuple[float, float]]:
    """Source in/out ranges from a CMX3600 EDL, merged and sorted."""
    ranges: list[tuple[float, float]] = []
    for line in path.read_text().splitlines():
        if not line[:3].isdigit():
            continue
        tcs = _TC.findall(line)
        if len(tcs) < 4:
            continue
        a, b = (assemble._untc(t.replace(";", ":"), fps) for t in tcs[:2])
        if b > a:
            ranges.append((a, b))
    if not ranges:
        raise SystemExit(f"no events parsed from {path}; is it a CMX3600 EDL?")
    ranges.sort()
    merged = [ranges[0]]
    for a, b in ranges[1:]:
        if a <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], b))
        else:
            merged.append((a, b))
    return merged


def covered(seg, ranges: list[tuple[float, float]]) -> float:
    """Fraction of a segment the human kept."""
    if seg.duration <= 0:
        return 0.0
    hit = sum(max(0.0, min(seg.end, b) - max(seg.start, a)) for a, b in ranges)
    return hit / seg.duration


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("media")
    ap.add_argument("brief")
    ap.add_argument("ground_truth", type=Path, help="EDL of the cut you made by hand")
    ap.add_argument("--target", type=float, default=None,
                    help="seconds; defaults to the duration of the hand cut")
    ap.add_argument("--cut-penalty", type=float, default=0.6)
    ap.add_argument("--fps", type=int, default=24)
    ap.add_argument("--tolerance", type=float, default=15.0, help="points")
    ap.add_argument("--min-bucket", type=int, default=10,
                    help="deciles thinner than this are reported but not gated")
    args = ap.parse_args()

    ranges = parse_edl(args.ground_truth, args.fps)
    human_duration = sum(b - a for a, b in ranges)
    target = args.target if args.target is not None else human_duration

    media = extract.ingest(args.media)
    decs = decide.run(media, args.brief)
    timeline = assemble.build(media, decs, target, args.brief, cut_penalty=args.cut_penalty)

    by_idx = {d.idx: d for d in decs}
    kept = {i for c in timeline.clips for i in range(c.first_segment, c.last_segment + 1)}

    # One row per line: the call the pipeline made, the model's own stated
    # probability that the call is right, and whether the human agreed.
    #
    # Confidence is folded onto the call rather than read off p(keep) raw,
    # because "belongs in a cut" and "survived into a 168-second cut" are
    # different events — a good line still gets dropped when the budget runs
    # out. Folding keeps the two base rates comparable, and it is honest
    # about budget-forced calls: when the DP drops a line the model scored at
    # 0.9, that call goes in the 0.1 bucket, where it should be wrong often.
    rows = []
    for s in media.segments:
        call = s.idx in kept
        p = by_idx[s.idx].keep
        rows.append((p if call else 1.0 - p, call == (covered(s, ranges) >= 0.5)))

    print(f"\nfootage      {media.path}")
    print(f"brief        {args.brief!r}")
    print(f"hand cut     {len(ranges)} clips, {human_duration:.1f}s")
    print(f"cutmcp       {len(timeline.clips)} clips, {timeline.duration:.1f}s "
          f"(target {target:.1f}s, cut_penalty {args.cut_penalty})")
    print("\ncalibration — of the calls made at 0.9, did ~90% match the human?\n")
    print(f"  {'decile':<14}{'n':>6}{'confidence':>12}{'agreement':>13}{'gap':>10}")
    print("  " + "-" * 55)

    worst = 0.0
    failures = []
    for lo, hi in _DECILES:
        bucket = [r for r in rows if lo <= r[0] < hi or (hi == 1.0 and r[0] == 1.0)]
        if not bucket:
            print(f"  [{lo:.1f},{hi:.1f}){'':<5}{0:>6}{'—':>12}{'—':>13}{'—':>10}")
            continue
        predicted = sum(r[0] for r in bucket) / len(bucket)
        observed = sum(1 for r in bucket if r[1]) / len(bucket)
        gap = (observed - predicted) * 100
        thin = len(bucket) < args.min_bucket
        flag = " thin" if thin else ("  FAIL" if abs(gap) > args.tolerance else "")
        print(f"  [{lo:.1f},{hi:.1f}){'':<5}{len(bucket):>6}{predicted:>12.3f}"
              f"{observed:>13.3f}{gap:>+9.1f}{flag}")
        if not thin:
            worst = max(worst, abs(gap))
            if abs(gap) > args.tolerance:
                failures.append((lo, gap))

    overlap = sum(
        max(0.0, min(c.end, b) - max(c.start, a)) for c in timeline.clips for a, b in ranges
    )
    agree = sum(1 for _, ok in rows if ok)
    print(f"\n  segment agreement   {agree / len(rows):.1%} of {len(rows)} lines")
    print(f"  timeline overlap    {overlap:.1f}s of the {human_duration:.1f}s hand cut "
          f"({overlap / human_duration:.1%})")

    if failures:
        print(f"\nFAIL — {len(failures)} decile(s) off by more than {args.tolerance:.0f} points "
              f"(worst {worst:.1f}). The review queue ordering is not trustworthy on this "
              f"footage: low confidence is not tracking low accuracy.\n")
        return 1
    print(f"\nPASS — every populated decile within {args.tolerance:.0f} points "
          f"(worst {worst:.1f}). Review queue ordering is supported by this project.\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
