"""room-run detects a drop, calls iterate, and writes a chat summary."""

from __future__ import annotations

import json
import shutil
import zipfile
from datetime import datetime, timezone
from pathlib import Path

import pytest

from conductor.cli import main
from conductor.errors import ConductorError
from conductor.room import WatchState, room_run, scan_once

FIXTURE = Path("fixtures/sample_interview.fcpxml")
SRT = Path("fixtures/sample_interview.srt")
SELECTS = Path("fixtures/selects")
BRIEF = "A tight interview. Keep the guest's story, lose dead air."


def _xml(version: str = "1.11", src: str = "file:///Volumes/Media/interview.mov") -> str:
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE fcpxml>
<fcpxml version="{version}">
  <resources>
    <format id="r1" frameDuration="1/24s" width="1920" height="1080"/>
    <asset id="r2" name="interview" start="0s" duration="10s" hasVideo="1" hasAudio="1">
      <media-rep kind="original-media" src="{src}"/>
    </asset>
  </resources>
  <project name="Rough">
    <sequence format="r1" duration="10s" tcStart="0s" tcFormat="NDF">
      <spine>
        <asset-clip ref="r2" offset="0s" name="A" start="0s" duration="10s"/>
      </spine>
    </sequence>
  </project>
</fcpxml>
"""


def _assert_summary(result, *, kind: str) -> dict:
    payload = result.payload
    assert payload["protocol"] == "cut-conductor.room-run"
    assert payload["protocol_version"] == 1
    assert payload["ok"] is True
    assert payload["mode"] == "dry-run"
    assert payload["input"]["kind"] == kind
    assert payload["stop_reason"]
    assert payload["duration"]["before_seconds"] >= payload["duration"]["after_seconds"]
    assert payload["signals_label"] in {"none", "transcript", "media", "transcript, media"}
    shadow = Path(payload["shadow"])
    open_path = Path(payload["open_in_final_cut"])
    assert shadow.is_file()
    assert open_path.is_file()
    assert open_path.is_relative_to(result.out_dir)
    assert shadow.is_relative_to(result.out_dir)
    assert (result.out_dir / "room.md").read_text(encoding="utf-8") == result.markdown
    on_disk = json.loads((result.out_dir / "room.json").read_text(encoding="utf-8"))
    assert on_disk["open_in_final_cut"] == str(open_path)
    assert "Open in Final Cut:" in result.markdown
    assert f"Signals: {payload['signals_label']}" in result.markdown
    assert "Stop: " in result.markdown
    return payload


def test_fcpxml_fixture_writes_a_summary_and_can_run_again(tmp_path, monkeypatch):
    before = FIXTURE.read_bytes()
    fixed = datetime(2026, 9, 25, 12, 0, 0, tzinfo=timezone.utc)
    monkeypatch.setattr("conductor.room._now", lambda: fixed)
    first = room_run(FIXTURE, out_root=tmp_path, brief=BRIEF)
    payload = _assert_summary(first, kind="fcpxml")
    assert payload["signals"] == ["transcript"]
    assert payload["media_note"]
    assert "not on this machine" in payload["media_note"]
    assert payload["duration"]["before_seconds"] > payload["duration"]["after_seconds"]
    assert payload["cuts"]
    assert all(cut["pass"] == "mechanical" for cut in payload["cuts"])
    assert payload["cuts"][0]["timecode"]
    assert "00:00:" in payload["cuts"][0]["timecode"]
    assert payload["flagged"]
    last_json = Path(json.loads((first.out_dir / "iterate.json").read_text())["rounds"][-1]["json"])
    changes = [
        row
        for row in json.loads(last_json.read_text())["changes"]
        if row["section"] in {"review", "escalate"}
    ]
    assert [item["candidate_id"] for item in payload["flagged"]] == [row["candidate_id"] for row in changes]
    assert FIXTURE.read_bytes() == before

    second = room_run(FIXTURE, out_root=tmp_path, brief=BRIEF)
    assert second.out_dir != first.out_dir
    assert first.out_dir.name == "sample_interview-20260925-120000"
    assert second.out_dir.name == "sample_interview-20260925-120000-2"
    assert FIXTURE.read_bytes() == before
    assert second.payload["signals"] == ["transcript"]


def test_fcpxmld_bundle(tmp_path):
    before = FIXTURE.read_bytes()
    bundle = tmp_path / "in" / "Cut.fcpxmld"
    bundle.mkdir(parents=True)
    shutil.copy(FIXTURE, bundle / "Info.fcpxml")
    info = (bundle / "Info.fcpxml").read_bytes()
    result = room_run(bundle, out_root=tmp_path / "out", brief=BRIEF)
    payload = _assert_summary(result, kind="fcpxmld")
    assert payload["signals_label"] == "none"
    assert (bundle / "Info.fcpxml").read_bytes() == info
    assert FIXTURE.read_bytes() == before


def test_zipped_fcpxmld(tmp_path):
    bundle = tmp_path / "Cut.fcpxmld"
    bundle.mkdir()
    shutil.copy(FIXTURE, bundle / "Info.fcpxml")
    shutil.copy(SRT, bundle / "interview.srt")
    archive = tmp_path / "in" / "cut.zip"
    archive.parent.mkdir()
    with zipfile.ZipFile(archive, "w") as zipped:
        for file in bundle.rglob("*"):
            if file.is_file():
                zipped.write(file, Path("Cut.fcpxmld") / file.relative_to(bundle))
    before = archive.read_bytes()
    result = room_run(archive, out_root=tmp_path / "out", brief=BRIEF)
    payload = _assert_summary(result, kind="zip")
    assert payload["input"]["contained"] == "fcpxmld"
    assert payload["signals"] == ["transcript"]
    assert "zip, fcpxmld inside" in result.markdown
    assert archive.read_bytes() == before
    assert (result.out_dir / "unpacked" / "Cut.fcpxmld" / "Info.fcpxml").is_file()


def test_media_folder_uses_durations_and_leaves_clips(tmp_path):
    before = {path: path.read_bytes() for path in SELECTS.rglob("*") if path.is_file()}
    result = room_run(SELECTS, out_root=tmp_path, brief=BRIEF)
    payload = _assert_summary(result, kind="media")
    assert payload["signals"] == ["media"]
    assert payload["media_note"] is None
    starter = Path(payload["starter"])
    assert starter.is_file()
    text = starter.read_text(encoding="utf-8")
    assert 'duration="8s"' in text
    assert {path: path.read_bytes() for path in SELECTS.rglob("*") if path.is_file()} == before


def test_zip_without_fcpxml(tmp_path):
    archive = tmp_path / "notes.zip"
    with zipfile.ZipFile(archive, "w") as zipped:
        zipped.writestr("readme.txt", "no timeline here")
    with pytest.raises(ConductorError, match="no Final Cut XML"):
        room_run(archive, out_root=tmp_path / "out", brief=BRIEF)
    note = next((tmp_path / "out").glob("*/room.md"))
    assert "no Final Cut XML" in note.read_text(encoding="utf-8")


def test_unsupported_fcpxml_version(tmp_path):
    xml = tmp_path / "old.fcpxml"
    xml.write_text(_xml(version="1.5"), encoding="utf-8")
    with pytest.raises(ConductorError, match="version 1.5"):
        room_run(xml, out_root=tmp_path / "out", brief=BRIEF)
    message = next((tmp_path / "out").glob("*/room.md")).read_text(encoding="utf-8")
    assert "Export XML" in message
    assert "1.8" in message


def test_local_media_missing(tmp_path):
    xml = tmp_path / "in" / "cut.fcpxml"
    xml.parent.mkdir()
    xml.write_text(_xml(src="clips/interview.mov"), encoding="utf-8")
    with pytest.raises(ConductorError, match="media for this drop is missing"):
        room_run(xml, out_root=tmp_path / "out", brief=BRIEF)
    assert "interview" in next((tmp_path / "out").glob("*/room.md")).read_text(encoding="utf-8")


def test_media_folder_with_no_clips(tmp_path):
    folder = tmp_path / "empty"
    folder.mkdir()
    (folder / "notes.txt").write_text("hello", encoding="utf-8")
    with pytest.raises(ConductorError, match="media is missing"):
        room_run(folder, out_root=tmp_path / "out", brief=BRIEF)


def test_refuses_to_write_inside_the_drop(tmp_path):
    folder = tmp_path / "clips"
    folder.mkdir()
    (folder / "a.mp4").write_bytes(b"clip")
    with pytest.raises(ConductorError, match="inside the drop"):
        room_run(folder, out_root=folder / "out", brief=BRIEF)


def test_zip_slip_is_refused(tmp_path):
    archive = tmp_path / "bad.zip"
    with zipfile.ZipFile(archive, "w") as zipped:
        zipped.writestr("../evil.fcpxml", "<fcpxml/>")
    with pytest.raises(ConductorError, match="escapes the archive"):
        room_run(archive, out_root=tmp_path / "out", brief=BRIEF)
    assert not (tmp_path / "evil.fcpxml").exists()


def test_cli_prints_the_summary_and_a_failure(tmp_path, capsys):
    code = main(
        [
            "room-run",
            str(FIXTURE),
            "--brief",
            BRIEF,
            "--out-root",
            str(tmp_path / "out"),
        ]
    )
    assert code == 0
    text = capsys.readouterr().out
    assert "Open in Final Cut:" in text
    assert "Signals: transcript" in text
    archive = tmp_path / "empty.zip"
    with zipfile.ZipFile(archive, "w") as zipped:
        zipped.writestr("notes.txt", "nope")
    code = main(["room-run", str(archive), "--out-root", str(tmp_path / "bad")])
    assert code == 2
    assert "no Final Cut XML" in capsys.readouterr().err


def test_watcher_waits_for_a_stable_file_then_skips_it(tmp_path):
    inbox = tmp_path / "in"
    out = tmp_path / "out"
    inbox.mkdir()
    drop = inbox / "cut.fcpxml"
    payload = FIXTURE.read_bytes()
    drop.write_bytes(payload[:40])
    state = WatchState.load(out)
    assert scan_once(inbox, out, state, now=0.0, stable_seconds=2, brief=BRIEF) == []
    assert list(out.glob("*/room.json")) == []
    drop.write_bytes(payload)
    assert scan_once(inbox, out, state, now=10.0, stable_seconds=2, brief=BRIEF) == []
    assert list(out.glob("*/room.json")) == []
    ran = scan_once(inbox, out, state, now=13.0, stable_seconds=2, brief=BRIEF)
    assert [event.status for event in ran] == ["ran"]
    assert len(list(out.glob("*/room.json"))) == 1
    assert "Open in Final Cut:" in ran[0].message
    skipped = scan_once(inbox, out, state, now=20.0, stable_seconds=2, brief=BRIEF)
    assert [event.status for event in skipped] == ["skipped"]
    again = scan_once(inbox, out, state, now=25.0, stable_seconds=2, brief=BRIEF)
    assert again == []
    log = (out / "room-run.log").read_text(encoding="utf-8")
    assert log.count("ok ") == 1
    assert log.count("skip ") == 1
    assert len(list(out.glob("*/room.json"))) == 1
    reloaded = WatchState.load(out)
    assert scan_once(inbox, out, reloaded, now=30.0, stable_seconds=2, brief=BRIEF) == []
    restarted = scan_once(inbox, out, reloaded, now=33.0, stable_seconds=2, brief=BRIEF)
    assert [event.status for event in restarted] == ["skipped"]
    assert (out / "room-run.log").read_text(encoding="utf-8").count("skip ") == 2


def test_watcher_records_a_bad_drop_once(tmp_path):
    inbox = tmp_path / "in"
    out = tmp_path / "out"
    inbox.mkdir()
    archive = inbox / "notes.zip"
    with zipfile.ZipFile(archive, "w") as zipped:
        zipped.writestr("readme.txt", "no timeline")
    state = WatchState.load(out)
    assert scan_once(inbox, out, state, now=0.0, stable_seconds=2, brief=BRIEF) == []
    failed = scan_once(inbox, out, state, now=3.0, stable_seconds=2, brief=BRIEF)
    assert [event.status for event in failed] == ["error"]
    assert "no Final Cut XML" in failed[0].message
    skipped = scan_once(inbox, out, state, now=6.0, stable_seconds=2, brief=BRIEF)
    assert [event.status for event in skipped] == ["skipped"]
    assert scan_once(inbox, out, state, now=9.0, stable_seconds=2, brief=BRIEF) == []
    log = (out / "room-run.log").read_text(encoding="utf-8")
    assert log.count("error ") == 1
    assert log.count("skip ") == 1
