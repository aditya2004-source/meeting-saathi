"""Covers app.config.settings.disable_api_docs -- a production-deployment
hardening toggle for FastAPI's public /docs, /redoc, /openapi.json routes.

Testing the real app.main `app` singleton for BOTH settings values isn't
possible in-process (FastAPI reads docs_url/redoc_url/openapi_url once at
construction time, which already happened at import time under whatever
value was in effect then) -- so this covers the default (disabled=False,
what every other test in this suite already runs against, confirming this
change didn't regress it) against the real app, and the disabled=True
behavior against a throwaway FastAPI instance built with the exact same
conditional app.main uses, proving the mechanism itself actually removes
the routes rather than just returning an error response for them.
"""
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.main import app as real_app


def test_docs_are_reachable_by_default():
    client = TestClient(real_app)

    assert client.get("/docs").status_code == 200
    assert client.get("/redoc").status_code == 200
    assert client.get("/openapi.json").status_code == 200


def _build_app(disable_api_docs: bool) -> FastAPI:
    # Mirrors app.main's exact construction line.
    return FastAPI(
        docs_url=None if disable_api_docs else "/docs",
        redoc_url=None if disable_api_docs else "/redoc",
        openapi_url=None if disable_api_docs else "/openapi.json",
    )


def test_docs_routes_are_actually_removed_when_disabled():
    disabled_app = _build_app(disable_api_docs=True)
    client = TestClient(disabled_app)

    assert client.get("/docs").status_code == 404
    assert client.get("/redoc").status_code == 404
    assert client.get("/openapi.json").status_code == 404


def test_docs_routes_are_present_when_not_disabled():
    enabled_app = _build_app(disable_api_docs=False)
    client = TestClient(enabled_app)

    assert client.get("/docs").status_code == 200
    assert client.get("/redoc").status_code == 200
    assert client.get("/openapi.json").status_code == 200
