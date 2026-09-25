"""Taste learns from a re-export and from notes a room bot writes."""

from __future__ import annotations

import json
from fractions import Fraction
from pathlib import Path

import pytest

from conductor.cli import main
from conductor.errors import ConductorError
from conductor.feedback import diff_fcpxml, parse_at
from conductor.iterate import iterate
from conductor.run import analyze
from conductor.taste import load_taste, write_taste

FIXTURE = Path("fixtures/sample_interview.fcpxml")
SRT = Path("fixtures/sample_interview.srt")
PROPOSED = Path("fixtures/feedback/proposed.fcpxml")
EDITED = Path("fixtures/feedback/edited.fcpxml")
NOTES = Path("fixtures/feedback/notes.json")
BRIEF = "A tight interview. Keep the guest's story, lose dead air."


def _silence(report):
    return next(row for row in report.changes if row["kind"] == "silence_gap")


def _write_log(path: Path, events: list[dict], rules: list[dict] | None = None) -> None:
    payload = {"version": 1, "prefs": {}, "gates": {}, "log": events}
    if rules:
        payload["rules"] = rules
    path.write_text(json.dumps(payload), encoding="utf-8")


def test_diff_fixture_accepts_rejects_modifies_and_extras():
    events, warnings = diff_fcpxml(PROPOSED, EDITED)
    assert warnings == []
    by_id = {event["candidate_id"]: event for event in events if event["event"] != "extra"}
    assert by_id["c0002"]["event"] == "accept"
    assert by_id["c0002"]["kind"] == "short_clip"
    assert by_id["c0001"]["event"] == "reject"
    assert by_id["c0001"]["kind"] == "silence_gap"
    assert by_id["c0003"]["event"] == "modify"
    assert by_id["c0003"]["kind"] == "long_static"
    assert "c0004" not in by_id
    extras = [event for event in events if event["event"] == "extra"]
    assert len(extras) == 1
    assert extras[0]["kind"] == "editor_cut"
    assert extras[0]["clip_name"] == "Button"
    again, _ = diff_fcpxml(PROPOSED, EDITED)
    assert [event["fingerprint"] for event in again] == [event["fingerprint"] for event in events]


def test_analyze_learns_from_the_reexport_before_it_judges(tmp_path):
    report = analyze(
        EDITED,
        brief=BRIEF,
        out_dir=tmp_path,
        learn_from=PROPOSED,
        passes=["mechanical"],
    )
    learned = report.payload["learned"]
    assert any(event["event"] == "reject" and event["kind"] == "silence_gap" for event in learned)
    gap = _silence(report)
    assert gap["confidence"] < gap["confidence_raw"]
    assert gap["section"] != "eligible"
    assert "rejected" in gap["taste_reason"]


def test_stripped_markers_do_not_count_as_rejects(tmp_path):
    edited = tmp_path / "stripped.fcpxml"
    text = EDITED.read_text(encoding="utf-8")
    text = text.replace("Cut Conductor shadow proposal", "exported without conductor notes")
    edited.write_text(text, encoding="utf-8")
    events, warnings = diff_fcpxml(PROPOSED, edited)
    assert any("did not survive" in warning for warning in warnings)
    assert all(event["event"] != "reject" for event in events)
    assert any(event["event"] == "accept" and event["kind"] == "short_clip" for event in events)


def test_fixture_rejection_lowers_the_next_run(tmp_path, monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    events, _warnings = diff_fcpxml(PROPOSED, EDITED)
    taste_path = tmp_path / "taste.json"
    taste = load_taste(None)
    for event in events:
        taste.append(event)
    write_taste(taste, taste_path)
    baseline = analyze(FIXTURE, transcript_path=SRT, brief=BRIEF, out_dir=tmp_path / "base")
    learned = analyze(
        FIXTURE,
        transcript_path=SRT,
        brief=BRIEF,
        out_dir=tmp_path / "learned",
        taste_path=taste_path,
    )
    before = _silence(baseline)
    after = _silence(learned)
    assert before["section"] == "eligible"
    assert after["confidence"] < before["confidence"]
    assert after["confidence_raw"] == before["confidence"]
    assert after["section"] != "eligible"
    assert after["taste_reason"]
    assert "rejected" in after["taste_reason"]
    assert "silence_gap" in learned.payload["taste"]["priors"]
    assert "Taste" in learned.out_markdown.read_text(encoding="utf-8")
    assert learned.mode == "dry-run"


def test_four_of_five_rejections_name_the_count(tmp_path):
    taste_path = tmp_path / "taste.json"
    events = [
        {"event": "reject", "candidate_id": f"c{i}", "action": "remove", "pass": "mechanical", "kind": "silence_gap"}
        for i in range(4)
    ]
    events.append(
        {"event": "accept", "candidate_id": "c9", "action": "remove", "pass": "mechanical", "kind": "silence_gap"}
    )
    _write_log(taste_path, events)
    report = analyze(FIXTURE, transcript_path=SRT, brief=BRIEF, out_dir=tmp_path / "out", taste_path=taste_path)
    gap = _silence(report)
    assert "confidence lowered because you rejected 4/5 similar suggestions" in gap["taste_reason"]
    assert gap["confidence"] < gap["confidence_raw"]
    assert gap["section"] != "eligible"
    prior = report.payload["taste"]["priors"]["silence_gap"]
    assert prior["auto_confidence"] > 0.8
    assert prior["rejects"] == 4
    assert prior["accepts"] == 1


def test_accepts_cannot_open_auto_without_opt_in(tmp_path):
    gap_xml = tmp_path / "gap.fcpxml"
    gap_xml.write_text(_gap_xml(Fraction(3, 2)), encoding="utf-8")
    accepts = [
        {"event": "accept", "candidate_id": f"a{i}", "action": "tighten", "pass": "mechanical", "kind": "silence_gap"}
        for i in range(5)
    ]
    plain = tmp_path / "plain.json"
    opted = tmp_path / "opted.json"
    _write_log(plain, accepts)
    _write_log(
        opted,
        accepts,
        rules=[{"kind": "silence_gap", "event": "accept", "loosen_auto": True, "note": "always cut the gaps"}],
    )
    held = analyze(gap_xml, brief=BRIEF, out_dir=tmp_path / "held", taste_path=plain, passes=["mechanical"])
    opened = analyze(gap_xml, brief=BRIEF, out_dir=tmp_path / "opened", taste_path=opted, passes=["mechanical"])
    held_gap = _silence(held)
    opened_gap = _silence(opened)
    assert held_gap["confidence_raw"] < 0.8
    assert held_gap["confidence"] < 0.8
    assert held_gap["disposition"] != "auto"
    assert "explicit opt-in" in held_gap["taste_reason"]
    assert load_taste(plain).priors()["silence_gap"]["auto_confidence"] == 0.8
    assert opened_gap["disposition"] == "auto"
    assert opened_gap["confidence"] >= 0.8


def test_standing_reject_blocks_auto_and_creative_stays_review(tmp_path):
    taste_path = tmp_path / "taste.json"
    _write_log(
        taste_path,
        [],
        rules=[{"kind": "silence_gap", "event": "reject", "note": "keep the room tone"}],
    )
    report = analyze(FIXTURE, transcript_path=SRT, brief=BRIEF, out_dir=tmp_path / "out", taste_path=taste_path)
    gap = _silence(report)
    assert gap["confidence"] < 0.8
    assert gap["disposition"] != "auto"
    long = next(row for row in report.changes if row["kind"] == "long_static")
    assert long["disposition"] != "auto"


def test_notes_bind_a_timecode_and_store_a_rule(tmp_path):
    assert parse_at("12:30") == Fraction(12 * 60 + 30)
    taste_path = tmp_path / "in.json"
    write_taste(load_taste(None), taste_path)
    out = tmp_path / "out.json"
    code = main(
        [
            "feedback",
            "--taste",
            str(taste_path),
            "--out",
            str(out),
            "--notes",
            str(NOTES),
            "--fcpxml",
            str(FIXTURE),
        ]
    )
    assert code == 0
    taste = load_taste(out)
    assert taste.rules[0]["kind"] == "short_clip"
    assert taste.rules[0]["loosen_auto"] is True
    assert taste.rules[0]["when"] == {"flash": True}
    assert taste.pending
    assert taste.pending[0]["at"] == "12:30"
    report = analyze(
        FIXTURE,
        transcript_path=SRT,
        brief=BRIEF,
        out_dir=tmp_path / "run",
        taste_path=out,
        feedback_path=NOTES,
    )
    assert any(rule["kind"] == "short_clip" for rule in report.payload["taste"]["rules"])
    assert any(item.get("at") == "12:30" for item in load_taste(report.out_taste).pending)


def test_global_profile_is_read_and_not_copied(tmp_path):
    project = tmp_path / "project.json"
    glob = tmp_path / "global.json"
    write_taste(load_taste(None), project)
    _write_log(
        glob,
        [
            {"event": "reject", "candidate_id": "g1", "action": "remove", "pass": "mechanical", "kind": "silence_gap", "fingerprint": "global-reject"}
        ],
    )
    report = analyze(
        FIXTURE,
        transcript_path=SRT,
        brief=BRIEF,
        out_dir=tmp_path / "out",
        taste_path=project,
        global_taste_path=glob,
    )
    gap = _silence(report)
    assert gap["confidence"] < gap["confidence_raw"]
    written = json.loads(report.out_taste.read_text(encoding="utf-8"))
    assert all(event.get("fingerprint") != "global-reject" for event in written["log"])
    assert "rejected" in gap["taste_reason"]


def test_iterate_respects_a_stricter_silence_prior(tmp_path):
    taste_path = tmp_path / "taste.json"
    events = [
        {"event": "reject", "candidate_id": f"c{i}", "action": "remove", "pass": "mechanical", "kind": "silence_gap"}
        for i in range(4)
    ]
    events.append(
        {"event": "accept", "candidate_id": "c9", "action": "remove", "pass": "mechanical", "kind": "silence_gap"}
    )
    _write_log(taste_path, events)
    result = iterate(
        fcpxml=FIXTURE,
        transcript_path=SRT,
        brief=BRIEF,
        out_dir=tmp_path / "loop",
        taste_path=taste_path,
        max_rounds=3,
    )
    assert result.rounds[0]["applied_ids"] == []
    assert result.stop_reason == "no-progress"


def test_feedback_cli_diffs_the_fixture_pair(tmp_path):
    taste_path = tmp_path / "taste.json"
    write_taste(load_taste(None), taste_path)
    out = tmp_path / "learned.json"
    code = main(
        [
            "feedback",
            "--taste",
            str(taste_path),
            "--out",
            str(out),
            "--proposed",
            str(PROPOSED),
            "--edited",
            str(EDITED),
        ]
    )
    assert code == 0
    taste = load_taste(out)
    kinds = {event["kind"]: event["event"] for event in taste.log}
    assert kinds["silence_gap"] == "reject"
    assert kinds["short_clip"] == "accept"
    code = main(
        [
            "feedback",
            "--taste",
            str(taste_path),
            "--out",
            str(taste_path),
            "--event",
            "reject",
            "--id",
            "c1",
            "--action",
            "remove",
            "--pass",
            "mechanical",
        ]
    )
    assert code == 2


def test_global_prefs_and_gates_stay_out_of_the_project_file(tmp_path):
    project = tmp_path / "project.json"
    glob = tmp_path / "global.json"
    project.write_text(json.dumps({"version": 1, "prefs": {"hold_seconds": 3}}), encoding="utf-8")
    glob.write_text(
        json.dumps(
            {
                "version": 1,
                "prefs": {"target_pace": "tight", "hold_seconds": 9},
                "gates": {"auto_confidence": 0.9},
            }
        ),
        encoding="utf-8",
    )
    taste = load_taste(project, global_path=glob)
    assert taste.prefs["target_pace"] == "tight"
    assert taste.prefs["hold_seconds"] == 3
    assert taste.gates.auto_confidence == 0.9
    written = taste.dump()
    assert written["prefs"]["target_pace"] == "measured"
    assert written["gates"]["auto_confidence"] == 0.8


def test_iterate_auto_applies_do_not_feed_the_prior(tmp_path):
    result = iterate(
        fcpxml=FIXTURE,
        transcript_path=SRT,
        brief=BRIEF,
        out_dir=tmp_path,
        max_rounds=3,
    )
    first = result.rounds[0]
    assert first["applied_ids"]
    taste = load_taste(first["taste"])
    accepts = [event for event in taste.log if event["event"] == "accept"]
    assert accepts and all(event["source"] == "auto" for event in accepts)
    assert taste.priors()["silence_gap"]["accepts"] == 0
    assert taste.priors()["silence_gap"]["confidence_delta"] == 0.0


def test_person_accept_counts_and_auto_accept_does_not(tmp_path):
    shadow = analyze(FIXTURE, transcript_path=SRT, brief=BRIEF, out_dir=tmp_path / "shadow")
    gap_id = _silence(shadow)["candidate_id"]
    report = analyze(FIXTURE, transcript_path=SRT, brief=BRIEF, out_dir=tmp_path, apply=True, accept=[gap_id])
    taste = load_taste(report.out_taste)
    assert taste.log[-1]["source"] == "person"
    assert taste.priors()["silence_gap"]["accepts"] == 1


def test_reject_rule_says_it_closed_auto(tmp_path):
    taste_path = tmp_path / "taste.json"
    _write_log(taste_path, [], rules=[{"kind": "silence_gap", "event": "reject"}])
    report = analyze(FIXTURE, transcript_path=SRT, brief=BRIEF, out_dir=tmp_path / "out", taste_path=taste_path)
    gap = _silence(report)
    assert "standing reject rule" in gap["taste_reason"]
    assert "opt-in" not in gap["taste_reason"]


def test_style_observers_and_unknown_keys_round_trip(tmp_path, monkeypatch):
    from conductor import feedback

    monkeypatch.setattr(feedback, "OBSERVERS", {})

    def fades(proposed, edited):
        return [{"param": "music.fade_out_seconds", "section": "outro", "value": 1.5}]

    feedback.register_observer("fades", fades)
    events, _warnings = diff_fcpxml(PROPOSED, EDITED)
    observed = [event for event in events if event["event"] == "observe"]
    assert observed[0]["param"] == "music.fade_out_seconds"
    assert observed[0]["observer"] == "fades"

    taste_path = tmp_path / "taste.json"
    taste_path.write_text(
        json.dumps({"version": 1, "style": {"profile": "styles/doc/profile"}, "log": []}),
        encoding="utf-8",
    )
    taste = load_taste(taste_path)
    for event in events:
        taste.append(event)
    out = tmp_path / "out.json"
    write_taste(taste, out)
    reloaded = load_taste(out)
    assert reloaded.extra["style"] == {"profile": "styles/doc/profile"}
    assert reloaded.observations("music.fade_out_seconds")[0]["value"] == 1.5
    assert "music.fade_out_seconds" not in reloaded.priors()
    with pytest.raises(ConductorError, match="param"):
        feedback.register_observer("bad", lambda p, e: [{"value": 1}])
        diff_fcpxml(PROPOSED, EDITED)


def test_unreadable_timecode_is_an_error():
    with pytest.raises(ConductorError, match="timecode"):
        parse_at("noon")


def _gap_xml(duration: Fraction) -> str:
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE fcpxml>
<fcpxml version="1.11">
  <resources>
    <format id="r1" frameDuration="1/24s" width="1920" height="1080"/>
    <asset id="r2" name="interview" start="0s" duration="60s" hasVideo="1" hasAudio="1" format="r1">
      <media-rep kind="original-media" src="file:///tmp/interview.mov"/>
    </asset>
  </resources>
  <library>
    <event name="E">
      <project name="Gap">
        <sequence format="r1" duration="20s" tcStart="0s">
          <spine>
            <asset-clip ref="r2" offset="0s" name="Talk" start="0s" duration="8s" audioRole="dialogue"/>
            <gap name="Gap" offset="8s" start="3600s" duration="{duration.numerator}/{duration.denominator}s"/>
            <asset-clip ref="r2" offset="{8 + float(duration)}s" name="After" start="12s" duration="8s" audioRole="dialogue"/>
          </spine>
        </sequence>
      </project>
    </event>
  </library>
</fcpxml>
"""
