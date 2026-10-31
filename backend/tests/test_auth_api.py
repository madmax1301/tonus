"""HTTP-Tests für /api/auth/* — Setup, Login, Refresh, Logout, TOTP, PATs,
User-Verwaltung, Bans und /me.

Laufen gegen die echte App und eine leere Auth-DB pro Test. Sie halten das
Verhalten fest, bevor die Endpoints aus app.py in einen eigenen Router
wandern.
"""

from __future__ import annotations

import pyotp
import pytest
from fastapi.testclient import TestClient

from utils.job_store import _db

_AUTH_TABLES = ("pats", "refresh_tokens", "login_attempts", "banned_ips", "users")


def _wipe_auth_tables():
    conn = _db()
    try:
        for table in _AUTH_TABLES:
            conn.execute(f"DELETE FROM {table}")
        conn.commit()
    finally:
        conn.close()


@pytest.fixture
def client():
    """Leere Auth-DB vor und nach jedem Test — andere Testmodule erwarten
    den Setup-Modus (kein User angelegt)."""
    from app import app

    _wipe_auth_tables()
    yield TestClient(app)
    _wipe_auth_tables()


def _setup_admin(client, username="admin", password="correct horse battery"):
    r = client.post("/api/auth/setup", json={"username": username, "password": password})
    assert r.status_code == 200, r.text
    return r.json()


def _bearer(tokens):
    return {"Authorization": f"Bearer {tokens['access']}"}


def _login(client, username, password, **extra):
    return client.post("/api/auth/login", json={"username": username, "password": password, **extra})


# ── Setup ───────────────────────────────────────────────────────────────

def test_setup_creates_first_admin_once(client):
    assert client.get("/api/auth/setup-status").json()["setup_required"] is True
    body = _setup_admin(client)
    assert body["user"]["is_admin"] is True
    assert body["tokens"]["access"] and body["tokens"]["refresh"]
    assert body["totp_secret"] is None
    assert client.get("/api/auth/setup-status").json()["setup_required"] is False

    again = client.post("/api/auth/setup", json={"username": "x", "password": "another long one"})
    assert again.status_code == 409


def test_setup_with_totp_returns_secret_but_does_not_activate_it(client):
    r = client.post("/api/auth/setup",
                    json={"username": "admin", "password": "correct horse battery", "enable_totp": True})
    body = r.json()
    assert body["totp_secret"] and body["totp_qr_data_url"].startswith("data:image/svg+xml;base64,")
    # Erst totp-confirm schaltet 2FA scharf — Login geht noch ohne Code.
    assert _login(client, "admin", "correct horse battery").status_code == 200


# ── Login / Refresh / Logout / me ───────────────────────────────────────

def test_login_and_me(client):
    _setup_admin(client)
    r = _login(client, "admin", "correct horse battery")
    assert r.status_code == 200
    tokens = r.json()["tokens"]
    me = client.get("/api/auth/me", headers=_bearer(tokens)).json()
    assert me["username"] == "admin"
    assert me["is_admin"] is True
    assert me["totp_enabled"] is False


def test_login_rejects_wrong_password_and_missing_fields(client):
    _setup_admin(client)
    assert _login(client, "admin", "wrong").status_code == 401
    assert _login(client, "nobody", "whatever").status_code == 401
    assert _login(client, "  ", "x").status_code == 400


def test_five_failed_logins_ban_the_ip(client):
    """Fünf Fehlversuche von einer IP → Lifetime-Ban. Der Ban greift vor dem
    Rate-Limit pro Username, deshalb 403 statt 429."""
    _setup_admin(client)
    for _ in range(5):
        assert _login(client, "admin", "wrong").status_code == 401
    assert _login(client, "admin", "correct horse battery").status_code == 403


def test_protected_endpoint_needs_token_once_a_user_exists(client):
    _setup_admin(client)
    assert client.get("/api/auth/me").status_code == 401
    assert client.get("/api/auth/me", headers={"Authorization": "Bearer junk"}).status_code == 401


def test_refresh_rotates_and_revokes_old_token(client):
    tokens = _setup_admin(client)["tokens"]
    r = client.post("/api/auth/refresh", json={"refresh_token": tokens["refresh"]})
    assert r.status_code == 200
    new = r.json()["tokens"]
    assert new["refresh"] != tokens["refresh"]
    # Das alte Refresh-Token ist nach der Rotation verbraucht.
    assert client.post("/api/auth/refresh", json={"refresh_token": tokens["refresh"]}).status_code == 401
    assert client.post("/api/auth/refresh", json={"refresh_token": "junk"}).status_code == 401


def test_logout_revokes_refresh_token(client):
    tokens = _setup_admin(client)["tokens"]
    r = client.post("/api/auth/logout", json={"refresh_token": tokens["refresh"]}, headers=_bearer(tokens))
    assert r.json() == {"ok": True, "revoked": 1}
    assert client.post("/api/auth/refresh", json={"refresh_token": tokens["refresh"]}).status_code == 401


# ── TOTP ────────────────────────────────────────────────────────────────

def test_totp_init_confirm_login_disable(client):
    tokens = _setup_admin(client)["tokens"]
    h = _bearer(tokens)

    init = client.post("/api/auth/totp-init", headers=h).json()
    secret = init["secret"]
    assert client.post("/api/auth/totp-confirm", json={"secret": secret, "code": "000000"},
                       headers=h).status_code == 401
    code = pyotp.TOTP(secret).now()
    assert client.post("/api/auth/totp-confirm", json={"secret": secret, "code": code},
                       headers=h).status_code == 200
    assert client.get("/api/auth/me", headers=h).json()["totp_enabled"] is True
    assert client.post("/api/auth/totp-init", headers=h).status_code == 409

    no_code = _login(client, "admin", "correct horse battery")
    assert no_code.status_code == 401
    assert no_code.headers.get("x-auth-required-2fa") == "true"
    assert _login(client, "admin", "correct horse battery",
                  totp_code=pyotp.TOTP(secret).now()).status_code == 200

    assert client.post("/api/auth/totp-disable", json={"password": "wrong"},
                       headers=h).status_code == 401
    assert client.post("/api/auth/totp-disable", json={"password": "correct horse battery"},
                       headers=h).status_code == 401
    assert client.post("/api/auth/totp-disable",
                       json={"password": "correct horse battery", "totp_code": pyotp.TOTP(secret).now()},
                       headers=h).status_code == 200
    assert _login(client, "admin", "correct horse battery").status_code == 200


# ── PATs ────────────────────────────────────────────────────────────────

def test_pat_lifecycle(client):
    h = _bearer(_setup_admin(client)["tokens"])
    assert client.post("/api/auth/pats", json={"name": ""}, headers=h).status_code == 400

    created = client.post("/api/auth/pats", json={"name": "plugin"}, headers=h).json()
    plain = created["token"]
    pat_h = {"Authorization": f"Bearer {plain}"}
    assert client.get("/api/auth/me", headers=pat_h).json()["username"] == "admin"

    listed = client.get("/api/auth/pats", headers=h).json()["pats"]
    assert [p["name"] for p in listed] == ["plugin"]
    assert "token" not in listed[0]

    assert client.delete(f"/api/auth/pats/{created['id']}", headers=h).status_code == 200
    assert client.get("/api/auth/me", headers=pat_h).status_code == 401
    assert client.delete(f"/api/auth/pats/{created['id']}", headers=h).status_code == 404


# ── User-Verwaltung ─────────────────────────────────────────────────────

def test_user_management_and_admin_guard(client):
    admin = _setup_admin(client)
    h = _bearer(admin["tokens"])
    admin_id = admin["user"]["id"]

    created = client.post("/api/auth/users", json={"username": "bob", "password": "another long one"},
                          headers=h)
    assert created.status_code == 200
    bob_id = created.json()["user"]["id"]
    assert client.post("/api/auth/users", json={"username": "bob", "password": "another long one"},
                       headers=h).status_code == 400

    users = client.get("/api/auth/users", headers=h).json()["users"]
    assert {u["username"] for u in users} == {"admin", "bob"}
    assert all("password_hash" not in u and "totp_secret" not in u for u in users)

    # Nicht-Admins kommen an die Admin-Endpoints nicht ran.
    bob_h = _bearer(_login(client, "bob", "another long one").json()["tokens"])
    assert client.get("/api/auth/users", headers=bob_h).status_code == 403
    assert client.get("/api/auth/pats", headers=bob_h).status_code == 403

    # Letzter Admin: weder löschen noch degradieren; sich selbst löschen auch nicht.
    assert client.delete(f"/api/auth/users/{admin_id}", headers=h).status_code == 400
    assert client.patch(f"/api/auth/users/{admin_id}", json={"is_admin": False},
                        headers=h).status_code == 400

    # Admin-Reset fremder Passwörter braucht kein current_password …
    assert client.patch(f"/api/auth/users/{bob_id}", json={"password": "a brand new one"},
                        headers=h).status_code == 200
    assert _login(client, "bob", "a brand new one").status_code == 200
    # … die eigene Änderung schon.
    assert client.patch(f"/api/auth/users/{admin_id}", json={"password": "a brand new one"},
                        headers=h).status_code == 400
    assert client.patch(f"/api/auth/users/{admin_id}",
                        json={"password": "a brand new one", "current_password": "wrong"},
                        headers=h).status_code == 401

    assert client.delete(f"/api/auth/users/{bob_id}", headers=h).status_code == 200
    assert client.delete(f"/api/auth/users/{bob_id}", headers=h).status_code == 404


# ── Bans ────────────────────────────────────────────────────────────────

def test_banned_ip_list_and_unban(client):
    from utils import auth_users as au

    h = _bearer(_setup_admin(client)["tokens"])
    au.ban_ip("203.0.113.7", reason="test")
    banned = client.get("/api/auth/banned-ips", headers=h).json()["banned"]
    assert [b["ip"] for b in banned] == ["203.0.113.7"]
    assert client.delete("/api/auth/banned-ips/203.0.113.7", headers=h).status_code == 200
    assert client.delete("/api/auth/banned-ips/203.0.113.7", headers=h).status_code == 404
    au.ban_ip("2001:db8::1", reason="test")
    assert client.delete("/api/auth/banned-ips/2001:db8::1", headers=h).status_code == 200
