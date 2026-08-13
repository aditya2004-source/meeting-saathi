"""Covers app.pipeline.diarize._utterances_to_segments -- the pure
conversion from AssemblyAI's utterances (speaker_labels=True) to this
module's SpeakerSegment shape, factored out so it's testable without a
network call. Uses SimpleNamespace to stand in for AssemblyAI SDK Utterance
objects (only .text/.start/.end/.speaker are read).
"""
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from app.pipeline.diarize import _assemblyai_transcribe_and_diarize, _utterances_to_segments


def _utterance(text, start_ms, end_ms, speaker):
    return SimpleNamespace(text=text, start=start_ms, end=end_ms, speaker=speaker)


def test_converts_utterances_to_speaker_segments_with_seconds_timestamps():
    utterances = [_utterance("hello there", 0, 2000, "A")]

    segments = _utterances_to_segments(utterances)

    assert len(segments) == 1
    assert segments[0].text == "hello there"
    assert segments[0].start == 0.0
    assert segments[0].end == 2.0
    assert segments[0].speaker == "Speaker 1"


def test_assigns_friendly_names_in_order_of_first_appearance():
    utterances = [
        _utterance("first", 0, 1000, "B"),
        _utterance("second", 1000, 2000, "A"),
        _utterance("third", 2000, 3000, "B"),
    ]

    segments = _utterances_to_segments(utterances)

    # "B" spoke first, so it becomes "Speaker 1" even though AssemblyAI's
    # own raw label alphabetically precedes "A" -- order of appearance is
    # what matters, matching _align()'s pyannote-label convention.
    assert [s.speaker for s in segments] == ["Speaker 1", "Speaker 2", "Speaker 1"]


def test_skips_empty_or_whitespace_only_utterances():
    utterances = [
        _utterance("", 0, 1000, "A"),
        _utterance("   ", 1000, 2000, "A"),
        _utterance("real text", 2000, 3000, "A"),
    ]

    segments = _utterances_to_segments(utterances)

    assert len(segments) == 1
    assert segments[0].text == "real text"


def test_empty_utterance_list_returns_empty_segments():
    assert _utterances_to_segments([]) == []


def test_assemblyai_config_requests_language_detection():
    # Without this, AssemblyAI defaults to English-only decoding, which
    # mistranscribes Hindi speech as garbled English instead of accurate
    # Hindi text -- a real bug this branch (the pyannote-fallback path's
    # paid alternative) had until this was added.
    fake_transcript = MagicMock(utterances=[])

    with patch("assemblyai.TranscriptionConfig") as mock_config_cls, patch(
        "assemblyai.Transcriber"
    ) as mock_transcriber_cls:
        mock_transcriber_cls.return_value.transcribe.return_value = fake_transcript
        _assemblyai_transcribe_and_diarize(Path("irrelevant.webm"))

    assert mock_config_cls.call_args.kwargs["language_detection"] is True
