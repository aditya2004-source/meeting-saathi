"""Covers Phase 3's AssemblyAI-primary switch: confirmed via the SaaS-
conversion audit that merely setting ASSEMBLYAI_API_KEY did NOT change
diarize_chunk()'s good-DOM-coverage branch at all -- it always called local
faster-whisper there regardless. settings.transcription_provider is the new
explicit flag that actually changes this. No real AssemblyAI/network access
in this test run -- everything here mocks the `assemblyai` SDK the same way
tests/test_assemblyai_diarize.py already does; end-to-end verification
against real audio is the founder's to do once a real API key is available.
"""
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from app.config import settings
from app.pipeline import diarize as diarize_module
from app.pipeline.diarize import _assemblyai_transcribe_only, diarize_chunk
from app.pipeline.speaker_names import SpeakerEvent


def _utterance(text, start_ms, end_ms):
    return SimpleNamespace(text=text, start=start_ms, end=end_ms, speaker="A")


def test_assemblyai_transcribe_only_returns_plain_transcribed_segments_in_chunk_local_seconds():
    fake_transcript = MagicMock(utterances=[_utterance("hello there", 0, 2000), _utterance("  ", 2000, 3000)])

    with patch("assemblyai.TranscriptionConfig"), patch("assemblyai.Transcriber") as mock_transcriber_cls:
        mock_transcriber_cls.return_value.transcribe.return_value = fake_transcript
        segments = _assemblyai_transcribe_only(Path("irrelevant.webm"))

    assert len(segments) == 1  # blank utterance skipped
    assert segments[0].text == "hello there"
    assert segments[0].start == 0.0
    assert segments[0].end == 2.0
    assert not hasattr(segments[0], "speaker")  # TranscribedSegment, not SpeakerSegment -- no speaker field at all


def test_diarize_chunk_uses_local_whisper_by_default_even_with_good_coverage_and_a_key_set(monkeypatch, tmp_path):
    # Regression test for the exact bug this phase fixes: before
    # transcription_provider existed, this was already true regardless of
    # the key -- confirming "auto" (the default) preserves that.
    monkeypatch.setattr(settings, "transcription_provider", "auto")
    monkeypatch.setattr(settings, "assemblyai_api_key", "some-key")
    events = [SpeakerEvent("Priya", 0.0)]  # full coverage of the whole window

    with patch.object(diarize_module, "transcribe") as mock_transcribe, patch(
        "assemblyai.Transcriber"
    ) as mock_transcriber_cls:
        mock_transcribe.return_value = []
        diarize_chunk(tmp_path / "chunk.webm", events, chunk_start_offset=0.0, chunk_end_offset=50.0)

    mock_transcribe.assert_called_once()
    mock_transcriber_cls.assert_not_called()


def test_diarize_chunk_uses_assemblyai_when_forced_primary_with_good_coverage(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "transcription_provider", "assemblyai")
    monkeypatch.setattr(settings, "assemblyai_api_key", "some-key")
    events = [SpeakerEvent("Priya", 0.0)]
    fake_transcript = MagicMock(utterances=[])

    with patch.object(diarize_module, "transcribe") as mock_transcribe, patch(
        "assemblyai.TranscriptionConfig"
    ), patch("assemblyai.Transcriber") as mock_transcriber_cls:
        mock_transcriber_cls.return_value.transcribe.return_value = fake_transcript
        diarize_chunk(tmp_path / "chunk.webm", events, chunk_start_offset=0.0, chunk_end_offset=50.0)

    mock_transcribe.assert_not_called()
    mock_transcriber_cls.return_value.transcribe.assert_called_once()


def test_diarize_chunk_falls_back_to_local_whisper_when_forced_primary_but_no_key_set(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "transcription_provider", "assemblyai")
    monkeypatch.setattr(settings, "assemblyai_api_key", "")
    events = [SpeakerEvent("Priya", 0.0)]

    with patch.object(diarize_module, "transcribe") as mock_transcribe, patch(
        "assemblyai.Transcriber"
    ) as mock_transcriber_cls:
        mock_transcribe.return_value = []
        diarize_chunk(tmp_path / "chunk.webm", events, chunk_start_offset=0.0, chunk_end_offset=50.0)

    mock_transcribe.assert_called_once()
    mock_transcriber_cls.assert_not_called()


def test_sparse_coverage_fallback_branch_is_unaffected_by_transcription_provider(monkeypatch, tmp_path):
    # The pre-existing fallback (sparse DOM coverage) already used
    # AssemblyAI whenever a key was set, regardless of this new flag --
    # confirm that's still true and this phase didn't change it.
    monkeypatch.setattr(settings, "transcription_provider", "auto")
    monkeypatch.setattr(settings, "assemblyai_api_key", "some-key")
    fake_transcript = MagicMock(utterances=[])

    with patch.object(diarize_module, "transcribe") as mock_transcribe, patch(
        "assemblyai.TranscriptionConfig"
    ), patch("assemblyai.Transcriber") as mock_transcriber_cls:
        mock_transcriber_cls.return_value.transcribe.return_value = fake_transcript
        diarize_chunk(tmp_path / "chunk.webm", [], chunk_start_offset=0.0, chunk_end_offset=50.0)  # no events -> zero coverage

    mock_transcribe.assert_not_called()
    mock_transcriber_cls.return_value.transcribe.assert_called_once()
