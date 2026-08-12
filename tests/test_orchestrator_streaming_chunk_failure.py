"""Covers orchestrator_streaming's per-chunk failure isolation -- flagged
(but not fixed) after a session where a single chunk's diarize_chunk()
exception was found to fail the *entire* run via db.mark_failed(),
discarding every other already-processed chunk's segments even though the
rest of the meeting was fine. A bad ~50s audio window shouldn't cost the
whole meeting's transcript.
"""
import pytest

from app import chunked_state, db, orchestrator_streaming
from app.pipeline.diarize import SpeakerSegment


@pytest.fixture
def run_id():
    return "test-run-chunk-failure"


@pytest.fixture(autouse=True)
def _clean_chunk_state(run_id):
    yield
    chunked_state.drop(run_id)


def test_one_chunk_failing_does_not_fail_the_run_or_lose_other_chunks(tmp_path, monkeypatch, run_id):
    monkeypatch.setattr(orchestrator_streaming.settings, "working_dir", tmp_path)
    orchestrator_streaming.working_dir_for(run_id)

    monkeypatch.setattr(orchestrator_streaming, "probe_duration_seconds", lambda path: 1.0)

    good_segment = SpeakerSegment(start=0.0, end=1.0, speaker="Rahul Verma", text="fine chunk")

    def fake_diarize_chunk(chunk_path, events, chunk_start_offset, chunk_end_offset, recorder=None):
        if "bad" in chunk_path.name:
            raise RuntimeError("simulated corrupt audio chunk")
        return [good_segment]

    monkeypatch.setattr(orchestrator_streaming, "diarize_chunk", fake_diarize_chunk)

    mark_failed_calls = []
    monkeypatch.setattr(db, "mark_failed", lambda rid, exc: mark_failed_calls.append((rid, exc)))
    monkeypatch.setattr(db, "get_run", lambda rid: {"id": rid, "state": "chunk_processing"})
    monkeypatch.setattr(db, "update_run", lambda rid, **fields: None)

    good_path = tmp_path / "good.webm"
    bad_path = tmp_path / "bad.webm"
    good_path.write_bytes(b"fake")
    bad_path.write_bytes(b"fake")

    orchestrator_streaming._process_chunk_then_maybe_finalize(run_id, 0, good_path, final=False)
    orchestrator_streaming._process_chunk_then_maybe_finalize(run_id, 1, bad_path, final=False)

    assert mark_failed_calls == []  # the whole run must NOT be marked failed

    state = chunked_state.get_or_create(run_id)
    assert 0 not in state.pending_sequences
    assert 1 not in state.pending_sequences  # failed chunk still clears pending, so finalize can proceed
    assert state.processed_segments == [good_segment]  # good chunk's segment survives
