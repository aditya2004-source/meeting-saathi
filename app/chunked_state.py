"""Per-run bookkeeping for the chunked/streaming pipeline: which chunk
sequences are still being processed, the speaker segments assembled so far
(with global timestamps), and each chunk's own measured duration (used to
compute the next chunk's global time offset).

Mirrored to disk under working/<run_id>/ as each chunk finishes (see
orchestrator_streaming.py's _process_chunk()) -- processed_segments.jsonl
(one segment per line, append-only) and chunk_durations.json -- so that if
this process restarts mid-meeting (crash, deploy, `systemctl restart`),
get_or_create() can recover a run's accumulated progress instead of
silently starting over from empty state (confirmed in production: a
restart during a real ~50-minute meeting wiped 60 already-transcribed
chunks' worth of segments, producing an empty MOM despite every chunk
having uploaded and processed successfully).
"""
import json
import threading
from dataclasses import dataclass, field
from pathlib import Path

from app.pipeline.diarize import SpeakerSegment

SEGMENTS_SIDECAR_NAME = "processed_segments.jsonl"
DURATIONS_SIDECAR_NAME = "chunk_durations.json"


@dataclass
class RunChunkState:
    lock: threading.Lock = field(default_factory=threading.Lock)
    processed_segments: list[SpeakerSegment] = field(default_factory=list)
    pending_sequences: set[int] = field(default_factory=set)
    # sequence -> measured duration (seconds); used to compute each chunk's
    # global start offset as the sum of all lower sequences' durations,
    # since MediaRecorder restart-cycling doesn't produce exactly uniform
    # chunk lengths.
    chunk_durations: dict[int, float] = field(default_factory=dict)


_registry_lock = threading.Lock()
_run_states: dict[str, RunChunkState] = {}


def get_or_create(run_id: str, work_dir: Path | None = None) -> RunChunkState:
    """`work_dir` is only consulted the first time this run_id is seen by
    this process -- an already-registered state is assumed current (this
    same process already has whatever progress exists). Pass it from every
    call site that has it handy; recovery only actually needs it once.
    """
    with _registry_lock:
        if run_id not in _run_states:
            state = RunChunkState()
            if work_dir is not None:
                _recover_from_disk(state, work_dir)
            _run_states[run_id] = state
        return _run_states[run_id]


def _recover_from_disk(state: RunChunkState, work_dir: Path) -> None:
    """Best-effort: a missing or corrupt sidecar just means starting from
    empty state, same behavior as before this recovery path existed --
    never raises.
    """
    segments_path = work_dir / SEGMENTS_SIDECAR_NAME
    if segments_path.exists():
        for line in segments_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                data = json.loads(line)
                state.processed_segments.append(
                    SpeakerSegment(
                        start=data["start"], end=data["end"], speaker=data["speaker"], text=data["text"]
                    )
                )
            except (json.JSONDecodeError, KeyError, TypeError, ValueError):
                continue

    durations_path = work_dir / DURATIONS_SIDECAR_NAME
    if durations_path.exists():
        try:
            raw = json.loads(durations_path.read_text(encoding="utf-8"))
            state.chunk_durations = {int(k): float(v) for k, v in raw.items()}
        except (json.JSONDecodeError, ValueError, TypeError):
            pass


def append_segments_to_disk(work_dir: Path, segments: list[SpeakerSegment]) -> None:
    """Called right after a chunk's segments are computed, before they're
    considered durable -- one JSON object per line, append-only, so a crash
    mid-write only ever loses the segment(s) not yet flushed, never
    corrupts previously-written lines.
    """
    if not segments:
        return
    with open(work_dir / SEGMENTS_SIDECAR_NAME, "a", encoding="utf-8") as f:
        for seg in segments:
            f.write(json.dumps({"start": seg.start, "end": seg.end, "speaker": seg.speaker, "text": seg.text}))
            f.write("\n")


def drop(run_id: str) -> None:
    with _registry_lock:
        _run_states.pop(run_id, None)
