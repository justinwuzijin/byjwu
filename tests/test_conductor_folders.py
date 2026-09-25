"""The Desktop drop folders, and the fallback to the pre-rename jevid-* names."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from conductor.cli import main
from conductor.errors import ConductorError
from conductor.folders import DROP_IN, DROP_OUT, drop_folder, drop_input

FIXTURE = Path("fixtures/sample_interview.fcpxml").resolve()
SELECTS = Path("fixtures/selects").resolve()
BRIEF = "A tight interview. Keep the guest's story, lose dead air."


def test_new_names_when_nothing_exists(tmp_path):
    assert drop_folder(DROP_IN, home=tmp_path) == (tmp_path / "Desktop" / "byjwu-in", None)
    assert drop_folder(DROP_OUT, home=tmp_path) == (tmp_path / "Desktop" / "byjwu-out", None)


def test_legacy_folders_are_used_with_a_note(tmp_path):
    for name in ("jevid-in", "jevid-out"):
        (tmp_path / "Desktop" / name).mkdir(parents=True)
    folder, note = drop_folder(DROP_IN, home=tmp_path)
    assert folder == tmp_path / "Desktop" / "jevid-in"
    assert note is not None and "legacy" in note and "byjwu-in" in note and "\n" not in note
    folder, note = drop_folder(DROP_OUT, home=tmp_path)
    assert folder == tmp_path / "Desktop" / "jevid-out"
    assert "byjwu-out" in note


def test_new_folder_wins_over_legacy(tmp_path):
    for name in ("byjwu-in", "jevid-in"):
        (tmp_path / "Desktop" / name).mkdir(parents=True)
    assert drop_folder(DROP_IN, home=tmp_path) == (tmp_path / "Desktop" / "byjwu-in", None)


def test_drop_input_picks_an_export_or_the_folder(tmp_path):
    assert drop_input(tmp_path) == {"media": tmp_path}
    export = tmp_path / "cut.fcpxml"
    export.write_text("<fcpxml/>")
    assert drop_input(tmp_path) == {"fcpxml": export}
    (tmp_path / "other.fcpxml").write_text("<fcpxml/>")
    with pytest.raises(ConductorError):
        drop_input(tmp_path)
    with pytest.raises(ConductorError):
        drop_input(tmp_path / "missing")


def test_iterate_with_no_input_falls_back_to_legacy_folders(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HOME", str(tmp_path))
    legacy_in = tmp_path / "Desktop" / "jevid-in"
    legacy_out = tmp_path / "Desktop" / "jevid-out"
    legacy_in.mkdir(parents=True)
    legacy_out.mkdir()
    shutil.copy(FIXTURE, legacy_in / "cut.fcpxml")
    code = main(["iterate", "--brief", BRIEF, "--max-rounds", "1"])
    assert code == 0
    err = capsys.readouterr().err
    assert "using legacy" in err and "byjwu-in" in err and "byjwu-out" in err
    assert (legacy_out / "iterate.json").is_file()
    assert (legacy_out / "v1").is_dir()
    assert not (tmp_path / "Desktop" / "byjwu-out").exists()


def test_iterate_with_no_input_uses_new_folders(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HOME", str(tmp_path))
    drop = tmp_path / "Desktop" / "byjwu-in"
    shutil.copytree(SELECTS, drop)
    code = main(["iterate", "--brief", BRIEF, "--durations", str(drop / "durations.json"), "--max-rounds", "1"])
    assert code == 0
    assert "legacy" not in capsys.readouterr().err
    assert (tmp_path / "Desktop" / "byjwu-out" / "starter.fcpxml").is_file()


def test_room_run_defaults_to_the_legacy_outbox_with_a_note(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HOME", str(tmp_path))
    legacy_out = tmp_path / "Desktop" / "jevid-out"
    legacy_out.mkdir(parents=True)
    code = main(["room-run", str(FIXTURE), "--brief", BRIEF, "--max-rounds", "1"])
    assert code == 0
    assert "using legacy" in capsys.readouterr().err
    assert list(legacy_out.glob("*/room.md"))
    assert not (tmp_path / "Desktop" / "byjwu-out").exists()


def test_room_run_needs_a_path_unless_watching(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    assert main(["room-run", "--brief", BRIEF]) == 2


def test_room_run_watch_defaults_to_the_drop_folders(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HOME", str(tmp_path))
    seen = {}
    monkeypatch.setattr("conductor.cli.watch", lambda inbox, **kwargs: seen.update(inbox=inbox, **kwargs))
    assert main(["room-run", "--watch"]) == 0
    assert seen["inbox"] == tmp_path / "Desktop" / "byjwu-in"
    assert seen["out_root"] == tmp_path / "Desktop" / "byjwu-out"
    for name in ("jevid-in", "jevid-out"):
        (tmp_path / "Desktop" / name).mkdir(parents=True)
    assert main(["room-run", "--watch"]) == 0
    assert seen["inbox"] == tmp_path / "Desktop" / "jevid-in"
    assert seen["out_root"] == tmp_path / "Desktop" / "jevid-out"
    assert capsys.readouterr().err.count("using legacy") == 2
