from pathlib import Path
from unittest.mock import MagicMock, patch

from app.pipeline.diarize import _align, _overlap, _pyannote_turns
from app.pipeline.transcribe import TranscribedSegment


def test_pyannote_turns_reads_exclusive_speaker_diarization():
    # Regression test: pyannote 4.x's "speaker-diarization-community-1"
    # pipeline returns a DiarizeOutput wrapper (speaker_diarization /
    # exclusive_speaker_diarization / speaker_embeddings), not an
    # Annotation directly -- calling .itertracks() on the wrapper itself
    # raises "'DiarizeOutput' object has no attribute 'itertracks'".
    # _pyannote_turns must read from .exclusive_speaker_diarization.
    fake_turn_1 = MagicMock(start=0.0, end=1.0)
    fake_turn_2 = MagicMock(start=1.0, end=2.0)
    fake_output = MagicMock(spec=["exclusive_speaker_diarization"])
    fake_output.exclusive_speaker_diarization.itertracks.return_value = [
        (fake_turn_1, None, "SPEAKER_00"),
        (fake_turn_2, None, "SPEAKER_01"),
    ]
    fake_pipeline = MagicMock(return_value=fake_output)

    with patch("app.pipeline.diarize._pyannote_pipeline", return_value=fake_pipeline), patch(
        "app.pipeline.diarize._load_waveform", return_value={}
    ):
        turns = _pyannote_turns(Path("irrelevant.webm"))

    assert turns == [(0.0, 1.0, "SPEAKER_00"), (1.0, 2.0, "SPEAKER_01")]
    fake_output.exclusive_speaker_diarization.itertracks.assert_called_once_with(yield_label=True)


def test_overlap_no_intersection():
    assert _overlap(0.0, 1.0, 2.0, 3.0) == 0.0


def test_overlap_partial_intersection():
    assert _overlap(0.0, 5.0, 3.0, 8.0) == 2.0


def test_align_assigns_speaker_of_containing_turn():
    segments = [TranscribedSegment(start=1.0, end=2.0, text="hello")]
    turns = [(0.0, 5.0, "SPEAKER_00"), (5.0, 10.0, "SPEAKER_01")]

    aligned = _align(segments, turns)

    assert len(aligned) == 1
    assert aligned[0].speaker == "Speaker 1"
    assert aligned[0].text == "hello"


def test_align_picks_turn_with_greater_overlap_when_segment_spans_speaker_change():
    # Segment [4, 8] overlaps SPEAKER_00 by 1s (4-5) and SPEAKER_01 by 3s (5-8),
    # so it should be attributed to SPEAKER_01, the larger overlap.
    segments = [TranscribedSegment(start=4.0, end=8.0, text="switching speakers mid-sentence")]
    turns = [(0.0, 5.0, "SPEAKER_00"), (5.0, 10.0, "SPEAKER_01")]

    aligned = _align(segments, turns)

    # Only one segment is aligned here, so only one friendly name is ever
    # minted -- and it's minted for whichever raw label actually won
    # (SPEAKER_01), not for SPEAKER_00's first appearance in `turns`.
    assert aligned[0].speaker == "Speaker 1"


def test_align_labels_unknown_when_no_turns_overlap():
    segments = [TranscribedSegment(start=100.0, end=101.0, text="silence gap")]
    turns = [(0.0, 5.0, "SPEAKER_00")]

    aligned = _align(segments, turns)

    assert aligned[0].speaker == "Unknown"


def test_align_skips_empty_text_segments():
    segments = [
        TranscribedSegment(start=0.0, end=1.0, text=""),
        TranscribedSegment(start=1.0, end=2.0, text="real content"),
    ]
    turns = [(0.0, 5.0, "SPEAKER_00")]

    aligned = _align(segments, turns)

    assert len(aligned) == 1
    assert aligned[0].text == "real content"


def test_align_stable_friendly_names_across_multiple_segments():
    segments = [
        TranscribedSegment(start=0.0, end=1.0, text="a"),
        TranscribedSegment(start=6.0, end=7.0, text="b"),
        TranscribedSegment(start=1.5, end=2.5, text="c"),
    ]
    turns = [(0.0, 5.0, "SPEAKER_00"), (5.0, 10.0, "SPEAKER_01")]

    aligned = _align(segments, turns)

    assert [s.speaker for s in aligned] == ["Speaker 1", "Speaker 2", "Speaker 1"]
