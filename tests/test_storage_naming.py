import datetime

from app.storage import create_meeting_folder, folder_name, sanitize_name, save_meeting_folder, write_meeting_file


def test_sanitize_name_strips_illegal_filesystem_chars():
    assert sanitize_name('Q3 Review: Budget/Plan?') == "Q3 Review Budget Plan"


def test_sanitize_name_collapses_whitespace_and_trims():
    assert sanitize_name("  Weekly   Sync   ") == "Weekly Sync"


def test_sanitize_name_empty_falls_back_to_default():
    assert sanitize_name("   ") == "Untitled Meeting"


def test_sanitize_name_truncates_long_titles():
    long_title = "A" * 300
    result = sanitize_name(long_title, max_length=50)
    assert len(result) <= 50


def test_folder_name_format():
    when = datetime.datetime(2026, 7, 31, 14, 5)
    assert folder_name("Weekly Sales Review", when) == "Weekly Sales Review - 2026-07-31 1405"


def test_folder_name_sanitizes_title_component():
    when = datetime.datetime(2026, 7, 31, 9, 0)
    result = folder_name("Budget/Plan: Q3?", when)
    assert result == "Budget Plan Q3 - 2026-07-31 0900"


def test_save_meeting_folder_writes_all_files_atomically(tmp_path):
    when = datetime.datetime(2026, 7, 31, 10, 30)
    files = {"MOM.md": "# Minutes", "transcript.json": '{"ok": true}', "recording.mp3": b"\x00\x01"}

    result_path = save_meeting_folder(tmp_path, "Test Meeting", when, files)

    assert result_path == tmp_path / "Test Meeting - 2026-07-31 1030"
    assert (result_path / "MOM.md").read_text() == "# Minutes"
    assert (result_path / "transcript.json").read_text() == '{"ok": true}'
    assert (result_path / "recording.mp3").read_bytes() == b"\x00\x01"


def test_save_meeting_folder_avoids_collision_by_suffixing(tmp_path):
    when = datetime.datetime(2026, 7, 31, 10, 30)
    first = save_meeting_folder(tmp_path, "Dup Meeting", when, {"a.md": "1"})
    second = save_meeting_folder(tmp_path, "Dup Meeting", when, {"a.md": "2"})

    assert first != second
    assert second.name.endswith("(2)")


def test_create_meeting_folder_exists_and_is_empty(tmp_path):
    when = datetime.datetime(2026, 7, 31, 10, 30)

    folder = create_meeting_folder(tmp_path, "Test Meeting", when)

    assert folder == tmp_path / "Test Meeting - 2026-07-31 1030"
    assert folder.is_dir()
    assert list(folder.iterdir()) == []


def test_create_meeting_folder_avoids_collision_by_suffixing(tmp_path):
    when = datetime.datetime(2026, 7, 31, 10, 30)
    first = create_meeting_folder(tmp_path, "Dup Meeting", when)
    second = create_meeting_folder(tmp_path, "Dup Meeting", when)

    assert first != second
    assert second.name.endswith("(2)")


def test_write_meeting_file_writes_text_and_bytes(tmp_path):
    folder = create_meeting_folder(tmp_path, "Test Meeting", datetime.datetime(2026, 7, 31, 10, 30))

    write_meeting_file(folder, "MOM.md", "# Minutes")
    write_meeting_file(folder, "recording.mp3", b"\x00\x01")

    assert (folder / "MOM.md").read_text() == "# Minutes"
    assert (folder / "recording.mp3").read_bytes() == b"\x00\x01"


def test_write_meeting_file_does_not_disturb_sibling_files(tmp_path):
    # The whole point of per-file writes -- one document being generated
    # (or re-written) must never touch a sibling document that's already
    # sitting there, unlike the old whole-folder atomic rename.
    folder = create_meeting_folder(tmp_path, "Test Meeting", datetime.datetime(2026, 7, 31, 10, 30))
    write_meeting_file(folder, "MOM.md", "# MOM ready first")

    write_meeting_file(folder, "Meeting_Analysis.md", "# Analysis ready later")

    assert (folder / "MOM.md").read_text() == "# MOM ready first"
    assert (folder / "Meeting_Analysis.md").read_text() == "# Analysis ready later"
    assert {p.name for p in folder.iterdir()} == {"MOM.md", "Meeting_Analysis.md"}


def test_write_meeting_file_leaves_no_temp_file_behind(tmp_path):
    folder = create_meeting_folder(tmp_path, "Test Meeting", datetime.datetime(2026, 7, 31, 10, 30))

    write_meeting_file(folder, "MOM.md", "# Minutes")

    assert [p.name for p in folder.iterdir()] == ["MOM.md"]
