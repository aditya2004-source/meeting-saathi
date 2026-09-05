"""Covers app.config.validate_settings() -- the startup guard added so a
from-scratch checkout without a .env fails loudly instead of silently signing
session cookies with an empty (forgeable) secret. See app/config.py.

Builds isolated Settings instances with _env_file=None so these don't read
the real project .env (which always has a real SESSION_SECRET_KEY set).
"""
import logging

import pytest

from app.config import Settings, validate_settings


def _settings(**overrides):
    defaults = dict(
        session_secret_key="a-real-secret",
        admin_url_slug="slug",
        admin_username="admin",
        admin_password="password",
    )
    defaults.update(overrides)
    return Settings(_env_file=None, **defaults)


def test_blank_session_secret_key_raises():
    with pytest.raises(RuntimeError, match="SESSION_SECRET_KEY"):
        validate_settings(_settings(session_secret_key=""))


def test_real_session_secret_key_does_not_raise():
    validate_settings(_settings())  # no exception


def test_blank_admin_credentials_warn_but_do_not_raise(caplog):
    # app.main sets logger.propagate = False on "meeting_saathi" (its own
    # dedicated handler, not the root logger's), so once app.main has been
    # imported anywhere in this test run, caplog's root-attached handler
    # never sees records from this logger unless attached directly to it.
    target_logger = logging.getLogger("meeting_saathi")
    target_logger.addHandler(caplog.handler)
    try:
        with caplog.at_level(logging.WARNING, logger="meeting_saathi"):
            validate_settings(_settings(admin_username=""))
    finally:
        target_logger.removeHandler(caplog.handler)
    assert any("ADMIN_URL_SLUG" in record.message for record in caplog.records)


def test_full_admin_credentials_do_not_warn(caplog):
    target_logger = logging.getLogger("meeting_saathi")
    target_logger.addHandler(caplog.handler)
    try:
        with caplog.at_level(logging.WARNING, logger="meeting_saathi"):
            validate_settings(_settings())
    finally:
        target_logger.removeHandler(caplog.handler)
    assert not caplog.records
