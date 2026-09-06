"""Covers app.pipeline.diarize._call_with_timeout_and_retry -- the
production-audit fix for AssemblyAI calls that previously had no timeout
and no retry at all. A transient failure or a hang used to silently drop
that chunk's entire transcript (the existing per-chunk try/except in
orchestrator_streaming.py swallows the exception by design), so a bounded
retry directly improves transcript completeness/accuracy, not just
robustness.
"""
import time

import pytest

from app.pipeline import diarize


def test_succeeds_on_first_attempt_without_retrying(monkeypatch):
    sleep_calls = []
    monkeypatch.setattr(time, "sleep", lambda s: sleep_calls.append(s))
    calls = []

    def run_once():
        calls.append(1)
        return "ok"

    result = diarize._call_with_timeout_and_retry(run_once, label="test")

    assert result == "ok"
    assert len(calls) == 1
    assert sleep_calls == []


def test_retries_a_transient_failure_and_eventually_succeeds(monkeypatch):
    sleep_calls = []
    monkeypatch.setattr(time, "sleep", lambda s: sleep_calls.append(s))
    attempts = {"count": 0}

    def run_once():
        attempts["count"] += 1
        if attempts["count"] < 3:
            raise ConnectionError("transient network blip")
        return "recovered"

    result = diarize._call_with_timeout_and_retry(run_once, label="test")

    assert result == "recovered"
    assert attempts["count"] == 3
    # Backed off before the 2nd and 3rd attempts, not before the 1st.
    assert sleep_calls == [diarize._ASSEMBLYAI_BACKOFF_SECONDS[0], diarize._ASSEMBLYAI_BACKOFF_SECONDS[1]]


def test_raises_the_last_error_once_all_attempts_are_exhausted(monkeypatch):
    monkeypatch.setattr(time, "sleep", lambda s: None)

    def always_fails():
        raise RuntimeError("AssemblyAI transcription failed: permanent error")

    with pytest.raises(RuntimeError, match="permanent error"):
        diarize._call_with_timeout_and_retry(always_fails, label="test")


def test_a_hung_call_past_the_timeout_is_retried_as_a_timeout_error(monkeypatch):
    import threading

    monkeypatch.setattr(diarize, "_ASSEMBLYAI_TIMEOUT_SECONDS", 0.05)
    monkeypatch.setattr(diarize, "_ASSEMBLYAI_MAX_ATTEMPTS", 2)
    monkeypatch.setattr(time, "sleep", lambda s: None)

    def hangs_forever():
        # threading.Event().wait(), not time.sleep() -- this test patches
        # time.sleep globally (it's the same module object diarize.py's
        # backoff calls use), which would make this "hang" a no-op too.
        threading.Event().wait(5)
        return "too late"

    with pytest.raises(TimeoutError, match="timed out"):
        diarize._call_with_timeout_and_retry(hangs_forever, label="test")


def test_does_not_sleep_after_the_final_failed_attempt(monkeypatch):
    sleep_calls = []
    monkeypatch.setattr(time, "sleep", lambda s: sleep_calls.append(s))

    def always_fails():
        raise RuntimeError("nope")

    with pytest.raises(RuntimeError):
        diarize._call_with_timeout_and_retry(always_fails, label="test")

    # 3 max attempts -> 2 backoff sleeps, never a 3rd (no point delaying
    # after the last attempt has already failed).
    assert len(sleep_calls) == diarize._ASSEMBLYAI_MAX_ATTEMPTS - 1
