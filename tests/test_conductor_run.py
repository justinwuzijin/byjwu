"""Shadow markers preserve the cut. Apply writes a different file."""

from __future__ import annotations

from fractions import Fraction
from pathlib import Path

import pytest

from conductor.cli import main
from conductor.errors import ConductorError
from conductor.fcpxml import parse_fcpxml
from conductor.markers import conductor_marker_count
from conductor.run import analyze

FIXTURE = Path("fixtures/sample_interview.fcpxml")
SRT = Path("fixtures/sample_interview.srt")
BRIEF = "A tight interview. Keep the guest's story, lose dead air."


def _run(tmp_path: Path, **kwargs):
    before = FIXTURE.read_bytes()
    report = analyze(
        FIXTURE,
        transcript_path=SRT,
        brief=BRIEF,
        out_dir=tmp_path,
        **kwargs,
    )
    assert FIXTURE.read_bytes() == before
    return report


def _spine(path: Path):
    return parse_fcpxml(path).sequences[0].spine


def test_shadow_adds_markers_and_keeps_every_edit(tmp_path):
    report = _run(tmp_path)
    assert report.mode == "dry-run"
    assert report.payload["applied"] is False
    assert report.payload["shadow"] is True
    assert report.cuts_applied == 0
    original = _spine(FIXTURE)
    shadow = _spine(report.out_fcpxml)
    assert [(c.name, c.offset, c.start, c.duration, c.ref) for c in shadow] == [
        (c.name, c.offset, c.start, c.duration, c.ref) for c in original
    ]
    cold = shadow[0]
    assert [marker.value for marker in cold.markers if not marker.value.startswith("CC ")] == [
        "Keep this"
    ]
    assert cold.markers[0].note == "human marker"
    assert cold.connected_clips[0].name == "Lower third"
    guest = next(clip for clip in shadow if clip.name == "Guest explains")
    assert guest.element is not None
    keywords = [child.get("value") for child in guest.element if child.tag == "keyword"]
    assert keywords == ["interview"]
    assert conductor_marker_count(parse_fcpxml(report.out_fcpxml).tree.getroot()) == 7
    gap = next(clip for clip in shadow if clip.name == "Gap")
    proposal = [marker for marker in gap.markers if marker.value.startswith("CC ")]
    assert len(proposal) == 1
    assert proposal[0].completed is None
    assert "color=red" in proposal[0].note
    assert "disposition=auto" in proposal[0].note
    flash = next(clip for clip in shadow if clip.name == "Flash frame")
    review = flash.markers[0]
    assert review.completed == "0"
    assert review.value.startswith("CC review")


def test_ranked_report_keeps_creative_calls_in_review(tmp_path):
    report = _run(tmp_path)
    eligible = [row for row in report.changes if row["section"] == "eligible"]
    review = [row for row in report.changes if row["section"] == "review"]
    assert [row["candidate_id"] for row in eligible] == ["c0002"]
    assert eligible[0]["kind"] == "silence_gap"
    assert eligible[0]["action"] == "remove" and eligible[0]["pass"] == "mechanical"
    assert eligible[0]["timecode"] == "00:00:08:00"
    colour = next(row for row in review if row["pass"] == "colour")
    assert colour["candidate_id"] == "c0001"
    assert colour["raw_action"] == "mark_review" and colour["disposition"] == "review"
    assert [row["candidate_id"] for row in review] == [
        "c0003",
        "c0001",
        "c0005",
        "c0006",
        "c0004",
        "c0007",
    ]
    assert {row["pass"] for row in review} >= {"dialogue", "pacing", "mechanical", "colour"}
    jev_receipt, opus_receipt = report.payload["receipts"]
    assert jev_receipt["engine"] == "jev" and opus_receipt["engine"] == "opus"
    assert jev_receipt["state"]["taste"]["prefs"]["target_pace"] == "measured"
    assert "c0001_action" not in jev_receipt["questions"]
    assert [item["id"] for item in opus_receipt["state"]["items"]] == ["c0001"]
    assert colour["engine"] == "opus" and colour["decision_type"] == "colour_unseen"
    veto = jev_receipt["questions"]["c0002_veto"]
    assert set(veto["criteria"]) >= {"allow", "veto_story", "veto_breath"}
    open_action = next(
        spec for key, spec in jev_receipt["questions"].items() if key.endswith("_action")
    )
    assert set(open_action["criteria"]) == {
        "keep",
        "tighten",
        "remove",
        "mark_review",
        "escalate",
    }
    text = report.out_markdown.read_text()
    assert "Eligible to apply" in text
    assert "No timeline edits were applied" in text


def test_running_on_the_shadow_file_does_not_duplicate_markers(tmp_path):
    first = _run(tmp_path / "a")
    second = analyze(
        first.out_fcpxml,
        transcript_path=SRT,
        brief=BRIEF,
        out_dir=tmp_path / "b",
    )
    assert second.marker_count == 0
    root = parse_fcpxml(second.out_fcpxml).tree.getroot()
    assert conductor_marker_count(root) == 7


def test_confidence_apply_removes_only_the_gap(tmp_path):
    report = _run(tmp_path, apply=True, min_confidence=0.8, passes=["mechanical"])
    assert report.payload["applied"] is True
    assert report.cuts_applied == 1
    assert report.payload["cuts"][0]["candidate_id"] == "c0001"
    assert FIXTURE.read_bytes()  # source still exists
    applied = _spine(report.out_applied)
    assert [clip.name for clip in applied] == [
        "Cold open",
        "Flash frame",
        "Guest explains",
        "B-roll static hold",
        "Button",
    ]
    assert applied[0].markers[0].value == "Keep this"
    assert applied[0].connected_clips[0].name == "Lower third"
    assert applied[1].offset == Fraction(21, 2) - Fraction(5, 2)
    assert applied[2].offset == Fraction(65, 6) - Fraction(5, 2)
    assert applied[2].start == 100 and applied[2].duration == 18
    shadow = _spine(report.out_fcpxml)
    assert any(clip.name == "Gap" for clip in shadow)
    log = json_log(report.out_taste)
    assert log["log"][0]["event"] == "accept"
    assert log["log"][0]["candidate_id"] == "c0001"
    assert "Cuts were written" in report.out_markdown.read_text()


def test_accept_lifts_a_review_filler(tmp_path):
    report = _run(tmp_path, apply=True, accept=["c0004"])
    applied = _spine(report.out_applied)
    guests = [clip for clip in applied if clip.name == "Guest explains"]
    assert len(guests) == 2
    head, tail = guests
    assert head.start == 100
    assert head.duration == Fraction(101, 30)
    assert tail.start == Fraction(3137, 30)
    assert head.offset == Fraction(65, 6)
    assert tail.offset == Fraction(71, 5)
    assert any(clip.name == "Gap" for clip in applied)


def test_accept_and_confidence_path_can_stack(tmp_path):
    report = _run(tmp_path, apply=True, accept=["c0002", "c0004"])
    applied = _spine(report.out_applied)
    assert "Gap" not in {clip.name for clip in applied}
    guests = [clip for clip in applied if clip.name == "Guest explains"]
    assert len(guests) == 2
    assert guests[0].offset == Fraction(65, 6) - Fraction(5, 2)


def test_creative_pass_is_not_auto_applied(tmp_path):
    with pytest.raises(ConductorError, match="no cuts matched"):
        _run(tmp_path, apply=True, min_confidence=0.8, passes=["dialogue"])
    with pytest.raises(ConductorError, match="no cuts matched"):
        _run(tmp_path, apply=True, min_confidence=0.8, passes=["colour"])


def test_apply_requires_an_explicit_selector(tmp_path):
    with pytest.raises(ConductorError, match="--accept"):
        _run(tmp_path, apply=True)


def test_unknown_accept_id_and_non_cut(tmp_path):
    with pytest.raises(ConductorError, match="unknown candidate"):
        _run(tmp_path, apply=True, accept=["c9999"])
    with pytest.raises(ConductorError, match="no cut to apply"):
        _run(tmp_path, apply=True, accept=["c0001"])


def test_loose_pace_drops_the_gap_out_of_auto(tmp_path, monkeypatch):
    monkeypatch.setenv("CONDUCTOR_DECISION_MODE", "model-gated")
    taste = tmp_path / "taste.json"
    taste.write_text(
        '{"version": 1, "prefs": {"target_pace": "loose", "cold_open_bias": "neutral",'
        ' "jump_cut_tolerance": 0.5, "hold_seconds": 4}}'
    )
    report = _run(tmp_path / "out", taste_path=taste)
    eligible = [row for row in report.changes if row["section"] == "eligible"]
    assert eligible == []
    gap = next(row for row in report.changes if row["kind"] == "silence_gap")
    assert gap["section"] == "review"
    assert gap["raw_action"] == "tighten"


def test_cli_dry_run_and_feedback(tmp_path, monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    code = main(
        [
            "analyze",
            str(FIXTURE),
            "--transcript",
            str(SRT),
            "--brief",
            BRIEF,
            "--out-dir",
            str(tmp_path / "cli"),
            "--pass",
            "mechanical",
        ]
    )
    assert code == 0
    code = main(
        [
            "apply",
            str(FIXTURE),
            "--brief",
            BRIEF,
            "--out-dir",
            str(tmp_path / "nope"),
        ]
    )
    assert code == 2
    code = main(
        [
            "feedback",
            "--taste",
            "fixtures/taste.json",
            "--out",
            str(tmp_path / "taste.json"),
            "--event",
            "reject",
            "--id",
            "c0005",
            "--action",
            "tighten",
            "--pass",
            "pacing",
            "--note",
            "hold is the joke",
        ]
    )
    assert code == 0
    written = (tmp_path / "taste.json").read_text()
    assert "hold is the joke" in written
    assert "c0004" in written  # the sample reject came along


def test_live_without_a_key_is_a_usage_error(tmp_path, monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    monkeypatch.delenv("CONDUCTOR_DRY_RUN", raising=False)
    code = main(
        [
            "analyze",
            str(FIXTURE),
            "--brief",
            BRIEF,
            "--live",
            "--out-dir",
            str(tmp_path),
        ]
    )
    assert code == 2


def test_apply_closes_a_hole_and_trims_a_hold(tmp_path):
    from conductor.apply import Deletion, apply_edits
    from conductor.fcpxml import write_document

    hole = tmp_path / "hole.fcpxml"
    hole.write_text(
        """<?xml version="1.0" encoding="UTF-8"?>
        <fcpxml version="1.11">
          <resources><format id="r1" frameDuration="1/24s" width="1920" height="1080"/></resources>
          <project name="Hole">
            <sequence format="r1" duration="12s" tcStart="0s">
              <spine>
                <asset-clip ref="r2" offset="0s" name="A" start="0s" duration="4s"/>
                <asset-clip ref="r2" offset="7s" name="B" start="20s" duration="5s"/>
              </spine>
            </sequence>
          </project>
        </fcpxml>
        """
    )
    doc = parse_fcpxml(hole)
    apply_edits(
        doc,
        [Deletion("c0001", "Hole", Fraction(4), Fraction(7), "remove", "mechanical")],
    )
    out = tmp_path / "hole.out.fcpxml"
    write_document(doc.tree, out)
    spine = _spine(out)
    assert [clip.name for clip in spine] == ["A", "B"]
    assert spine[1].offset == 4 and spine[1].start == 20
    assert parse_fcpxml(out).sequences[0].duration == 9

    hold = tmp_path / "hold.fcpxml"
    hold.write_text(
        """<?xml version="1.0" encoding="UTF-8"?>
        <fcpxml version="1.11">
          <resources><format id="r1" frameDuration="1/24s" width="1920" height="1080"/></resources>
          <project name="Hold">
            <sequence format="r1" duration="10s" tcStart="0s">
              <spine>
                <asset-clip ref="r2" offset="0s" name="Static" start="3s" duration="10s"/>
              </spine>
            </sequence>
          </project>
        </fcpxml>
        """
    )
    doc = parse_fcpxml(hold)
    apply_edits(
        doc,
        [Deletion("c0005", "Hold", Fraction(4), Fraction(10), "tighten", "pacing")],
    )
    out = tmp_path / "hold.out.fcpxml"
    write_document(doc.tree, out)
    clip = _spine(out)[0]
    assert clip.duration == 4 and clip.start == 3 and clip.offset == 0


def json_log(path: Path) -> dict:
    import json

    return json.loads(path.read_text())
