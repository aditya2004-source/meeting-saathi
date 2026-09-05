"""Covers app.email_sender.send_email() -- no email-sending capability
existed anywhere in this repo before Phase 1. The dev-mode (no API key)
fallback must never raise or silently claim success by actually contacting
anything; the real path must call Resend's API with the right payload and
report failure (not raise) on an HTTP error.
"""
import logging

import httpx
import pytest

from app import email_sender
from app.config import settings


def test_without_api_key_logs_instead_of_sending(monkeypatch, caplog):
    monkeypatch.setattr(settings, "resend_api_key", "")
    calls = []
    monkeypatch.setattr(httpx, "post", lambda *a, **k: calls.append((a, k)))

    # app.main sets logger.propagate = False on "meeting_saathi" (its own
    # dedicated handler, not the root logger's) -- once app.main has been
    # imported anywhere in this test run, caplog's root-attached handler
    # never sees records from this logger unless attached directly to it
    # (see tests/test_config_validation.py for the same fix).
    target_logger = logging.getLogger("meeting_saathi")
    target_logger.addHandler(caplog.handler)
    try:
        with caplog.at_level(logging.WARNING, logger="meeting_saathi"):
            result = email_sender.send_email("user@example.com", "Subject", "Body")
    finally:
        target_logger.removeHandler(caplog.handler)

    assert result is True
    assert calls == []  # never actually called out to Resend
    assert any("user@example.com" in record.message for record in caplog.records)


def test_with_api_key_posts_to_resend(monkeypatch):
    monkeypatch.setattr(settings, "resend_api_key", "test-key")
    monkeypatch.setattr(settings, "email_from_address", "Meeting Saathi <hello@example.com>")
    captured = {}

    def fake_post(url, headers=None, json=None, timeout=None):
        captured["url"] = url
        captured["headers"] = headers
        captured["json"] = json
        return httpx.Response(200, request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx, "post", fake_post)

    result = email_sender.send_email("user@example.com", "Your code", "123456")

    assert result is True
    assert captured["url"] == "https://api.resend.com/emails"
    assert captured["headers"]["Authorization"] == "Bearer test-key"
    assert captured["json"]["to"] == ["user@example.com"]
    assert captured["json"]["subject"] == "Your code"
    assert captured["json"]["from"] == "Meeting Saathi <hello@example.com>"


def test_http_error_returns_false_instead_of_raising(monkeypatch):
    monkeypatch.setattr(settings, "resend_api_key", "test-key")

    def fake_post(url, headers=None, json=None, timeout=None):
        return httpx.Response(401, request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx, "post", fake_post)

    result = email_sender.send_email("user@example.com", "Subject", "Body")

    assert result is False
