"""Exercises orchestrator_streaming.finalize_run()'s wiring end-to-end with
fakes for db/Gemini/PDF-render/storage -- the real bug this guards against
(finalize_run() never called resolve_speaker_names() at all) could only be
caught by a test that runs the actual finalize_run() function, not by
speaker_names.py's own pure-function unit tests.
"""
import json

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


def _install_fakes(monkeypatch, run_row, saved_files):
    monkeypatch.setattr(db, "get_run", lambda rid: dict(run_row))

    def fake_update_run(rid, **fields):
        run_row.update(fields)
        return dict(run_row)

    monkeypatch.setattr(db, "update_run", fake_update_run)

    def fake_generate_documents(title, transcript_text, attendees=None, meeting_date="", recorder=None):
        saved_files["_transcript_text_seen_by_docgen"] = transcript_text
        saved_files["_attendees_seen_by_docgen"] = attendees
        saved_files["_meeting_date_seen_by_docgen"] = meeting_date
        return {
            "mom": {"markdown_body": "mom body"},
            "requirement_gathering": {"markdown_body": "rg body"},
            "action_points": {"markdown_body": "ap body"},
        }

    monkeypatch.setattr(orchestrator_streaming.docgen_engine, "generate_documents", fake_generate_documents)

    def fake_render(docs, mom_path, rg_path, ap_path, recorder=None):
        mom_path.write_bytes(b"%PDF-fake")
        rg_path.write_bytes(b"%PDF-fake")
        ap_path.write_bytes(b"%PDF-fake")

    monkeypatch.setattr(orchestrator_streaming, "render_documents_to_pdf", fake_render)

    def fake_save_meeting_folder(base_dir, title, when, files):
        saved_files.update(files)
        return base_dir / "Fake Meeting Folder"

    monkeypatch.setattr(orchestrator_streaming, "save_meeting_folder", fake_save_meeting_folder)


def test_finalize_run_resolves_dom_names_and_never_leaves_bare_placeholders(
    tmp_path, monkeypatch, run_id
):
    monkeypatch.setattr(settings, "working_dir", tmp_path)
    # finalize_run() also persists cross-run stage-duration history (see
    # app/progress.py) -- redirect it away from the real project's data/
    # directory so this test can't pollute it with fake durations.
    monkeypatch.setattr(settings, "project_root", tmp_path)

    run_row = {
        "id": run_id,
        "title": "Weekly Sync",
        "created_at": "2026-01-01T00:00:00+00:00",
        "state": "chunk_processing",
        "folder_path": None,
        "error_message": None,
    }
    saved_files: dict = {}
    _install_fakes(monkeypatch, run_row, saved_files)

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

    transcript = json.loads(saved_files["transcript.json"])
    speakers = [seg["speaker"] for seg in transcript["segments"]]

    assert speakers[0] == "Priya Shah"
    assert speakers[1] == "Rahul Verma"
    assert speakers[2] == "Unidentified speaker 1"
    assert "who is speaking right now" in transcript["unidentified_speaker_excerpts"]["Unidentified speaker 1"]

    for speaker in speakers:
        assert speaker != "Unknown"
        assert not speaker.startswith("Speaker ")

    # Real names (roster-independent here, since no attendee_roster.json was
    # written) still make it into the deterministic attendee list; the
    # unidentified placeholder must not.
    assert saved_files["_attendees_seen_by_docgen"] == ["Priya Shah", "Rahul Verma"]
    assert transcript["attendees"] == ["Priya Shah", "Rahul Verma"]

    assert run_row["state"] == "saved"
    assert run_row["folder_path"] is not None


def test_finalize_run_without_speaker_events_still_fills_placeholders(tmp_path, monkeypatch, run_id):
    monkeypatch.setattr(settings, "working_dir", tmp_path)
    # finalize_run() also persists cross-run stage-duration history (see
    # app/progress.py) -- redirect it away from the real project's data/
    # directory so this test can't pollute it with fake durations.
    monkeypatch.setattr(settings, "project_root", tmp_path)

    run_row = {
        "id": run_id,
        "title": "No DOM Data Meeting",
        "created_at": "2026-01-01T00:00:00+00:00",
        "state": "chunk_processing",
        "folder_path": None,
        "error_message": None,
    }
    saved_files: dict = {}
    _install_fakes(monkeypatch, run_row, saved_files)

    orchestrator_streaming.working_dir_for(run_id)  # no speaker_events.json written at all

    state = chunked_state.get_or_create(run_id)
    state.processed_segments.extend(
        [
            SpeakerSegment(start=0.0, end=1.0, speaker="Speaker 1 (chunk@0.0)", text="first line"),
            SpeakerSegment(start=1.5, end=2.5, speaker="Speaker 1 (chunk@0.0)", text="second line same person"),
        ]
    )

    orchestrator_streaming.finalize_run(run_id)

    transcript = json.loads(saved_files["transcript.json"])
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
    # than reach generate_documents() with nothing to summarize.
    monkeypatch.setattr(settings, "working_dir", tmp_path)
    monkeypatch.setattr(settings, "project_root", tmp_path)

    run_row = {
        "id": run_id,
        "title": "Silent Test Call",
        "created_at": "2026-01-01T00:00:00+00:00",
        "state": "chunk_processing",
        "folder_path": None,
        "error_message": None,
    }
    saved_files: dict = {}
    _install_fakes(monkeypatch, run_row, saved_files)

    def _fail_if_called(title, transcript_text, recorder=None):
        raise AssertionError("generate_documents() must not be called for a transcript with zero segments")

    monkeypatch.setattr(orchestrator_streaming.docgen_engine, "generate_documents", _fail_if_called)

    orchestrator_streaming.working_dir_for(run_id)  # no chunks/segments at all -- nobody spoke

    orchestrator_streaming.finalize_run(run_id)

    assert run_row["state"] == "saved"
    assert "No speech was captured" in saved_files["MOM.md"]

    transcript = json.loads(saved_files["transcript.json"])
    assert transcript["segments"] == []
