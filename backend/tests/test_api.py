"""HTTP smoke tests for the FastAPI app (no external catalog calls)."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient


# Frontend-Bundle liegt unter backend/frontend/build/index.html nach
# `npm run build`. Backend-Tests laufen unabhängig vom Frontend-Build —
# fehlt das Bundle (z.B. in CI's Backend-Job), wird der "/"-Test
# geskipped statt zu failen. Lokal mit gebautem Frontend läuft er.
_FRONTEND_BUILD = Path(__file__).resolve().parent.parent / "frontend" / "build" / "index.html"
_HAS_FRONTEND_BUNDLE = _FRONTEND_BUILD.is_file()


@pytest.fixture(scope="module")
def client() -> TestClient:
    from app import app

    return TestClient(app)


def test_health(client: TestClient) -> None:
    r = client.get("/api/health")
    assert r.status_code == 200
    data = r.json()
    assert data["status"] == "healthy"
    assert "default_metadata_provider" in data
    assert "navidrome_path" in data


def test_formats(client: TestClient) -> None:
    r = client.get("/api/formats")
    assert r.status_code == 200
    data = r.json()
    assert "formats" in data
    assert "qualities" in data
    values = {f["value"] for f in data["formats"]}
    assert "mp3" in values and "flac" in values


def test_metadata_providers(client: TestClient) -> None:
    r = client.get("/api/metadata/providers")
    assert r.status_code == 200
    data = r.json()
    assert data["default"] in ("deezer", "spotify")
    ids = {p["id"] for p in data["providers"]}
    assert "deezer" in ids and "spotify" in ids


@pytest.mark.skipif(
    not _HAS_FRONTEND_BUNDLE,
    reason="frontend not built (backend/frontend/build/index.html missing) — run `npm run build` in frontend/",
)
def test_root_returns_html(client: TestClient) -> None:
    r = client.get("/")
    assert r.status_code == 200
    assert "text/html" in r.headers.get("content-type", "")


def test_search_invalid_provider(client: TestClient) -> None:
    r = client.post(
        "/api/search",
        json={"query": "test", "provider": "not-a-provider"},
    )
    assert r.status_code == 400
    assert "provider" in r.json()["detail"].lower()
