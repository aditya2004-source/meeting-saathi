"""Covers app.docgen.engine's 429-rate-limit retry -- added after a real,
live finding (Phase 3's long-transcript verification spike, see
scripts/spike_long_transcript.py): one meeting's document generation makes
~9 Gemini calls in quick succession, several re-sending the whole
transcript, and on the free tier's per-minute input-token quota this
genuinely triggers a 429 RESOURCE_EXHAUSTED mid-run for one meeting alone.
There was no handling for this before -- it just crashed that document's
generation. Mocks time.sleep so these tests run instantly regardless of the
real/suggested retry delay.
"""
from types import SimpleNamespace
from unittest.mock import patch

import httpx
import pytest
from google.genai import errors

from app.docgen.engine import _generate_json


def _rate_limit_error(retry_delay: str | None = "2s") -> errors.ClientError:
    details = [{"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": retry_delay}] if retry_delay else []
    return errors.ClientError(
        429,
        {"error": {"code": 429, "status": "RESOURCE_EXHAUSTED", "message": "quota exceeded", "details": details}},
    )


def _response(text: str):
    return SimpleNamespace(text=text, candidates=[SimpleNamespace(finish_reason=None)])


def test_retries_after_a_429_and_eventually_succeeds():
    ok = _response('{"a": "value"}')

    with patch(
        "app.docgen.engine._client.models.generate_content", side_effect=[_rate_limit_error(), ok]
    ) as mock_call, patch("app.docgen.engine.time.sleep") as mock_sleep:
        result = _generate_json("system", {"type": "OBJECT"}, "content", max_output_tokens=4096)

    assert result == {"a": "value"}
    assert mock_call.call_count == 2
    mock_sleep.assert_called_once_with(2.0)


def test_gives_up_after_max_retries():
    with patch(
        "app.docgen.engine._client.models.generate_content",
        side_effect=[_rate_limit_error()] * 10,
    ) as mock_call, patch("app.docgen.engine.time.sleep"):
        with pytest.raises(errors.ClientError):
            _generate_json("system", {"type": "OBJECT"}, "content", max_output_tokens=4096)

    assert mock_call.call_count == 4  # 1 initial + 3 retries


def test_non_429_client_error_is_not_retried():
    not_found = errors.ClientError(404, {"error": {"code": 404, "status": "NOT_FOUND", "message": "nope"}})

    with patch(
        "app.docgen.engine._client.models.generate_content", side_effect=[not_found]
    ) as mock_call, patch("app.docgen.engine.time.sleep") as mock_sleep:
        with pytest.raises(errors.ClientError):
            _generate_json("system", {"type": "OBJECT"}, "content", max_output_tokens=4096)

    assert mock_call.call_count == 1
    mock_sleep.assert_not_called()


def test_falls_back_to_a_fixed_delay_when_retry_info_is_missing():
    ok = _response('{"a": "value"}')

    with patch(
        "app.docgen.engine._client.models.generate_content",
        side_effect=[_rate_limit_error(retry_delay=None), ok],
    ), patch("app.docgen.engine.time.sleep") as mock_sleep:
        _generate_json("system", {"type": "OBJECT"}, "content", max_output_tokens=4096)

    mock_sleep.assert_called_once_with(20.0)


# --- Production-audit fix: transient 5xx / network-error retry -------------
# Previously only a 429 was ever retried -- a transient Gemini-side 5xx or a
# network blip (connection reset, read timeout) crashed that document's
# generation outright with no retry at all.


def _server_error(code: int = 503) -> errors.ServerError:
    return errors.ServerError(code, {"error": {"code": code, "status": "UNAVAILABLE", "message": "server overloaded"}})


def test_retries_after_a_transient_5xx_and_eventually_succeeds():
    ok = _response('{"a": "value"}')

    with patch(
        "app.docgen.engine._client.models.generate_content", side_effect=[_server_error(), ok]
    ) as mock_call, patch("app.docgen.engine.time.sleep") as mock_sleep:
        result = _generate_json("system", {"type": "OBJECT"}, "content", max_output_tokens=4096)

    assert result == {"a": "value"}
    assert mock_call.call_count == 2
    mock_sleep.assert_called_once_with(2.0)


def test_retries_after_a_network_error_and_eventually_succeeds():
    ok = _response('{"a": "value"}')

    with patch(
        "app.docgen.engine._client.models.generate_content",
        side_effect=[httpx.ConnectError("connection reset"), ok],
    ) as mock_call, patch("app.docgen.engine.time.sleep") as mock_sleep:
        result = _generate_json("system", {"type": "OBJECT"}, "content", max_output_tokens=4096)

    assert result == {"a": "value"}
    assert mock_call.call_count == 2
    mock_sleep.assert_called_once_with(2.0)


def test_gives_up_after_max_transient_error_retries():
    with patch(
        "app.docgen.engine._client.models.generate_content",
        side_effect=[_server_error()] * 10,
    ) as mock_call, patch("app.docgen.engine.time.sleep") as mock_sleep:
        with pytest.raises(errors.ServerError):
            _generate_json("system", {"type": "OBJECT"}, "content", max_output_tokens=4096)

    assert mock_call.call_count == 3  # 1 initial + 2 retries
    assert mock_sleep.call_args_list == [((2.0,),), ((5.0,),)]


def test_429_and_5xx_retry_counters_are_independent():
    # Mixed failure modes across attempts -- confirms the two retry
    # counters are independent, not a shared attempt budget where one
    # error type could silently consume the other's retry allowance.
    # 3 retried 429s exhausts that counter's budget (3) without raising,
    # then a 5xx still gets its own retry rather than being refused.
    ok = _response('{"a": "value"}')

    with patch(
        "app.docgen.engine._client.models.generate_content",
        side_effect=[_rate_limit_error(), _rate_limit_error(), _rate_limit_error(), _server_error(), ok],
    ) as mock_call, patch("app.docgen.engine.time.sleep"):
        result = _generate_json("system", {"type": "OBJECT"}, "content", max_output_tokens=4096)

    assert result == {"a": "value"}
    assert mock_call.call_count == 5
