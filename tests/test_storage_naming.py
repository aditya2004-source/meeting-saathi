import datetime

from app.storage import folder_name, sanitize_name, save_meeting_folder


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
