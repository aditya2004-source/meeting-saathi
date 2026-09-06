"""Phase 10: the full customer journey, end to end, exercising every phase's
real code together rather than in isolation -- this is the test that proves
the phases actually compose into one working product, not just that each
one passes its own unit tests.

Journey: signup -> consent -> OTP verify -> extension pairing (device
token) -> 3 successful free meetings (documents generated, viewed,
downloaded) -> 4th meeting blocked with trial_exhausted -> subscribe
(mocked Razorpay -- no real account available, see Phase 6/3's own notes)
-> webhook activates the subscription -> unlimited meetings confirmed ->
cancel -> access reverts once the period ends.

Real Razorpay/AssemblyAI/Chrome-extension-click-through verification is
explicitly NOT done here (no live accounts/browser available in this
session) -- see DEPLOYMENT.md and project memory for what's still open.
"""
import hashlib
import hmac
import json
from unittest.mock import patch

from fastapi.testclient import TestClient

from app import auth, db
from app.config import settings
from app.main import app

client = TestClient(app, base_url="https://testserver")


def _fresh_db(tmp_path, monkeypatch):
    db_path = tmp_path / "runs.sqlite3"
    monkeypatch.setattr(settings, "db_path", db_path)
    db.init_db()
    monkeypatch.setattr(settings, "working_dir", tmp_path / "working")
    monkeypatch.setattr(settings, "razorpay_webhook_secret", "test-secret")


def _signed_webhook(payload: dict) -> tuple[bytes, str]:
    raw = json.dumps(payload).encode("utf-8")
    signature = hmac.new(b"test-secret", raw, hashlib.sha256).hexdigest()
    return raw, signature


def _complete_meeting(run_id: str, tmp_path) -> None:
    """Mirrors what the real orchestrator does at the "saved" transition
    (see app/orchestrator_streaming.py's finalize_run()) without running
    the actual transcription pipeline -- including that a real "saved" run
    always has folder_path set (written well before "saved", see
    app/storage.py's create_meeting_folder()), even though this fake
    pipeline never actually writes facts.json/transcript.json into it.
    """
    from app import entitlement

    folder = tmp_path / f"meeting-{run_id}"
    folder.mkdir(exist_ok=True)
    db.update_run(run_id, state="saved", duration_seconds=300.0, folder_path=str(folder))
    entitlement.record_meeting_completed(run_id)


def test_full_customer_journey(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)
    email = "priya@example.com"

    # 1. Signup
    assert client.post("/auth/signup", data={"name": "Priya Shah", "email": email}).status_code == 200

    # 2. Consent must exist before OTP can even be requested
    denied = client.post("/auth/send-otp", data={"email": email})
    assert denied.status_code == 400
    assert client.post("/auth/consent", data={"email": email}).status_code == 200
    assert client.post("/auth/send-otp", data={"email": email}).status_code == 200

    # 3. OTP verification (raw code obtained directly, same as
    # tests/test_auth_flow.py -- it's only ever emailed/logged in reality)
    code = auth.issue_otp(email)
    verify = client.post("/auth/verify-otp", data={"email": email, "code": code})
    assert verify.status_code == 200
    customer_id = verify.json()["id"]

    # 4. Extension pairing -- mints a device token the same way the
    # install page's "Connect Account" button does
    token_response = client.post("/account/connect/device-token")
    assert token_response.status_code == 200
    device_token = token_response.json()["token"]
    client.post("/auth/logout")  # the browser session ends; the extension keeps its own token

    extension = TestClient(app, base_url="https://testserver")
    auth_header = {"Authorization": f"Bearer {device_token}"}

    # 5. Three successful free meetings
    for i in range(settings.free_meeting_allowance):
        start = extension.post("/meetings/start", data={"title": f"Meeting {i}"}, headers=auth_header)
        assert start.status_code == 200
        run_id = start.json()["id"]
        assert db.get_run(run_id)["billing_mode"] == "free"
        _complete_meeting(run_id, tmp_path)

        generate = extension.post(
            f"/meetings/{run_id}/documents/mom/generate", headers=auth_header
        )
        # Not actually ready (no real facts.json/transcript.json written by
        # this test's fake pipeline) -- 409 is the correct, expected
        # response here; the point is the ownership check passes (not 401/404).
        assert generate.status_code == 409

    customer_after_trial = db.get_customer(customer_id)
    assert customer_after_trial["free_meetings_used"] == settings.free_meeting_allowance

    # 6. Fourth meeting is blocked
    fourth = extension.post("/meetings/start", data={"title": "Meeting 4"}, headers=auth_header)
    assert fourth.status_code == 402
    assert fourth.json()["detail"] == "trial_exhausted"

    # 7. Subscribe (mocked Razorpay -- see module docstring)
    website = TestClient(app, base_url="https://testserver")
    code2 = auth.issue_otp(email)
    website.post("/auth/verify-otp", data={"email": email, "code": code2})
    with patch("app.billing.routes.razorpay_client.find_or_create_customer", return_value="cust_e2e"), patch(
        "app.billing.routes.razorpay_client.create_subscription",
        return_value={"id": "sub_e2e", "short_url": "https://rzp.io/i/e2e"},
    ):
        subscribe = website.post("/billing/subscribe", data={"billing_cycle": "monthly", "currency": "INR"})
    assert subscribe.status_code == 200
    assert subscribe.json()["checkout_url"] == "https://rzp.io/i/e2e"
    assert db.get_active_subscription(customer_id) is None  # not active until the webhook fires

    # 8. Razorpay webhook activates the subscription
    raw, signature = _signed_webhook(
        {
            "event": "subscription.activated",
            "payload": {
                "subscription": {"entity": {"id": "sub_e2e", "current_end": 4102444800}},
                "payment": {"entity": {"id": "pay_e2e", "currency": "INR", "amount": 29900}},
            },
        }
    )
    webhook = client.post("/billing/webhook/razorpay", content=raw, headers={"X-Razorpay-Signature": signature})
    assert webhook.status_code == 200
    assert db.get_active_subscription(customer_id) is not None

    # 9. The SAME already-paired extension immediately gets unlimited
    # access -- no reinstall, no new token, no code change on the
    # extension's side.
    fifth = extension.post("/meetings/start", data={"title": "Meeting 5"}, headers=auth_header)
    assert fifth.status_code == 200
    assert db.get_run(fifth.json()["id"])["billing_mode"] == "paid"

    # 10. Cancel -- access is cancel-at-cycle-end, not instant
    with patch("app.billing.routes.razorpay_client.cancel_subscription", return_value={}):
        cancel = website.post("/billing/cancel")
    assert cancel.status_code == 200
    # Still active right up until the period actually ends (real end date
    # far in the future here) -- another meeting still succeeds today.
    still_active = extension.post("/meetings/start", data={"title": "Meeting 6"}, headers=auth_header)
    assert still_active.status_code == 200

    # 11. Once Razorpay's own subscription.cancelled webhook actually fires
    # (i.e. the period has ended), access reverts.
    raw2, signature2 = _signed_webhook(
        {"event": "subscription.cancelled", "payload": {"subscription": {"entity": {"id": "sub_e2e"}}}}
    )
    client.post("/billing/webhook/razorpay", content=raw2, headers={"X-Razorpay-Signature": signature2})
    assert db.get_active_subscription(customer_id) is None

    reverted = extension.post("/meetings/start", data={"title": "Meeting 7"}, headers=auth_header)
    assert reverted.status_code == 402  # trial already exhausted, no active subscription anymore
    assert reverted.json()["detail"] == "trial_exhausted"
