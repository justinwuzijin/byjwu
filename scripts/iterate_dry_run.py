#!/usr/bin/env python3
"""Dry-run the iterate loop on the sample interview. No API key.

    python scripts/iterate_dry_run.py
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from conductor.fcpxml import parse_fcpxml  # noqa: E402
from conductor.iterate import iterate  # noqa: E402

BRIEF = "A tight interview. Keep the guest's story, lose dead air."


def main() -> int:
    source = ROOT / "fixtures" / "sample_interview.fcpxml"
    transcript = ROOT / "fixtures" / "sample_interview.srt"
    out = ROOT / "out" / "iterate-dry-run"
    before = source.read_bytes()
    result = iterate(
        fcpxml=source,
        transcript_path=transcript,
        brief=BRIEF,
        out_dir=out,
        max_rounds=5,
    )
    if source.read_bytes() != before:
        raise SystemExit("iterate modified the source FCPXML")
    if result.stop_reason != "no-progress" or len(result.rounds) < 2:
        raise SystemExit(
            f"expected at least two rounds then no-progress, got {result.stop_reason} "
            f"after {len(result.rounds)}"
        )
    applied = result.rounds[0].get("applied_fcpxml")
    if not applied or result.rounds[0]["cuts"][0]["pass"] != "mechanical":
        raise SystemExit(f"round 1 should auto-apply a mechanical cut, got {result.rounds[0]}")
    if any(cut["pass"] != "mechanical" for cut in result.applied):
        raise SystemExit(f"iterate applied a non-mechanical cut: {result.applied}")
    spine = parse_fcpxml(applied).sequences[0].spine
    if any(clip.name == "Gap" for clip in spine):
        raise SystemExit("the applied file still contains the gap")
    if result.rounds[1]["applied_ids"]:
        raise SystemExit("round 2 applied another cut")
    print(
        f"iterate dry-run ok: {len(result.rounds)} rounds, stop {result.stop_reason}, "
        f"{len(result.applied)} mechanical cut(s)"
    )
    print(f"  json  {result.out_json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
