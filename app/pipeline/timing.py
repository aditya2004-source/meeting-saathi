import json
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Union

TimingValue = Union[float, list[float], dict[str, Any]]


class TimingRecorder:
    """Accumulates {stage: seconds} for one run, flushed to a JSON sidecar
    (working/<run_id>/timing.json) after every recorded stage rather than
    only at the end, so a still-in-progress run's timing is visible on the
    status page while it's happening -- this matters for the chunked
    pipeline, where "processing during the call" should be observable live.

    One-shot stages (e.g. "extract_facts") store a single float. Repeated
    per-chunk stages (e.g. "chunk_transcribe") accumulate as a list, one
    entry per call, so both count and total cost are visible.

    Thread-safe: multiple stages (e.g. the 3 parallel Gemini generate calls,
    or multiple in-flight chunks) can call record() concurrently.
    """

    def __init__(self, work_dir: Path):
        self._path = work_dir / "timing.json"
        self._lock = threading.Lock()
        self._data: dict[str, TimingValue] = {}
        if self._path.exists():
            try:
                self._data = json.loads(self._path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                self._data = {}

    def record(self, stage: str, value: TimingValue, repeated: bool = False) -> None:
        with self._lock:
            if repeated:
                bucket = self._data.setdefault(stage, [])
                if not isinstance(bucket, list):
                    bucket = [bucket]
                bucket.append(value)
                self._data[stage] = bucket
            else:
                self._data[stage] = value
            self._flush_locked()

    def flush(self) -> None:
        with self._lock:
            self._flush_locked()

    def _flush_locked(self) -> None:
        tmp_path = self._path.with_suffix(".json.tmp")
        tmp_path.write_text(json.dumps(self._data, indent=2), encoding="utf-8")
        tmp_path.replace(self._path)

    def as_dict(self) -> dict[str, TimingValue]:
        return dict(self._data)


@contextmanager
def timed(recorder: TimingRecorder, stage: str, repeated: bool = False) -> Iterator[None]:
    start = time.monotonic()
    try:
        yield
    finally:
        recorder.record(stage, time.monotonic() - start, repeated=repeated)


def load_timing(work_dir: Path) -> dict[str, TimingValue] | None:
    path = work_dir / "timing.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


# A run's own timing.json is deleted along with the rest of its working
# directory the moment it reaches "saved" (see orchestrator.py/
# orchestrator_streaming.py), so there's no per-run history to compute an
# ETA from by the time a *future* run needs one. This tiny separate file
# persists just {stage: {count, mean}} across every completed run instead
# of raw per-run durations, so app/progress.py can estimate "how long do the
# stages this run hasn't reached yet usually take" -- and it stays a few KB
# forever regardless of how many meetings have been processed.
_history_lock = threading.Lock()


def load_stage_history(path: Path) -> dict[str, dict[str, float]]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def record_stage_durations(path: Path, durations: dict[str, float]) -> None:
    """Folds one completed run's {stage: seconds} into the persisted
    history using Welford's incremental mean (mean_new = mean_old +
    (x - mean_old) / n) -- no raw sample list is kept, so file size never
    grows with the number of runs processed. Safe to call from multiple
    threads/runs finishing around the same time (module-level lock, same
    read-modify-write-under-lock shape as TimingRecorder._flush_locked).
    """
    with _history_lock:
        history = load_stage_history(path)
        for stage, value in durations.items():
            if not isinstance(value, (int, float)):
                continue
            entry = history.get(stage, {"count": 0, "mean": 0.0})
            count = entry["count"] + 1
            mean = entry["mean"] + (value - entry["mean"]) / count
            history[stage] = {"count": count, "mean": mean}
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = path.with_suffix(".json.tmp")
        tmp_path.write_text(json.dumps(history, indent=2), encoding="utf-8")
        tmp_path.replace(path)
