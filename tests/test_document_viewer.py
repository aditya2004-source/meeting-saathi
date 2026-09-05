"""Covers Phase 5's in-browser document viewer (GET
/dashboard/meetings/{run_id}/documents/{doc_key}): renders a document's .md
source client-side instead of only linking to the PDF, using the same
extract_mermaid_blocks() transform the PDF path already uses so the
Business Process Flow's fenced diagram renders via the vendored
mermaid.min.js. Inherits Phase 1's ownership enforcement -- a wrong
customer must not be able to view another customer's document.
"""
from fastapi.testclient import TestClient

from app import auth, db
from app.config import settings
from app.main import app

client = TestClient(app, base_url="https://testserver")


def _fresh_db(tmp_path, monkeypatch):
    db_path = tmp_path / "runs.sqlite3"
    monkeypatch.setattr(settings, "db_path", db_path)
    db.init_db()


def _verified_customer(email: str) -> dict:
    db.create_customer(name="Priya Shah", email=email)
    for policy in auth.REQUIRED_CONSENT_POLICIES:
        db.record_consent(email=email, policy_type=policy, policy_version="test")
    customer = db.get_customer_by_email(email)
    return db.update_customer(customer["id"], email_verified=1)


def _bearer_header(customer_id: str) -> dict:
    token = auth.issue_device_token(customer_id, label="test")
    return {"Authorization": f"Bearer {token}"}


def _meeting_with_document(tmp_path, customer_id, markdown_text: str) -> dict:
    folder = tmp_path / "Weekly Sync - 2026-09-05 1200"
    folder.mkdir()
    (folder / "MOM.md").write_text(markdown_text)
    (folder / "MOM.pdf").write_bytes(b"%PDF-fake")
    run = db.create_run(title="Weekly Sync", audio_path="", customer_id=customer_id)
    return db.update_run(run["id"], folder_path=str(folder))


def test_renders_plain_markdown_as_html(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)
    owner = _verified_customer("owner@example.com")
    run = _meeting_with_document(tmp_path, owner["id"], "# Minutes of Meeting\n\nDecisions were made.")

    response = client.get(
        f"/dashboard/meetings/{run['id']}/documents/mom", headers=_bearer_header(owner["id"])
    )

    assert response.status_code == 200
    assert "<h1>Minutes of Meeting</h1>" in response.text
    assert "Decisions were made." in response.text
    assert "AI-generated content may contain errors" in response.text


def test_renders_mermaid_fence_as_a_pre_mermaid_node_and_loads_the_runtime(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)
    owner = _verified_customer("owner@example.com")
    markdown_with_diagram = "# Business Process Flow\n\n```mermaid\nflowchart TD\nA-->B\n```\n"
    run = db.create_run(title="Weekly Sync", audio_path="", customer_id=owner["id"])
    folder = tmp_path / "Weekly Sync - 2026-09-05 1200"
    folder.mkdir()
    (folder / "Business_Process_Flow.md").write_text(markdown_with_diagram)
    (folder / "Business_Process_Flow.pdf").write_bytes(b"%PDF-fake")
    db.update_run(run["id"], folder_path=str(folder))

    response = client.get(
        f"/dashboard/meetings/{run['id']}/documents/business_process_flow",
        headers=_bearer_header(owner["id"]),
    )

    assert response.status_code == 200
    assert '<pre class="mermaid">' in response.text
    assert "flowchart TD" in response.text
    assert "/static/vendor/mermaid.min.js" in response.text
    assert "```mermaid" not in response.text  # the raw fence syntax shouldn't leak through


def test_document_without_mermaid_does_not_load_the_runtime(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)
    owner = _verified_customer("owner@example.com")
    run = _meeting_with_document(tmp_path, owner["id"], "# MOM\n\nNo diagram here.")

    response = client.get(
        f"/dashboard/meetings/{run['id']}/documents/mom", headers=_bearer_header(owner["id"])
    )

    assert "mermaid.min.js" not in response.text


def test_a_different_customer_cannot_view_the_document(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)
    owner = _verified_customer("owner@example.com")
    intruder = _verified_customer("intruder@example.com")
    run = _meeting_with_document(tmp_path, owner["id"], "# MOM\n\nSecret decisions.")

    response = client.get(
        f"/dashboard/meetings/{run['id']}/documents/mom", headers=_bearer_header(intruder["id"])
    )

    assert response.status_code == 404
    assert "Secret decisions" not in response.text


def test_unknown_document_key_404s(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)
    owner = _verified_customer("owner@example.com")
    run = _meeting_with_document(tmp_path, owner["id"], "# MOM")

    response = client.get(
        f"/dashboard/meetings/{run['id']}/documents/does_not_exist", headers=_bearer_header(owner["id"])
    )

    assert response.status_code == 404


def test_document_not_yet_generated_404s(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)
    owner = _verified_customer("owner@example.com")
    folder = tmp_path / "Weekly Sync - 2026-09-05 1200"
    folder.mkdir()  # MOM.md never written
    run = db.create_run(title="Weekly Sync", audio_path="", customer_id=owner["id"])
    db.update_run(run["id"], folder_path=str(folder))

    response = client.get(
        f"/dashboard/meetings/{run['id']}/documents/mom", headers=_bearer_header(owner["id"])
    )

    assert response.status_code == 404
