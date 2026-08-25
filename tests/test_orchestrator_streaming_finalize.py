"""Exercises orchestrator_streaming.finalize_run()'s wiring end-to-end with a
fake only for Gemini (docgen_engine.extract_meeting_facts) -- the real bug this
originally guarded against (finalize_run() never called resolve_speaker_names()
at all) could only be caught by a test that runs the actual finalize_run()
function, not by speaker_names.py's own pure-function unit tests. app.storage's
create_meeting_folder()/write_meeting_file() run for real against a tmp_path
settings.base_storage_dir, since they're cheap plain filesystem calls.

No document (MOM, BRD, ...) is generated here anymore -- finalize_run() now
stops at transcript.json + facts.json (see app/docgen/registry.py for the
on-demand generation that happens later, from the dashboard).
"""
import json
from pathlib import Path

import pytest

from app import chunked_state, db, orchestrator_streaming
from app.config import settings
from app.pipeline.diarize import SpeakerSegment


@pytest.fixture
def run_id():
    return "test-run-finalize"


@pytest.fixture(autouse=True)
def _clean_chunk_state(run_id):
    yield
    chunked_state.drop(run_id)


def _install_fakes(monkeypatch, run_row):
    monkeypatch.setattr(db, "get_run", lambda rid: dict(run_row))

    def fake_update_run(rid, **fields):
        run_row.update(fields)
        return dict(run_row)

    monkeypatch.setattr(db, "update_run", fake_update_run)

    seen: dict = {}

    def fake_extract_meeting_facts(transcript_text):
        seen["transcript_text"] = transcript_text
        return {"requirements": [], "topics_discussed": [], "decisions": [], "action_items": [], "key_quotes": []}

    monkeypatch.setattr(orchestrator_streaming.docgen_engine, "extract_meeting_facts", fake_extract_meeting_facts)

    return seen


def _configure_dirs(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "working_dir", tmp_path / "working")
    monkeypatch.setattr(settings, "base_storage_dir", tmp_path / "storage")
    # finalize_run() also persists cross-run stage-duration history (see
    # app/progress.py) -- redirect it away from the real project's data/
    # directory so this test can't pollute it with fake durations.
    monkeypatch.setattr(settings, "project_root", tmp_path)


def test_finalize_run_resolves_dom_names_and_never_leaves_bare_placeholders(
    tmp_path, monkeypatch, run_id
):
    _configure_dirs(monkeypatch, tmp_path)

    run_row = {
        "id": run_id,
        "title": "Weekly Sync",
        "created_at": "2026-01-01T00:00:00+00:00",
        "state": "chunk_processing",
        "folder_path": None,
        "error_message": None,
        "client_name": "",
    }
    _install_fakes(monkeypatch, run_row)

    work_dir = orchestrator_streaming.working_dir_for(run_id)
    # Full accumulated speaker_events.json, as it would exist at finalize
    # time -- includes events landing inside a chunk that (hypothetically)
    # fell back to pyannote and only got a placeholder at the time, giving
    # resolve_speaker_names() a real chance to name it after the fact.
    speaker_events = json.dumps(
        [
            {"name": "Priya Shah", "t_seconds": 0.2},
            {"name": "Priya Shah", "t_seconds": 0.5},
        ]
    )
    (work_dir / "speaker_events.json").write_text(speaker_events, encoding="utf-8")

    state = chunked_state.get_or_create(run_id)
    state.processed_segments.extend(
        [
            # Chunk-tagged placeholder from the pyannote-fallback branch --
            # should resolve to "Priya Shah" via the DOM events above.
            SpeakerSegment(start=0.0, end=1.0, speaker="Speaker 1 (chunk@0.0)", text="hello everyone"),
            # Already DOM-resolved by the fast path -- must pass through unchanged.
            SpeakerSegment(start=10.0, end=11.0, speaker="Rahul Verma", text="agreed"),
            # No nearby DOM event at all -- must become an excerpt label, never bare "Unknown".
            SpeakerSegment(start=20.0, end=21.0, speaker="Unknown", text="who is speaking right now"),
        ]
    )

    orchestrator_streaming.finalize_run(run_id)

    folder = Path(run_row["folder_path"])
    transcript = json.loads((folder / "transcript.json").read_text(encoding="utf-8"))
    speakers = [seg["speaker"] for seg in transcript["segments"]]

    assert speakers[0] == "Priya Shah"
    assert speakers[1] == "Rahul Verma"
    assert speakers[2] == "Unidentified speaker 1"
    assert "who is speaking right now" in transcript["unidentified_speaker_excerpts"]["Unidentified speaker 1"]

    for speaker in speakers:
        assert speaker != "Unknown"
        assert not speaker.startswith("Speaker ")

    # Real names (roster-independent here, since no attendee_roster.json was
    # written) still make it into the deterministic attendee list.
    assert transcript["attendees"] == ["Priya Shah", "Rahul Verma"]

    assert run_row["state"] == "saved"
    assert run_row["folder_path"] is not None
    assert (folder / "facts.json").is_file()
    # No document is generated automatically anymore.
    assert not (folder / "MOM.md").exists()
    assert not (folder / "MOM.pdf").exists()


def test_finalize_run_without_speaker_events_still_fills_placeholders(tmp_path, monkeypatch, run_id):
    _configure_dirs(monkeypatch, tmp_path)

    run_row = {
        "id": run_id,
        "title": "No DOM Data Meeting",
        "created_at": "2026-01-01T00:00:00+00:00",
        "state": "chunk_processing",
        "folder_path": None,
        "error_message": None,
        "client_name": "",
    }
    _install_fakes(monkeypatch, run_row)

    orchestrator_streaming.working_dir_for(run_id)  # no speaker_events.json written at all

    state = chunked_state.get_or_create(run_id)
    state.processed_segments.extend(
        [
            SpeakerSegment(start=0.0, end=1.0, speaker="Speaker 1 (chunk@0.0)", text="first line"),
            SpeakerSegment(start=1.5, end=2.5, speaker="Speaker 1 (chunk@0.0)", text="second line same person"),
        ]
    )

    orchestrator_streaming.finalize_run(run_id)

    folder = Path(run_row["folder_path"])
    transcript = json.loads((folder / "transcript.json").read_text(encoding="utf-8"))
    speakers = [seg["speaker"] for seg in transcript["segments"]]

    # Both lines belong to the same chunk-tagged placeholder group, so they
    # must share one label, not two different ones.
    assert speakers[0] == speakers[1] == "Unidentified speaker 1"
    excerpt = transcript["unidentified_speaker_excerpts"]["Unidentified speaker 1"]
    assert "first line" in excerpt
    assert "second line same person" in excerpt


def test_finalize_run_with_no_speech_skips_gemini_and_still_saves(tmp_path, monkeypatch, run_id):
    # Real production crash: a short test call where nobody spoke produced
    # zero transcribed segments, and calling Gemini with an essentially
    # empty transcript made it return non-JSON text, crashing json.loads()
    # ("Unterminated string starting at: line 1 column 82"). finalize_run()
    # must recognize "no segments at all" and skip Gemini entirely rather
    # than reach extract_meeting_facts() with nothing to summarize.
    _configure_dirs(monkeypatch, tmp_path)

    run_row = {
        "id": run_id,
        "title": "Silent Test Call",
        "created_at": "2026-01-01T00:00:00+00:00",
        "state": "chunk_processing",
        "folder_path": None,
        "error_message": None,
        "client_name": "",
    }
    _install_fakes(monkeypatch, run_row)

    def _fail_if_called(transcript_text):
        raise AssertionError("extract_meeting_facts() must not be called for a transcript with zero segments")

    monkeypatch.setattr(orchestrator_streaming.docgen_engine, "extract_meeting_facts", _fail_if_called)

    orchestrator_streaming.working_dir_for(run_id)  # no chunks/segments at all -- nobody spoke

    orchestrator_streaming.finalize_run(run_id)

    assert run_row["state"] == "saved"
    folder = Path(run_row["folder_path"])
    facts = json.loads((folder / "facts.json").read_text(encoding="utf-8"))
    assert facts["requirements"] == []
    assert facts["business_process"] is None

    transcript = json.loads((folder / "transcript.json").read_text(encoding="utf-8"))
    assert transcript["segments"] == []


def test_finalize_run_persists_client_name_into_transcript(tmp_path, monkeypatch, run_id):
    _configure_dirs(monkeypatch, tmp_path)

    run_row = {
        "id": run_id,
        "title": "Weekly Sync",
        "created_at": "2026-01-01T00:00:00+00:00",
        "state": "chunk_processing",
        "folder_path": None,
        "error_message": None,
        "client_name": "Acme Corp",
    }
    _install_fakes(monkeypatch, run_row)

    orchestrator_streaming.working_dir_for(run_id)  # no segments -- keeps this test focused on client_name

    orchestrator_streaming.finalize_run(run_id)

    folder = Path(run_row["folder_path"])
    transcript = json.loads((folder / "transcript.json").read_text(encoding="utf-8"))
    assert transcript["client_name"] == "Acme Corp"


def test_finalize_run_makes_folder_and_transcript_available_before_facts_finish(
    tmp_path, monkeypatch, run_id
):
    # The whole point of incremental delivery: folder_path (and the
    # transcript files, which don't depend on Gemini at all) must be set
    # on the run as soon as they exist, not held back until state=="saved".
    _configure_dirs(monkeypatch, tmp_path)

    run_row = {
        "id": run_id,
        "title": "Weekly Sync",
        "created_at": "2026-01-01T00:00:00+00:00",
        "state": "chunk_processing",
        "folder_path": None,
        "error_message": None,
        "client_name": "",
    }
    states_seen_with_folder_path: list[str] = []

    monkeypatch.setattr(db, "get_run", lambda rid: dict(run_row))

    def fake_update_run(rid, **fields):
        run_row.update(fields)
        if run_row.get("folder_path"):
            states_seen_with_folder_path.append(run_row["state"])
        return dict(run_row)

    monkeypatch.setattr(db, "update_run", fake_update_run)
    monkeypatch.setattr(
        orchestrator_streaming.docgen_engine,
        "extract_meeting_facts",
        lambda transcript_text: {"requirements": []},
    )

    orchestrator_streaming.working_dir_for(run_id)
    state = chunked_state.get_or_create(run_id)
    state.processed_segments.append(SpeakerSegment(start=0.0, end=1.0, speaker="Rahul Verma", text="hi"))

    orchestrator_streaming.finalize_run(run_id)

    # folder_path was already set while still "extracting_facts" -- not only
    # once the run reached "saved".
    assert "extracting_facts" in states_seen_with_folder_path
    folder = Path(run_row["folder_path"])
    assert (folder / "transcript.json").is_file()
    assert (folder / "transcript.txt").is_file()
