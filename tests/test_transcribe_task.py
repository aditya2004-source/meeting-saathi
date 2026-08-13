"""Covers app.pipeline.transcribe's new `task` param -- added so a bilingual
Hindi/English meeting can be transcribed straight to English text (Whisper's
built-in task="translate") instead of leaking mixed-language segments into
the Gemini prompt. settings.whisper_task="translate" is the default;
`task=` lets a caller override it explicitly.
"""
from pathlib import Path
from unittest.mock import MagicMock, patch

from app.config import settings
from app.pipeline.transcribe import transcribe


def _fake_batched_model(segments=()):
    model = MagicMock()
    model.transcribe.return_value = (list(segments), MagicMock())
    return model


def test_transcribe_defaults_task_from_settings():
    with patch("app.pipeline.transcribe._batched_model", return_value=_fake_batched_model()) as mock_model:
        transcribe(Path("irrelevant.webm"))

    assert mock_model.return_value.transcribe.call_args.kwargs["task"] == settings.whisper_task


def test_transcribe_explicit_task_overrides_settings():
    with patch("app.pipeline.transcribe._batched_model", return_value=_fake_batched_model()) as mock_model:
        transcribe(Path("irrelevant.webm"), task="transcribe")

    assert mock_model.return_value.transcribe.call_args.kwargs["task"] == "transcribe"


def test_transcribe_positional_call_sites_still_work():
    # diarize.py's ThreadPoolExecutor calls transcribe positionally:
    # pool.submit(transcribe, mixed_audio_path, recorder, "chunk_transcribe", True)
    with patch("app.pipeline.transcribe._batched_model", return_value=_fake_batched_model()) as mock_model:
        transcribe(Path("irrelevant.webm"), None, "chunk_transcribe", True)

    assert mock_model.return_value.transcribe.call_args.kwargs["task"] == settings.whisper_task
