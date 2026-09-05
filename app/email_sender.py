"""Transactional email (OTP codes, later: "your documents are ready").

No email-sending capability existed anywhere in this repo before Phase 1.
Deliberately reuses `httpx` (already a dependency for other reasons) against
Resend's HTTP API rather than adding an SMTP stack or a new SDK dependency --
one more HTTP call, not a new kind of integration.

Resend's free tier (100 emails/day) is plenty for OTP volume at early-stage
scale; swapping providers later only means changing send_email()'s body, not
any caller.

Without RESEND_API_KEY set (e.g. a fresh checkout, or running tests), this
logs the email instead of sending it -- loud and obvious in the log, never
silently pretending to have sent something it didn't. Real deployments must
set RESEND_API_KEY before OTP emails can actually reach anyone.
"""
import logging

import httpx

from app.config import settings

logger = logging.getLogger("meeting_saathi")

_RESEND_API_URL = "https://api.resend.com/emails"


def send_email(to: str, subject: str, text_body: str) -> bool:
    """Returns True if the email was actually sent (or logged, in dev-mode
    fallback) without raising -- callers (e.g. the send-otp route) should
    still treat a False return as "don't tell the user it was sent."
    """
    if not settings.resend_api_key:
        logger.warning(
            "RESEND_API_KEY not set -- logging email instead of sending it. "
            "to=%s subject=%r body=%r",
            to,
            subject,
            text_body,
        )
        return True

    try:
        response = httpx.post(
            _RESEND_API_URL,
            headers={"Authorization": f"Bearer {settings.resend_api_key}"},
            json={
                "from": settings.email_from_address,
                "to": [to],
                "subject": subject,
                "text": text_body,
            },
            timeout=10.0,
        )
        response.raise_for_status()
        return True
    except httpx.HTTPError:
        logger.exception("Failed to send email to %s", to)
        return False
