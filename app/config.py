from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # Gemini API (free tier -- used for document generation)
    gemini_api_key: str = ""
    gemini_model: str = "gemini-3.5-flash"

    # AssemblyAI (paid, pay-as-you-go) -- replaces local whisper+pyannote
    # for chunked/streaming diarization's pyannote-fallback branch only
    # (app.pipeline.diarize.diarize_chunk()). Confirmed in production: that
    # branch's local pyannote step took up to 630s for one ~50s chunk on
    # this 4-core box (CPU thread oversubscription); a single AssemblyAI
    # call does transcription + speaker diarization together, offloaded to
    # their infrastructure, at ~$0.17/hour of audio. The DOM-primary fast
    # path (the common case in a real multi-person meeting) is unaffected
    # and still free/local.
    assemblyai_api_key: str = ""

    # Local speech-to-text
    whisper_model_size: str = "small"
    whisper_compute_type: str = "int8"
    # CTranslate2 intra-op threads (parallelism within one transcribe() call)
    # and inter-op/pipeline workers (parallelism across concurrent transcribe()
    # calls, relevant once chunked/fallback diarization runs transcribe()
    # concurrently with pyannote). Was 4 (matching this box's core count),
    # but that's PER transcribe() call -- with orchestrator_streaming.py's
    # _CHUNK_EXECUTOR now allowing up to 2 chunks to process concurrently,
    # each pairing transcribe()+pyannote (2 more-way concurrency), 4 here
    # would let a single moment's CPU demand reach 2 chunks x 2 (transcribe
    # + pyannote) x 4 threads = 16 threads competing for 4 real cores.
    # Confirmed in production: this exact oversubscription drove pyannote's
    # embedding stage to 575s for one ~50s chunk. 2 keeps total in-flight
    # threads roughly matched to real core count instead.
    whisper_cpu_threads: int = 2
    # faster-whisper's BatchedInferencePipeline batch size -- ~2x CPU/int8
    # speedup per faster-whisper's own README benchmark.
    whisper_batch_size: int = 8

    # Diarization ("who said what") — local pyannote.audio, since audio comes
    # from the Chrome extension as a single mixed recording, not per-speaker
    # tracks from a vendor. Free HuggingFace token, not a paid API.
    huggingface_token: str = ""
    # torch.set_num_threads() for pyannote's segmentation/embedding models --
    # pyannote has no thread setting of its own, it just inherits plain
    # PyTorch config. Reduced from 4 alongside whisper_cpu_threads above, for
    # the same reason: with up to 2 chunks processing concurrently, 4 here
    # would badly oversubscribe this box's 4 real cores (confirmed in
    # production -- see whisper_cpu_threads' comment for the real numbers).
    pyannote_torch_threads: int = 2
    # Chunked/streaming diarization: minimum fraction of a chunk's duration
    # that must be covered by Meet active-speaker-tile events (speaker_events)
    # before we trust them as the primary (fast, no audio ML) diarization
    # source; below this, fall back to running pyannote on that chunk.
    chunk_pyannote_coverage_threshold: float = 0.5
    # speaker_from_dom_events() attributes a segment to the nearest
    # *preceding* DOM speaker-change event, carried forward indefinitely by
    # default. If the extension's MutationObserver stalls (tab backgrounded,
    # a Meet UI change it doesn't recognize) that stale event would otherwise
    # keep confidently mislabeling everyone as whoever spoke last before the
    # stall -- past this many seconds without a fresher event, attribution
    # reverts to "Unknown" instead.
    speaker_event_max_staleness_seconds: float = 300.0

    # Output storage
    base_storage_dir: Path = Path.home() / "Downloads" / "Sarathi Meetings"
    keep_raw_recording: bool = False

    # Local service
    port: int = 8420

    # Internal paths (not env-configurable)
    project_root: Path = Path(__file__).resolve().parent.parent
    db_path: Path = project_root / "data" / "runs.sqlite3"
    working_dir: Path = project_root / "working"


settings = Settings()
settings.base_storage_dir.mkdir(parents=True, exist_ok=True)
settings.working_dir.mkdir(parents=True, exist_ok=True)
settings.db_path.parent.mkdir(parents=True, exist_ok=True)
