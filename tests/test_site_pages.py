"""Covers Phase 4's public website: every page renders with a distinct
title, legal pages render real distinct content (not one shared screen
like the prototype), robots.txt/sitemap.xml never leak a private route or
the admin slug, every private route carries a noindex header, and feedback
submission persists a real row.
"""
import re

from fastapi.testclient import TestClient

from app import db
from app.config import settings
from app.main import app

client = TestClient(app)

_PUBLIC_PAGES = [
    "/",
    "/pricing",
    "/how-it-works",
    "/legal/terms-of-service",
    "/legal/privacy-policy",
    "/legal/refund-policy",
    "/legal/ai-disclaimer",
    "/signup",
    "/install",
    "/feedback",
]


def _title(html: str) -> str:
    match = re.search(r"<title>(.*?)</title>", html, re.DOTALL)
    return match.group(1).strip() if match else ""


def test_every_public_page_renders_with_a_title():
    for path in _PUBLIC_PAGES:
        response = client.get(path)
        assert response.status_code == 200, path
        assert _title(response.text), f"{path} has no <title>"


def test_public_pages_have_distinct_titles():
    titles = [_title(client.get(path).text) for path in _PUBLIC_PAGES]
    assert len(titles) == len(set(titles))


def test_legal_pages_render_distinct_content_not_one_shared_screen():
    bodies = {
        slug: client.get(f"/legal/{slug}").text
        for slug in ["terms-of-service", "privacy-policy", "refund-policy", "ai-disclaimer"]
    }
    # Pairwise distinct -- the prototype's bug was all four routes opening
    # the identical screen.
    values = list(bodies.values())
    for i in range(len(values)):
        for j in range(i + 1, len(values)):
            assert values[i] != values[j]
    assert "Refund" in bodies["refund-policy"] or "refund" in bodies["refund-policy"]


def test_unknown_legal_slug_404s():
    assert client.get("/legal/does-not-exist").status_code == 404


def test_logo_links_home_on_public_pages():
    for path in ["/", "/pricing"]:
        html = client.get(path).text
        assert '<a class="wordmark" href="/">' in html and "Meeting Saathi" in html


def test_logo_links_home_on_the_dashboard(monkeypatch):
    monkeypatch.setattr(db, "list_runs", lambda user_name=None, client_name=None: [])
    monkeypatch.setattr(db, "distinct_client_names", lambda user_name=None: [])

    html = client.get("/dashboard", params={"name": "Priya Shah"}).text

    assert '<a href="/"' in html and "Meeting Saathi</a>" in html


def test_robots_txt_disallows_private_paths_and_never_leaks_the_admin_slug():
    text = client.get("/robots.txt").text
    assert "Disallow: /dashboard" in text
    assert "Disallow: /account/" in text
    assert settings.admin_url_slug not in text


def test_sitemap_lists_only_public_pages():
    text = client.get("/sitemap.xml").text
    for private_fragment in ("/dashboard", "/account", settings.admin_url_slug):
        assert private_fragment not in text
    for path in ["/", "/pricing", "/legal/terms-of-service", "/signup"]:
        assert f"<loc>http://testserver{path}</loc>" in text


def test_private_routes_carry_noindex_header():
    for path in ["/dashboard"]:
        assert client.get(path).headers.get("x-robots-tag") == "noindex, nofollow"


def test_public_routes_do_not_carry_noindex_header():
    for path in _PUBLIC_PAGES:
        assert "x-robots-tag" not in {k.lower() for k in client.get(path).headers.keys()}


def test_feedback_submission_persists_a_real_row(tmp_path, monkeypatch):
    db_path = tmp_path / "runs.sqlite3"
    monkeypatch.setattr(settings, "db_path", db_path)
    db.init_db()

    response = client.post(
        "/feedback",
        data={"message": "The Business Process Flow diagram was very helpful.", "category": "feature", "email": "priya@example.com"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/feedback?submitted=1"
    rows = db.list_feedback()
    assert len(rows) == 1
    assert rows[0]["message"] == "The Business Process Flow diagram was very helpful."
    assert rows[0]["category"] == "feature"
    assert rows[0]["status"] == "new"


def test_feedback_page_shows_thank_you_after_submission():
    response = client.get("/feedback", params={"submitted": "1"})
    assert "Thanks" in response.text
