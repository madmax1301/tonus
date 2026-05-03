"""Bearer-Token-Schutz für Mutate-Endpoints.

Phase F: Drei Auth-Quellen werden akzeptiert (in dieser Reihenfolge probiert):

  1. **JWT-Access-Token** (typisch Browser-Session):
     "Authorization: Bearer eyJhbGciOiJIUzI1Ni..."
     → decoded claims werden in ``request.state.user`` abgelegt:
        {"id": int, "username": str, "is_admin": bool, "auth_method": "jwt"}

  2. **Personal Access Token** (Plugin/CLI/MCP):
     "Authorization: Bearer tonus_pat_<base64url>"
     → PAT-Lookup in DB, user-Daten in ``request.state.user``:
        {"id": int, "username": str, "is_admin": bool,
         "pat_id": int, "pat_name": str, "auth_method": "pat"}

  3. **Legacy static API-Token** (Backwards-Compat, deprecated in F.5):
     ``config.TONUS_API_TOKEN`` Env-Var. Wenn gesetzt UND Header matcht,
     gilt als impliziter "system"-User:
        {"id": 0, "username": "_static", "is_admin": True,
         "auth_method": "legacy"}

Auth-Failures geben 401 mit ``WWW-Authenticate: Bearer`` Header zurück.
Kein Logging des Tokens (auch nicht bei Failure).

Wenn KEIN Auth-Mechanismus aktiv ist (kein User in DB UND kein TONUS_API_TOKEN),
ist die Dependency ein No-Op. Das hält Setup-Endpoint /api/auth/setup
zugänglich beim ersten Start.
"""
from __future__ import annotations

import secrets
from typing import Any, Dict, Optional

from fastapi import Header, HTTPException, Request, status

import config
from utils import auth_users


def _legacy_token_user() -> Dict[str, Any]:
    """Synthetic user-record für Legacy-static-Token-Auth."""
    return {
        "id": 0,
        "username": "_static",
        "is_admin": True,
        "auth_method": "legacy",
    }


def _try_jwt(token: str) -> Optional[Dict[str, Any]]:
    claims = auth_users.decode_jwt(token, expected_type="access")
    if not claims:
        return None
    return {
        "id": int(claims.get("sub", 0)),
        "username": claims.get("username", ""),
        "is_admin": bool(claims.get("is_admin", False)),
        "auth_method": "jwt",
    }


def _try_pat(token: str) -> Optional[Dict[str, Any]]:
    row = auth_users.verify_pat(token)
    if not row:
        return None
    return {
        "id": int(row["user_id"]),
        "username": row["username"],
        "is_admin": bool(row["is_admin"]),
        "pat_id": int(row["pat_id"]),
        "pat_name": row["name"],
        "auth_method": "pat",
    }


def _try_legacy(token: str) -> Optional[Dict[str, Any]]:
    expected = config.TONUS_API_TOKEN
    if not expected:
        return None
    if secrets.compare_digest(token, expected):
        return _legacy_token_user()
    return None


def _extract_bearer(authorization: Optional[str]) -> Optional[str]:
    if not authorization:
        return None
    parts = authorization.strip().split(None, 1)
    if len(parts) == 2 and parts[0].lower() == "bearer":
        return parts[1].strip()
    return None


def client_ip(request: Request) -> Optional[str]:
    """Extract client IP from request, respecting Reverse-Proxy-Header. Best-effort.
    Wird sowohl von require_token (für Pre-Auth-Ban-Check) als auch vom
    Login-Endpoint genutzt — daher Public-Helper hier statt Duplikat in app.py."""
    fwd = request.headers.get("x-forwarded-for")
    if fwd:
        return fwd.split(",")[0].strip()
    real = request.headers.get("x-real-ip")
    if real:
        return real.strip()
    return request.client.host if request.client else None


def assert_ip_not_banned(request: Request) -> None:
    """Pre-Auth-Hook gegen Brute-Force. Wird VOR Token-Verify aufgerufen, damit
    gebannte IPs erst gar keine Auth-Versuche timen können (kein Timing-Leak).
    Wirft 403 statt 401 — semantische Trennung: Ban ≠ falsche Credentials.
    Loopback ist immun (siehe auth_users._is_loopback_ip)."""
    ip = client_ip(request)
    if auth_users.is_ip_banned(ip):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Diese IP ist nach mehreren fehlgeschlagenen Login-Versuchen gesperrt.",
        )


def require_token(
    request: Request,
    authorization: Optional[str] = Header(default=None),
) -> None:
    """FastAPI-Dependency. Per ``Depends(require_token)`` an Mutate-Routen
    anhängen. Setzt bei Erfolg ``request.state.user`` mit dem authenticierten
    User-Record (siehe Modul-Docstring für Format)."""

    # Pre-Auth-Ban-Check: gebannte IPs kommen erst gar nicht zur Token-Verify.
    # Verhindert Timing-Leaks und entlastet die JWT/PAT-Pfade von Junk-Traffic.
    assert_ip_not_banned(request)

    # Setup-Mode: noch kein User in DB UND kein Legacy-Token gesetzt → offen.
    # Erlaubt /api/auth/setup beim ersten Start. Sobald der erste Admin
    # angelegt ist, greift Auth.
    legacy_active = bool(config.TONUS_API_TOKEN)
    setup_pending = auth_users.setup_required()
    if setup_pending and not legacy_active:
        # Nur wenn der User noch nicht angelegt ist — Setup-Endpoint kommt durch.
        request.state.user = {"id": 0, "username": "_setup", "is_admin": True,
                              "auth_method": "setup"}
        return

    token = _extract_bearer(authorization)
    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing API token",
            headers={"WWW-Authenticate": "Bearer"},
        )

    # Try in order: JWT (most common) → PAT → Legacy
    user = _try_jwt(token) or _try_pat(token) or _try_legacy(token)
    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing API token",
            headers={"WWW-Authenticate": "Bearer"},
        )

    request.state.user = user


def require_admin(request: Request) -> None:
    """Zusätzlich zu require_token: nur is_admin Users durchlassen.
    Für /api/auth/users-Mgmt-Endpoints. Voraussetzung: require_token wurde
    schon evaluiert (request.state.user ist gesetzt)."""
    user = getattr(request.state, "user", None)
    if not user or not user.get("is_admin"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin privilege required",
        )


def auth_required() -> bool:
    """Helper für Health/Plugin-Endpoints, die im Body anzeigen wollen, ob das
    Backend Auth erzwingt. Kein FastAPI-Dependency."""
    if config.TONUS_API_TOKEN:
        return True
    return not auth_users.setup_required()
