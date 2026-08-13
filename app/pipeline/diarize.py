from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING, Optional

import numpy as np
import torch

from app.config import settings
from app.pipeline.timing import TimingRecorder, timed
from app.pipeline.transcribe import TranscribedSegment, transcribe

if TYPE_CHECKING:
    # app.pipeline.speaker_names imports SpeakerSegment from this module, so
    # a module-level import here would be circular -- diarize_chunk() below
    # imports it lazily at call time instead. Type-only import is safe.
    from app.pipeline.speaker_names import SpeakerEvent

_PYANNOTE_SAMPLE_RATE = 16000


def _load_waveform(path: Path) -> dict:
    """Decode audio into the in-memory {waveform, sample_rate} shape
    pyannote.audio accepts directly. pyannote 4.x normally decodes files
    itself via torchcodec, but torchcodec needs the system ffmpeg shared
    libraries, which this machine doesn't have installed -- PyAV (already a
    faster-whisper dependency, already confirmed working here) decodes the
    same webm/opus recordings without that extra system dependency.
    """
    import av

    container = av.open(str(path))
    stream = container.streams.audio[0]
    resampler = av.audio.resampler.AudioResampler(
        format="s16", layout="mono", rate=_PYANNOTE_SAMPLE_RATE
    )
    chunks = []
    for frame in container.decode(stream):
        for resampled in resampler.resample(frame):
            chunks.append(resampled.to_ndarray())
    container.close()

    if chunks:
        pcm = np.concatenate(chunks, axis=1)
    else:
        pcm = np.zeros((1, 0), dtype=np.int16)
    waveform = torch.from_numpy(pcm.astype(np.float32) / 32768.0)
    return {"waveform": waveform, "sample_rate": _PYANNOTE_SAMPLE_RATE}


def probe_duration_seconds(path: Path) -> float:
    """Measures a webm file's actual audio duration by demuxing and reading
    the last packet's timestamp. WebM files produced by MediaRecorder don't
    reliably carry a duration in the container header -- confirmed
    empirically on a real, complete recording (`container.duration` was
    `None`) -- so it must be measured this way rather than trusted from a
    header field. Used by the chunked pipeline to compute each chunk's
    global time offset from actual chunk lengths, not an assumed nominal
    duration (MediaRecorder restart-cycling doesn't produce exactly uniform
    chunks).
    """
    import av

    container = av.open(str(path))
    stream = container.streams.audio[0]
    time_base = stream.time_base
    last_pts = None
    for packet in container.demux(stream):
        if packet.pts is not None:
            last_pts = packet.pts
    container.close()
    if last_pts is None:
        return 0.0
    return float(last_pts * time_base)


@dataclass
class SpeakerSegment:
    start: float
    end: float
    speaker: str
    text: str


@lru_cache(maxsize=1)
def _pyannote_pipeline():
    from pyannote.audio import Pipeline

    # pyannote has no thread-count setting of its own -- its segmentation/
    # embedding models just inherit plain PyTorch thread config, which
    # defaults to 2 on this 4-core box unless set explicitly. Set once,
    # lazily, alongside this cached one-time pipeline load.
    torch.set_num_threads(settings.pyannote_torch_threads)
    return Pipeline.from_pretrained(
        "pyannote/speaker-diarization-community-1", token=settings.huggingface_token
    )


def _pyannote_turns(
    mixed_audio_path: Path,
    recorder: Optional[TimingRecorder] = None,
    stage: str = "pyannote",
    repeated: bool = False,
) -> list[tuple[float, float, str]]:
    # pyannote 4.x's "speaker-diarization-community-1" pipeline returns a
    # DiarizeOutput wrapper (speaker_diarization, exclusive_speaker_diarization,
    # speaker_embeddings), not an Annotation directly like older pipelines --
    # calling .itertracks() on it fails ("no attribute 'itertracks'"). Its
    # own docstring recommends exclusive_speaker_diarization specifically
    # for "downstream transcription" (non-overlapping turns), which is
    # exactly what aligning against Whisper's non-overlapping segments needs.
    waveform = _load_waveform(mixed_audio_path)
    if recorder is None:
        output = _pyannote_pipeline()(waveform)
    else:
        from pyannote.audio.pipelines.utils.hook import TimingHook

        with timed(recorder, stage, repeated=repeated), TimingHook() as hook:
            output = _pyannote_pipeline()(waveform, hook=hook)
        if "timing" in waveform:
            # pyannote's own per-substage breakdown (segmentation, embeddings,
            # clustering, ...) -- folded in for finer-grained diagnosis of
            # which part of diarization is actually the bottleneck.
            recorder.record(f"{stage}_substages", waveform["timing"])
    turns = []
    for turn, _, speaker_label in output.exclusive_speaker_diarization.itertracks(yield_label=True):
        turns.append((turn.start, turn.end, speaker_label))
    return turns


def _overlap(a_start: float, a_end: float, b_start: float, b_end: float) -> float:
    return max(0.0, min(a_end, b_end) - max(a_start, b_start))


def _align(
    transcribed: list[TranscribedSegment], turns: list[tuple[float, float, str]]
) -> list[SpeakerSegment]:
    # Map pyannote's raw labels ("SPEAKER_00") to friendlier, stable names
    # ("Speaker 1") in order of first appearance.
    friendly_names: dict[str, str] = {}

    def friendly(label: str) -> str:
        if label not in friendly_names:
            friendly_names[label] = f"Speaker {len(friendly_names) + 1}"
        return friendly_names[label]

    aligned = []
    for seg in transcribed:
        if not seg.text:
            continue
        best_label, best_overlap = "Unknown", 0.0
        for start, end, label in turns:
            ov = _overlap(seg.start, seg.end, start, end)
            if ov > best_overlap:
                best_label, best_overlap = label, ov
        speaker = friendly(best_label) if best_label != "Unknown" else "Unknown"
        aligned.append(SpeakerSegment(start=seg.start, end=seg.end, speaker=speaker, text=seg.text))
    return aligned


def _utterances_to_segments(utterances: list) -> list[SpeakerSegment]:
    """Pure conversion from AssemblyAI's utterances (each with .text,
    .start/.end in milliseconds, .speaker as a raw label like "A") to this
    module's SpeakerSegment shape, using the same "Speaker N" friendly-
    naming convention (in order of first appearance) as _align() above --
    so downstream code (resolve_speaker_names(), diarize_chunk()'s own
    chunk-tagging) doesn't need to know which diarization backend produced
    a given chunk. Factored out from _assemblyai_transcribe_and_diarize()
    so it's testable without a network call.
    """
    friendly_names: dict[str, str] = {}

    def friendly(label: str) -> str:
        if label not in friendly_names:
            friendly_names[label] = f"Speaker {len(friendly_names) + 1}"
        return friendly_names[label]

    segments = []
    for utt in utterances:
        text = (utt.text or "").strip()
        if not text:
            continue
        segments.append(
            SpeakerSegment(start=utt.start / 1000.0, end=utt.end / 1000.0, speaker=friendly(utt.speaker), text=text)
        )
    return segments


def _assemblyai_transcribe_and_diarize(
    mixed_audio_path: Path,
    recorder: Optional[TimingRecorder] = None,
    stage: str = "chunk_assemblyai",
    repeated: bool = False,
) -> list[SpeakerSegment]:
    """Replaces local transcribe()+_pyannote_turns() for one chunk with a
    single AssemblyAI call that does transcription + speaker diarization
    together, offloaded to their infrastructure -- see diarize_chunk()'s
    fallback branch for why (confirmed in production: local pyannote took
    up to 630s for one ~50s chunk on this box's 4 CPU cores)."""
    import assemblyai as aai

    aai.settings.api_key = settings.assemblyai_api_key
    config = aai.TranscriptionConfig(
        speech_models=["universal-3-5-pro", "universal-2"],
        speaker_labels=True,
        # Without this, AssemblyAI defaults to English-only decoding, which
        # mistranscribes Hindi speech as garbled English rather than actual
        # Hindi text. This makes it transcribe accurately in whatever
        # language is spoken instead -- note it does NOT translate to
        # English (AssemblyAI has no direct equivalent of Whisper's
        # task="translate"), so non-English text can still reach this
        # branch's output; the docgen prompts' language instruction is the
        # only translation layer for this specific fallback path.
        language_detection=True,
    )

    def _run():
        transcript = aai.Transcriber(config=config).transcribe(str(mixed_audio_path))
        if transcript.status == aai.TranscriptStatus.error:
            raise RuntimeError(f"AssemblyAI transcription failed: {transcript.error}")
        return transcript

    if recorder is None:
        transcript = _run()
    else:
        with timed(recorder, stage, repeated=repeated):
            transcript = _run()

    return _utterances_to_segments(transcript.utterances or [])


def diarize(mixed_audio_path: Path, recorder: Optional[TimingRecorder] = None) -> list[SpeakerSegment]:
    """The Chrome extension hands us one mixed recording (everyone's audio
    combined), so "who said what" always comes from running pyannote's
    speaker-diarization model on it and aligning its speaker turns to
    Whisper's transcribed segments by maximal timestamp overlap.

    Legacy whole-file path (used by the manual/fallback POST /meetings/upload
    route) -- left exactly as originally written. The chunked/streaming path
    uses diarize_chunk() below instead.
    """
    transcribed = transcribe(mixed_audio_path, recorder=recorder, stage="transcribe")
    turns = _pyannote_turns(mixed_audio_path, recorder=recorder, stage="pyannote")
    return _align(transcribed, turns)


def diarize_chunk(
    mixed_audio_path: Path,
    speaker_events: list["SpeakerEvent"],
    chunk_start_offset: float,
    chunk_end_offset: float,
    coverage_threshold: Optional[float] = None,
    recorder: Optional[TimingRecorder] = None,
) -> list[SpeakerSegment]:
    """DOM-primary, pyannote/AssemblyAI-fallback diarization for one short
    chunk of a live/streaming meeting. Returned segments carry GLOBAL
    timestamps (chunk_start_offset already added), so chunks can be
    concatenated across the whole meeting without further adjustment.

    Coverage of the chunk's time window by Meet active-speaker-tile events
    (speaker_events) is checked *before* transcribing -- coverage depends
    only on speaker_events + the window, not on audio content -- so the
    common case (good coverage) never touches pyannote/AssemblyAI at all,
    which is the entire point of this function (this fallback branch is the
    dominant cost when it does fire). Only when coverage is too sparse (e.g.
    screen-share, off-screen/virtualized participant tiles, or a solo call
    with no other DOM speaker signal at all) does it fall back -- to
    AssemblyAI if settings.assemblyai_api_key is set (one paid API call does
    transcription + diarization together, offloaded off this box's CPU), or
    else to local pyannote running concurrently with local transcribe()
    (same argument as diarize() above, just scoped to one short chunk
    instead of the whole meeting).
    """
    from app.pipeline.speaker_names import dom_coverage_ratio, speaker_from_dom_events

    threshold = (
        coverage_threshold if coverage_threshold is not None else settings.chunk_pyannote_coverage_threshold
    )
    coverage = dom_coverage_ratio(speaker_events, chunk_start_offset, chunk_end_offset)
    if recorder is not None:
        recorder.record("chunk_dom_coverage", coverage, repeated=True)

    if coverage >= threshold:
        transcribed = transcribe(
            mixed_audio_path, recorder=recorder, stage="chunk_transcribe", repeated=True
        )
        return speaker_from_dom_events(transcribed, speaker_events, chunk_start_offset)

    if settings.assemblyai_api_key:
        # Confirmed in production: the local path below (transcribe() +
        # pyannote concurrently) took up to 630s for one ~50s chunk on this
        # box's 4 CPU cores. One AssemblyAI call replaces both steps,
        # offloaded to their infrastructure instead of this box's cores.
        segments = _assemblyai_transcribe_and_diarize(
            mixed_audio_path, recorder=recorder, stage="chunk_assemblyai", repeated=True
        )
    else:
        with ThreadPoolExecutor(max_workers=2) as pool:
            transcribe_future = pool.submit(transcribe, mixed_audio_path, recorder, "chunk_transcribe", True)
            turns_future = pool.submit(_pyannote_turns, mixed_audio_path, recorder, "chunk_pyannote", True)
            transcribed = transcribe_future.result()
            turns = turns_future.result()

        segments = _align(transcribed, turns)
    # _align() mints "Speaker 1"/"Speaker 2"/... fresh on every call, scoped
    # only to this one chunk's pyannote turns -- chunk 5's "Speaker 1" and
    # chunk 39's "Speaker 1" are almost certainly different people who
    # happened to speak first in their own chunk. Tagging with this chunk's
    # (globally unique) start offset keeps that placeholder unique across
    # the whole meeting, so resolve_speaker_names() -- which later groups
    # segments by their exact placeholder string across ALL chunks -- never
    # conflates two different fallback chunks' unrelated "Speaker 1"s into
    # one group and one (wrong) resolved name.
    chunk_tag = f" (chunk@{chunk_start_offset:.1f})"
    return [
        replace(
            s,
            start=s.start + chunk_start_offset,
            end=s.end + chunk_start_offset,
            speaker=s.speaker + chunk_tag if s.speaker != "Unknown" else s.speaker,
        )
        for s in segments
    ]
