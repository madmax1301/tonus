"""Jeder Endpoint außer den beiden Health-Checks verlangt ein Token, sobald
ein User existiert. Die Liste unten ist bewusst explizit: ein neuer
ungeschützter Endpoint fällt in test_no_unprotected_routes_left auf."""

from __future__ import annotations

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from tests.test_auth_api import _setup_admin, _wipe_auth_tables

PUBLIC = {
    ("GET", "/api/health"),
    ("GET", "/api/plugin/health"),
    # Login-Flow: muss ohne Token erreichbar sein.
    ("GET", "/api/auth/setup-status"),
    ("POST", "/api/auth/setup"),
    ("POST", "/api/auth/login"),
    ("POST", "/api/auth/refresh"),
}

NEWLY_PROTECTED = [
    ("GET", "/api/metadata/providers"),
    ("POST", "/api/search"),
    ("POST", "/api/reverse/youtube"),
    ("POST", "/api/search/tracks/top"),
    ("POST", "/api/search/albums"),
    ("POST", "/api/search/artists"),
    ("GET", "/api/album/x"),
    ("GET", "/api/track/x"),
    ("GET", "/api/download/status/x"),
    ("GET", "/api/download/album/status/x"),
    ("GET", "/api/download/artist/status/x"),
    ("GET", "/api/youtube/candidates/x"),
    ("GET", "/api/download/file/x"),
    ("GET", "/api/navidrome/libraries"),
    ("GET", "/api/track/x/exists"),
    ("GET", "/api/formats"),
    ("POST", "/api/recommendations"),
    ("POST", "/api/url/search"),
    ("GET", "/api/plugin/sync-status"),
    ("GET", "/api/plugin/finished-tracks?navidrome_user=u"),
]


@pytest.fixture
def client():
    from app import app

    _wipe_auth_tables()
    yield TestClient(app)
    _wipe_auth_tables()


@pytest.mark.parametrize("method,path", NEWLY_PROTECTED)
def test_endpoint_needs_token(client, method, path):
    _setup_admin(client)
    r = client.request(method, path, json={})
    assert r.status_code == 401, (method, path, r.status_code)


def test_health_endpoints_stay_public(client):
    _setup_admin(client)
    assert client.get("/api/health").status_code == 200
    assert client.get("/api/plugin/health").status_code == 200


def test_token_opens_protected_endpoint(client):
    tokens = _setup_admin(client)["tokens"]
    r = client.get("/api/formats", headers={"Authorization": f"Bearer {tokens['access']}"})
    assert r.status_code == 200




def test_no_unprotected_routes_left(client):
    """Alle /api-Routen ohne Token → 401/403, außer PUBLIC. Schützt davor,
    dass ein neuer Endpoint ohne require_token durchrutscht."""
    from app import app

    _setup_admin(client)
    open_routes = []
    checked = 0

    def walk(routes):
        nonlocal checked
        for route in routes:
            # FastAPI hängt include_router() als einen Eintrag ein; die
            # Routen liegen am ursprünglichen Router (ohne Prefix, siehe
            # routers/*.py — die Pfade dort sind absolut).
            inner = getattr(route, "original_router", None)
            if inner is not None:
                walk(inner.routes)
                continue
            if not isinstance(route, APIRoute) or not route.path.startswith("/api/"):
                continue
            for method in route.methods - {"HEAD", "OPTIONS"}:
                if (method, route.path) in PUBLIC:
                    continue
                probe = route.path
                for name in route.param_convertors:
                    probe = probe.replace("{" + name + "}", "x").replace("{" + name + ":path}", "x")
                r = client.request(method, probe, json={})
                checked += 1
                if r.status_code not in (401, 403):
                    open_routes.append((method, route.path, r.status_code))

    walk(app.router.routes)
    assert checked > 60, checked  # app.py + routers/auth.py, Walker nicht leer
    assert not open_routes, open_routes
