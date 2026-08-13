from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Optional

from faster_whisper import BatchedInferencePipeline, WhisperModel

from app.config import settings
from app.pipeline.timing import TimingRecorder, timed


@dataclass
class TranscribedSegment:
    start: float
    end: float
    text: str


@lru_cache(maxsize=1)
def _model() -> WhisperModel:
    # Loaded once per process — model load is the expensive part on CPU.
    # cpu_threads = CTranslate2 intra-op threads (parallelism within one
    # transcribe() call); num_workers = inter-op/pipeline workers (only
    # matters once transcribe() is called concurrently from multiple
    # threads, e.g. the chunk fallback path running transcribe()+pyannote
    # concurrently) -- both tuned to this box's core count instead of left
    # at CTranslate2's untuned default.
    return WhisperModel(
        settings.whisper_model_size,
        device="cpu",
        compute_type=settings.whisper_compute_type,
        cpu_threads=settings.whisper_cpu_threads,
        num_workers=settings.whisper_cpu_threads,
    )


@lru_cache(maxsize=1)
def _batched_model() -> BatchedInferencePipeline:
    # ~2x CPU/int8 speedup over the plain WhisperModel.transcribe() call,
    # per faster-whisper's own README CPU benchmark -- a drop-in
    # replacement, VAD-chunks the audio and decodes chunks in batches.
    return BatchedInferencePipeline(model=_model())


def transcribe(
    audio_path: Path,
    recorder: Optional[TimingRecorder] = None,
    stage: str = "transcribe",
    repeated: bool = False,
    task: Optional[str] = None,
) -> list[TranscribedSegment]:
    def _run() -> list:
        segments, _info = _batched_model().transcribe(
            str(audio_path),
            vad_filter=True,
            batch_size=settings.whisper_batch_size,
            # BatchedInferencePipeline defaults without_timestamps=True
            # (opposite of plain WhisperModel.transcribe) -- explicit here
            # since diarization alignment depends on meaningful start/end.
            without_timestamps=False,
            # "translate" (the default via settings.whisper_task) decodes
            # straight to English regardless of spoken language; "transcribe"
            # keeps the original language. task=None falls back to settings
            # so existing positional call sites don't need updating.
            task=task if task is not None else settings.whisper_task,
        )
        return list(segments)

    if recorder is None:
        segments = _run()
    else:
        with timed(recorder, stage, repeated=repeated):
            segments = _run()
    return [
        TranscribedSegment(start=segment.start, end=segment.end, text=segment.text.strip())
        for segment in segments
    ]
