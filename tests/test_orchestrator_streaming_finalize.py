"""Exercises orchestrator_streaming.finalize_run()'s wiring end-to-end with
fakes only for Gemini (docgen_engine.generate_documents) and PDF rendering
(markdown_to_pdf, which needs a real Playwright browser) -- the real bug
this originally guarded against (finalize_run() never called
resolve_speaker_names() at all) could only be caught by a test that runs
the actual finalize_run() function, not by speaker_names.py's own
pure-function unit tests. app.storage's create_meeting_folder()/
write_meeting_file() run for real against a tmp_path
settings.base_storage_dir, since they're cheap plain filesystem calls and
running them for real is better coverage than faking them too -- this also
directly exercises the incremental-delivery behavior (each document
written to its final folder as soon as its own Gemini call "completes").
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

    def fake_generate_documents(
        title, transcript_text, attendees=None, meeting_date="", recorder=None, on_document_ready=None
    ):
        seen["transcript_text"] = transcript_text
        seen["attendees"] = attendees
        seen["meeting_date"] = meeting_date
        mom = {"markdown_body": "mom body"}
        meeting_analysis = {"markdown_body": "analysis body"}
        # Real generate_documents() fires on_document_ready per-document as
        # each Gemini call completes -- order isn't guaranteed there, so
        # exercise that here too rather than assuming mom always "finishes"
        # first.
        if on_document_ready is not None:
            on_document_ready("meeting_analysis", meeting_analysis)
            on_document_ready("mom", mom)
        return {"mom": mom, "meeting_analysis": meeting_analysis}

    monkeypatch.setattr(orchestrator_streaming.docgen_engine, "generate_documents", fake_generate_documents)

    def fake_markdown_to_pdf(markdown_text, dest_path):
        dest_path.write_bytes(b"%PDF-fake")
        return dest_path

    monkeypatch.setattr(orchestrator_streaming, "markdown_to_pdf", fake_markdown_to_pdf)

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
    }
    seen = _install_fakes(monkeypatch, run_row)

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
    # written) still make it into the deterministic attendee list; the
    # unidentified placeholder must not.
    assert seen["attendees"] == ["Priya Shah", "Rahul Verma"]
    assert transcript["attendees"] == ["Priya Shah", "Rahul Verma"]

    assert run_row["state"] == "saved"
    assert run_row["folder_path"] is not None
    assert (folder / "MOM.md").read_text(encoding="utf-8") == "mom body"
    assert (folder / "MOM.pdf").read_bytes() == b"%PDF-fake"
    assert (folder / "Meeting_Analysis.md").read_text(encoding="utf-8") == "analysis body"
    assert (folder / "Meeting_Analysis.pdf").read_bytes() == b"%PDF-fake"


def test_finalize_run_without_speaker_events_still_fills_placeholders(tmp_path, monkeypatch, run_id):
    _configure_dirs(monkeypatch, tmp_path)

    run_row = {
        "id": run_id,
        "title": "No DOM Data Meeting",
        "created_at": "2026-01-01T00:00:00+00:00",
        "state": "chunk_processing",
        "folder_path": None,
        "error_message": None,
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
    # than reach generate_documents() with nothing to summarize.
    _configure_dirs(monkeypatch, tmp_path)

    run_row = {
        "id": run_id,
        "title": "Silent Test Call",
        "created_at": "2026-01-01T00:00:00+00:00",
        "state": "chunk_processing",
        "folder_path": None,
        "error_message": None,
    }
    _install_fakes(monkeypatch, run_row)

    def _fail_if_called(title, transcript_text, recorder=None, **kwargs):
        raise AssertionError("generate_documents() must not be called for a transcript with zero segments")

    monkeypatch.setattr(orchestrator_streaming.docgen_engine, "generate_documents", _fail_if_called)

    orchestrator_streaming.working_dir_for(run_id)  # no chunks/segments at all -- nobody spoke

    orchestrator_streaming.finalize_run(run_id)

    assert run_row["state"] == "saved"
    folder = Path(run_row["folder_path"])
    assert "No speech was captured" in (folder / "MOM.md").read_text(encoding="utf-8")

    transcript = json.loads((folder / "transcript.json").read_text(encoding="utf-8"))
    assert transcript["segments"] == []


def test_finalize_run_makes_folder_and_transcript_available_before_documents_finish(
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
    }
    states_seen_with_folder_path: list[str] = []

    monkeypatch.setattr(db, "get_run", lambda rid: dict(run_row))

    def fake_update_run(rid, **fields):
        run_row.update(fields)
        if run_row.get("folder_path"):
            states_seen_with_folder_path.append(run_row["state"])
        return dict(run_row)

    monkeypatch.setattr(db, "update_run", fake_update_run)

    def fake_generate_documents(
        title, transcript_text, attendees=None, meeting_date="", recorder=None, on_document_ready=None
    ):
        mom = {"markdown_body": "mom body"}
        meeting_analysis = {"markdown_body": "analysis body"}
        if on_document_ready is not None:
            on_document_ready("mom", mom)
            on_document_ready("meeting_analysis", meeting_analysis)
        return {"mom": mom, "meeting_analysis": meeting_analysis}

    monkeypatch.setattr(orchestrator_streaming.docgen_engine, "generate_documents", fake_generate_documents)
    monkeypatch.setattr(
        orchestrator_streaming, "markdown_to_pdf", lambda text, dest_path: dest_path.write_bytes(b"%PDF-fake")
    )

    orchestrator_streaming.working_dir_for(run_id)
    state = chunked_state.get_or_create(run_id)
    state.processed_segments.append(SpeakerSegment(start=0.0, end=1.0, speaker="Rahul Verma", text="hi"))

    orchestrator_streaming.finalize_run(run_id)

    # folder_path was already set while still "generating_docs" -- not only
    # once the run reached "saved".
    assert "generating_docs" in states_seen_with_folder_path
    folder = Path(run_row["folder_path"])
    assert (folder / "transcript.json").is_file()
    assert (folder / "transcript.txt").is_file()
