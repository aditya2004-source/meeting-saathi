from app.progress import TAIL_STAGE_KEYS, describe_progress


def test_received_state_has_no_percent_or_eta():
    progress = describe_progress("received")

    assert progress["percent"] is None
    assert progress["eta_seconds"] is None
    assert progress["completed"] is False


def test_chunk_processing_with_no_chunks_yet():
    progress = describe_progress("chunk_processing", chunk_durations={})

    assert "waiting" in progress["label"].lower()
    assert progress["percent"] is None
    assert progress["recorded"] is None  # meeting still live -- no verdict yet


def test_chunk_processing_shows_minutes_captured():
    progress = describe_progress("chunk_processing", chunk_durations={"0": 50.0, "1": 48.0})

    assert "1.6 min" in progress["label"]
    assert progress["recorded"] is True


def test_finalize_with_zero_captured_audio_is_a_real_failure_signal():
    # The meeting has ended (state moved past chunk_processing) but not one
    # chunk had any audio -- a genuine "extension didn't actually record"
    # signal, unlike the still-live case above.
    progress = describe_progress("generating_docs", timing={}, chunk_durations={})

    assert progress["recorded"] is False


def test_legacy_whole_file_path_has_no_recorded_signal():
    # chunk_durations is None (not applicable) for the legacy upload path.
    progress = describe_progress("transcribing", chunk_durations=None)

    assert progress["recorded"] is None


def test_extracting_facts_label_is_shown():
    # No document (MOM, BRD, ...) generates automatically anymore -- the only
    # automatic post-meeting stage is fact extraction, so the label reflects
    # that directly rather than listing pending documents.
    progress = describe_progress("extracting_facts", timing={})

    assert "requirements" in progress["label"].lower()


def test_tail_percent_counts_completed_stages():
    # TAIL_STAGE_KEYS is just ["extract_facts"] now -- document generation is
    # on-demand (see app/docgen/registry.py), not part of this run's bounded tail.
    assert describe_progress("extracting_facts", timing={})["percent"] == 0
    assert describe_progress("extracting_facts", timing={"extract_facts": 1.0})["percent"] == 100


def test_saved_state_is_100_percent_and_completed_with_folder():
    progress = describe_progress("saved", folder_path="/some/folder")

    assert progress["percent"] == 100
    assert progress["completed"] is True
    assert progress["folder_path"] == "/some/folder"


def test_folder_path_surfaces_before_saved_for_incremental_delivery():
    # The meeting folder is now created (and its path recorded) well
    # before the run is fully "saved" -- individual documents can exist
    # and be downloadable while others are still generating (see
    # app.orchestrator_streaming.finalize_run()). folder_path must not be
    # withheld just because the run isn't finished yet.
    progress = describe_progress("generating_docs", folder_path="/some/folder")

    assert progress["folder_path"] == "/some/folder"
    assert progress["completed"] is False


def test_failed_state_shows_error_message():
    progress = describe_progress("failed", error_message="disk full")

    assert progress["label"] == "Failed: disk full"
    assert progress["completed"] is False
    assert progress["folder_path"] is None


def test_status_hides_internal_states_behind_simple_labels():
    # The badge should never leak raw pipeline vocabulary like
    # "chunk_processing"/"generating_docs" -- only a handful of simple,
    # user-facing categories.
    assert describe_progress("received")["status"] == "Starting"
    assert describe_progress("chunk_processing")["status"] == "Recording"
    assert describe_progress("transcribing")["status"] == "Recording"
    assert describe_progress("diarizing")["status"] == "Recording"
    assert describe_progress("generating_docs")["status"] == "Processing"
    assert describe_progress("rendering")["status"] == "Processing"
    assert describe_progress("saving")["status"] == "Processing"
    assert describe_progress("saved")["status"] == "Completed"
    assert describe_progress("failed")["status"] == "Failed"


def test_eta_requires_history_for_every_remaining_stage():
    timing = {}  # extract_facts (the only tail stage) still remaining
    history = {"some_other_stage": {"count": 3, "mean": 4.0}}  # doesn't cover it

    progress = describe_progress("extracting_facts", timing=timing, history=history)

    assert progress["eta_seconds"] is None


def test_eta_computed_when_history_covers_all_remaining_stages():
    timing = {k: 1.0 for k in TAIL_STAGE_KEYS[:-1]}  # only the last stage remains
    history = {TAIL_STAGE_KEYS[-1]: {"count": 5, "mean": 2.5}}

    progress = describe_progress("saving", timing=timing, history=history)

    assert progress["eta_seconds"] == 2.5


def test_no_history_means_no_eta():
    timing = {"extract_facts": 1.0}

    progress = describe_progress("generating_docs", timing=timing, history={})

    assert progress["eta_seconds"] is None
