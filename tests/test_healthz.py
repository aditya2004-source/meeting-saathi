"""Covers GET /healthz -- Phase 9's endpoint for the reverse proxy and any
uptime monitor to check. No auth (infra checking this has no login), and it
actually verifies the DB is reachable, not just that the process started.
"""
from fastapi.testclient import TestClient

from app import db
from app.config import settings
from app.main import app

client = TestClient(app)


def test_healthz_ok_when_db_reachable(tmp_path, monkeypatch):
    db_path = tmp_path / "runs.sqlite3"
    monkeypatch.setattr(settings, "db_path", db_path)
    db.init_db()

    response = client.get("/healthz")

    assert response.status_code == 200
    assert response.json() == {"ok": True, "db": True}


def test_healthz_reports_unhealthy_when_db_unreachable(monkeypatch):
    def _broken(*args, **kwargs):
        raise RuntimeError("db is down")

    monkeypatch.setattr(db, "get_run", _broken)

    response = client.get("/healthz")

    assert response.status_code == 503
    assert response.json() == {"ok": False, "db": False}


def test_healthz_has_no_noindex_confusion_but_is_not_in_sitemap():
    sitemap = client.get("/sitemap.xml").text
    assert "/healthz" not in sitemap
    robots = client.get("/robots.txt").text
    assert "Disallow: /healthz" in robots
