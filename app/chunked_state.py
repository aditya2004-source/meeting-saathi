"""In-memory per-run bookkeeping for the chunked/streaming pipeline: which
chunk sequences are still being processed, the speaker segments assembled
so far (with global timestamps), and each chunk's own measured duration
(used to compute the next chunk's global time offset).

Accepted v1 gap: this is in-memory only. A server restart mid-meeting loses
in-flight chunk accumulation for any run that hasn't reached "saved" yet --
the same category of gap as the already-documented "server must be running
while the extension is recording" constraint, not a new one. A future
option is persisting `processed_segments` incrementally to a .jsonl sidecar
under working/<run_id>/ so a restart could recover.
"""
import threading
from dataclasses import dataclass, field

from app.pipeline.diarize import SpeakerSegment


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


def get_or_create(run_id: str) -> RunChunkState:
    with _registry_lock:
        if run_id not in _run_states:
            _run_states[run_id] = RunChunkState()
        return _run_states[run_id]


def drop(run_id: str) -> None:
    with _registry_lock:
        _run_states.pop(run_id, None)
