#!/usr/bin/env python3
"""End-to-end dry-run of the sample interview. No API key.

    python scripts/dry_run.py
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from conductor.fcpxml import parse_fcpxml  # noqa: E402
from conductor.run import analyze  # noqa: E402

BRIEF = "A tight interview. Keep the guest's story, lose dead air."


def main() -> int:
    source = ROOT / "fixtures" / "sample_interview.fcpxml"
    transcript = ROOT / "fixtures" / "sample_interview.srt"
    out = ROOT / "out" / "sample-dry-run"
    before = source.read_bytes()
    report = analyze(
        source,
        transcript_path=transcript,
        brief=BRIEF,
        out_dir=out,
        live=False,
        html=True,
    )
    if source.read_bytes() != before:
        raise SystemExit("dry-run modified the source FCPXML")
    kinds = {item["kind"] for item in report.candidates}
    missing = {"silence_gap", "short_clip", "long_static", "filler_pause"} - kinds
    if missing:
        raise SystemExit(f"missing candidate kinds: {sorted(missing)}")
    eligible = [row for row in report.changes if row["section"] == "eligible"]
    if [row["candidate_id"] for row in eligible] != ["c0001"]:
        raise SystemExit(f"expected only the silence gap to be eligible, got {eligible}")
    if eligible[0]["pass"] != "mechanical" or eligible[0]["action"] != "remove":
        raise SystemExit(f"eligible call changed: {eligible[0]}")
    review_passes = {row["pass"] for row in report.changes if row["section"] == "review"}
    if not {"dialogue", "pacing"} <= review_passes:
        raise SystemExit(f"creative passes should stay in review, got {review_passes}")
    if report.payload["applied"] or report.cuts_applied:
        raise SystemExit("dry-run wrote cuts")
    written = parse_fcpxml(report.out_fcpxml)
    cold = written.sequences[0].spine[0]
    if cold.name != "Cold open" or cold.duration != 8 or not any(
        marker.value == "Keep this" for marker in cold.markers
    ):
        raise SystemExit("shadow file changed the cold open or dropped the human marker")
    if not report.out_json or not report.out_markdown or not report.out_html or not report.out_taste:
        raise SystemExit("missing report files")
    print(
        f"dry-run ok: {len(report.candidates)} candidates, "
        f"{report.marker_count} markers, 1 eligible mechanical cut"
    )
    print(f"  shadow  {report.out_fcpxml}")
    print(f"  report  {report.out_markdown}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
