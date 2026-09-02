from unittest.mock import patch

from app.pipeline.diarize import SpeakerSegment
from app.pipeline.translate import (
    apply_translations,
    maybe_translate_segments_to_english,
    segment_needs_translation,
)


def _seg(text, speaker="Speaker 1", start=0.0):
    return SpeakerSegment(start=start, end=start + 1.0, speaker=speaker, text=text)


def test_segment_needs_translation_detects_devanagari_and_urdu():
    assert segment_needs_translation("हेलो अश्विन सर मॉर्निंग")
    assert segment_needs_translation("okay बढ़िया")  # mixed
    assert segment_needs_translation("سلام")
    assert not segment_needs_translation("Hello Ashwin sir, good morning")
    assert not segment_needs_translation("")


def test_apply_translations_replaces_by_index_and_keeps_the_rest():
    segments = [_seg("हेलो", start=0), _seg("Already English", start=1), _seg("नमस्ते", start=2)]
    out = apply_translations(segments, {0: "Hello", 2: "  "})
    assert [s.text for s in out] == ["Hello", "Already English", "नमस्ते"]
    # unchanged segments keep start/end/speaker
    assert out[1].start == 1.0 and out[1].speaker == "Speaker 1"


def test_maybe_translate_noop_when_whisper_task_not_translate():
    segments = [_seg("हेलो अश्विन सर")]
    with patch("app.pipeline.translate.settings") as s, patch(
        "app.pipeline.translate.docgen_engine.translate_transcript_segments"
    ) as mock_call:
        s.whisper_task = "transcribe"
        out = maybe_translate_segments_to_english(segments)
    mock_call.assert_not_called()
    assert out == segments


def test_maybe_translate_noop_when_all_segments_are_english():
    segments = [_seg("Hello there"), _seg("Let us reconnect at 11")]
    with patch("app.pipeline.translate.settings") as s, patch(
        "app.pipeline.translate.docgen_engine.translate_transcript_segments"
    ) as mock_call:
        s.whisper_task = "translate"
        out = maybe_translate_segments_to_english(segments)
    mock_call.assert_not_called()
    assert out == segments


def test_maybe_translate_calls_gemini_for_non_english_and_applies_result():
    segments = [
        _seg("हेलो अश्विन सर मॉर्निंग", start=0),
        _seg("Okay.", start=100),
        _seg("अग्यारव आगे कनेक्ट करिये", start=55),
    ]
    fake = {"translations": [
        {"index": 0, "text": "Hello Ashwin sir, morning"},
        {"index": 2, "text": "Connect at 11 onwards"},
    ]}
    with patch("app.pipeline.translate.settings") as s, patch(
        "app.pipeline.translate.docgen_engine.translate_transcript_segments", return_value=fake
    ) as mock_call:
        s.whisper_task = "translate"
        out = maybe_translate_segments_to_english(segments)
    # only the 2 non-English segments were sent, by index
    sent = mock_call.call_args[0][0]
    assert [item["index"] for item in sent] == [0, 2]
    assert [seg.text for seg in out] == [
        "Hello Ashwin sir, morning",
        "Okay.",
        "Connect at 11 onwards",
    ]


def test_maybe_translate_is_fail_soft_on_gemini_error():
    segments = [_seg("हेलो अश्विन सर")]
    with patch("app.pipeline.translate.settings") as s, patch(
        "app.pipeline.translate.docgen_engine.translate_transcript_segments",
        side_effect=RuntimeError("boom"),
    ):
        s.whisper_task = "translate"
        out = maybe_translate_segments_to_english(segments)
    assert out == segments
