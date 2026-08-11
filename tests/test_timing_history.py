import json

from app.pipeline.timing import load_stage_history, record_stage_durations


def test_load_stage_history_missing_file_returns_empty(tmp_path):
    assert load_stage_history(tmp_path / "nope.json") == {}


def test_load_stage_history_corrupt_file_returns_empty(tmp_path):
    path = tmp_path / "history.json"
    path.write_text("not json", encoding="utf-8")

    assert load_stage_history(path) == {}


def test_record_stage_durations_first_run_sets_mean_to_that_value(tmp_path):
    path = tmp_path / "history.json"

    record_stage_durations(path, {"generate_mom": 5.0})

    history = load_stage_history(path)
    assert history["generate_mom"] == {"count": 1, "mean": 5.0}


def test_record_stage_durations_incremental_mean_across_runs(tmp_path):
    path = tmp_path / "history.json"

    record_stage_durations(path, {"generate_mom": 4.0})
    record_stage_durations(path, {"generate_mom": 6.0})

    history = load_stage_history(path)
    assert history["generate_mom"]["count"] == 2
    assert history["generate_mom"]["mean"] == 5.0


def test_record_stage_durations_ignores_non_numeric_values(tmp_path):
    # A repeated/list-shaped timing value (e.g. "chunk_transcribe") isn't
    # part of the bounded tail and must never corrupt the history file.
    path = tmp_path / "history.json"

    record_stage_durations(path, {"generate_mom": 5.0, "chunk_transcribe": [1.0, 2.0]})

    history = load_stage_history(path)
    assert "chunk_transcribe" not in history
    assert history["generate_mom"]["count"] == 1


def test_record_stage_durations_keeps_unrelated_stages_independent(tmp_path):
    path = tmp_path / "history.json"

    record_stage_durations(path, {"generate_mom": 5.0})
    record_stage_durations(path, {"render_mom_pdf": 1.0})

    history = load_stage_history(path)
    assert history["generate_mom"]["count"] == 1
    assert history["render_mom_pdf"]["count"] == 1


def test_record_stage_durations_writes_atomically_no_leftover_tmp_file(tmp_path):
    path = tmp_path / "history.json"

    record_stage_durations(path, {"generate_mom": 5.0})

    assert path.exists()
    assert not path.with_suffix(".json.tmp").exists()
    json.loads(path.read_text(encoding="utf-8"))  # valid JSON, not partially written


def test_record_stage_durations_creates_parent_directory(tmp_path):
    path = tmp_path / "nested" / "dir" / "history.json"

    record_stage_durations(path, {"generate_mom": 5.0})

    assert path.exists()
