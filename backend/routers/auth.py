"""/api/auth/* — Setup, Login, Tokens, 2FA, PATs, User-Verwaltung, Bans.

Aus app.py ausgelagert. Verhalten unverändert bis auf eine Ausnahme: der
Login protokolliert die Client-IP jetzt über utils.auth.client_ip, das
X-Forwarded-For nur von TRUSTED_PROXIES annimmt (Audit H-7). Vorher nahm
der Login-Pfad den Header von jedem Client, womit sich der Auto-Ban auf
fremde IPs umlenken oder umgehen ließ.
"""

import time
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

import config
from utils.auth import auth_required, client_ip, require_admin, require_token

router = APIRouter()


# ──────────────────────────────────────────────────────────────────────
# Phase F.2 — Auth-Endpoints
#
# /api/auth/setup-status  — public, sagt ob noch ein Initial-Setup nötig ist
# /api/auth/setup         — public, legt den ersten Admin an (nur einmal)
# /api/auth/login         — public, Username + Password (+ optional TOTP) → JWT pair
# /api/auth/refresh       — public, Refresh-Token → neues Access-Token
# /api/auth/logout        — auth, revoke aktuelles Refresh-Token
# /api/auth/me            — auth, gibt den aktuell auth'd User zurück
# ──────────────────────────────────────────────────────────────────────

class AuthSetupRequest(BaseModel):
    username: str
    password: str
    enable_totp: bool = False


class AuthLoginRequest(BaseModel):
    username: str
    password: str
    totp_code: Optional[str] = None


class AuthRefreshRequest(BaseModel):
    refresh_token: str


class AuthLogoutRequest(BaseModel):
    refresh_token: Optional[str] = None


@router.get("/api/auth/setup-status")
async def auth_setup_status():
    """Public — Frontend nutzt das beim ersten Page-Load um zu erkennen,
    ob ein Setup-Wizard gezeigt werden muss. Auth-aktiv wenn entweder
    ein User registriert ist oder Legacy-Token konfiguriert."""
    from utils import auth_users as au
    return {
        "setup_required": au.setup_required(),
        "auth_active": auth_required(),
        "legacy_token_active": bool(config.TONUS_API_TOKEN),
    }


@router.post("/api/auth/setup")
async def auth_setup(req: AuthSetupRequest):
    """Bootstrap: legt den ersten Admin-User an. Nur einmal aufrufbar —
    sobald ein User existiert, gibt's 409."""
    from utils import auth_users as au
    if not au.setup_required():
        raise HTTPException(status_code=409, detail="Setup bereits abgeschlossen")
    try:
        user = au.create_user(req.username, req.password, is_admin=True)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    # Optional: TOTP-Setup vorbereiten — aber NICHT direkt scharf machen!
    # Frontend zeigt QR + Code-Input → POST /api/auth/totp-confirm verifies
    # den ersten Code BEVOR das Secret in die DB landet. Sonst kann ein
    # User mit kaputtem QR-Scan sich selbst aussperren (TOTP aktiv, aber
    # er hat keinen funktionierenden Authenticator).
    totp_uri: Optional[str] = None
    totp_secret: Optional[str] = None
    totp_qr_data_url: Optional[str] = None
    if req.enable_totp:
        import qrcode as _qrcode
        import qrcode.image.svg as _qrcode_svg
        import base64 as _b64
        from io import BytesIO as _BytesIO

        totp_secret = au.generate_totp_secret()
        # NICHT au.set_totp_secret hier — erst nach Verify-Step.
        totp_uri = au.totp_provisioning_uri(totp_secret, req.username)

        # QR server-side rendern (SVG, nicht PNG) — vermeidet Pillow-
        # Dependency, ist scharfer als PNG und kleiner. Der otpauth-URI
        # enthält das Klartext-Secret, also nicht an externe QR-Services.
        _img = _qrcode.make(totp_uri, image_factory=_qrcode_svg.SvgImage)
        _buf = _BytesIO()
        _img.save(_buf)
        totp_qr_data_url = "data:image/svg+xml;base64," + _b64.b64encode(_buf.getvalue()).decode("ascii")

    # Direkt-Login nach Setup — User soll nicht extra einloggen müssen.
    pair = au.issue_jwt_pair(user["id"], req.username, is_admin=True)
    au.touch_last_login(user["id"])

    return {
        "user": {"id": user["id"], "username": req.username, "is_admin": True},
        "tokens": pair,
        "totp_secret": totp_secret,  # nur einmal sichtbar
        "totp_uri": totp_uri,
        "totp_qr_data_url": totp_qr_data_url,
    }


@router.post("/api/auth/login")
async def auth_login(req: AuthLoginRequest, request: Request):
    """Username + Password (+ TOTP wenn aktiv) → JWT pair. Wirft 401 bei
    falschen Creds. 429 bei Rate-Limit nach 5 Failed-Attempts/15min.
    403 wenn die IP bereits gebannt ist (Lifetime-Ban nach 5+ Fails/24h)."""
    from utils import auth_users as au
    from utils.auth import assert_ip_not_banned

    # Pre-Auth IP-Ban-Check — gebannte IPs erst gar nicht in den Username/
    # Password-Pfad lassen. Verhindert auch dass record_login_attempt sie
    # weiter mitzählt und u.U. ein Recovery via "alter ttl" möglich wäre.
    assert_ip_not_banned(request)

    username = req.username.strip()
    if not username or not req.password:
        raise HTTPException(status_code=400, detail="Username und Passwort erforderlich")

    if au.is_rate_limited(username):
        raise HTTPException(
            status_code=429,
            detail="Zu viele Fehlversuche. Bitte 15 Minuten warten.",
        )

    user = au.get_user_by_username(username)
    ip = client_ip(request)

    if not user or not au.verify_password(req.password, user["password_hash"]):
        au.record_login_attempt(username, ip, success=False)
        raise HTTPException(
            status_code=401,
            detail="Falsche Anmeldedaten",
            headers={"WWW-Authenticate": "Bearer"},
        )

    # TOTP-Check wenn aktiviert
    if user["totp_secret"]:
        secret = au.get_user_totp_secret(user["id"])
        if not secret or not au.verify_totp_code(secret, req.totp_code or ""):
            au.record_login_attempt(username, ip, success=False)
            raise HTTPException(
                status_code=401,
                detail="2FA-Code fehlt oder falsch",
                headers={"X-Auth-Required-2FA": "true"},
            )

    pair = au.issue_jwt_pair(user["id"], user["username"], bool(user["is_admin"]))
    au.touch_last_login(user["id"])
    au.record_login_attempt(username, ip, success=True)

    return {
        "user": {
            "id": user["id"],
            "username": user["username"],
            "is_admin": bool(user["is_admin"]),
            "totp_enabled": bool(user["totp_secret"]),
        },
        "tokens": pair,
    }


@router.post("/api/auth/refresh")
async def auth_refresh(req: AuthRefreshRequest):
    """Tausche Refresh-Token gegen neues Access-Token (+ rotiertem Refresh).
    Old refresh wird invalidiert (token-rotation pattern)."""
    from utils import auth_users as au
    claims = au.decode_jwt(req.refresh_token, expected_type="refresh")
    if not claims:
        raise HTTPException(status_code=401, detail="Refresh-Token ungültig oder abgelaufen")

    user_id = int(claims.get("sub", 0))
    user = au.get_user_by_id(user_id)
    if not user:
        raise HTTPException(status_code=401, detail="User nicht gefunden")

    # Rotation: altes refresh-jti revoken, neues pair ausstellen.
    au.revoke_refresh_token(claims["jti"])
    pair = au.issue_jwt_pair(user["id"], user["username"], bool(user["is_admin"]))
    return {"tokens": pair}


@router.post("/api/auth/logout")
async def auth_logout(req: AuthLogoutRequest, request: Request,
                      _: None = Depends(require_token)):
    """Invalidiert das übergebene Refresh-Token. Access-Token läuft sowieso
    nach JWT_ACCESS_TTL_MIN ab — Logout-Sicherheit kommt vom Refresh-Revoke.
    Wenn refresh_token nicht im Body: revoke ALLE refresh-tokens des Users
    (Logout-everywhere)."""
    from utils import auth_users as au
    user = request.state.user
    user_id = int(user.get("id", 0))
    if user_id <= 0:
        # Legacy/Setup-Auth → nichts zu logouten
        return {"ok": True, "revoked": 0}

    if req.refresh_token:
        claims = au.decode_jwt(req.refresh_token, expected_type="refresh")
        if claims and claims.get("jti"):
            au.revoke_refresh_token(claims["jti"])
            return {"ok": True, "revoked": 1}
        return {"ok": True, "revoked": 0}

    n = au.revoke_all_user_refresh_tokens(user_id)
    return {"ok": True, "revoked": n}


class TotpConfirmRequest(BaseModel):
    secret: str
    code: str


class TotpDisableRequest(BaseModel):
    password: str
    totp_code: Optional[str] = None


@router.post("/api/auth/totp-init")
async def auth_totp_init(request: Request, _: None = Depends(require_token)):
    """Generiert ein frisches TOTP-Secret + Provisioning-URI + QR-PNG (data-URL)
    für nachträgliches 2FA-Setup aus den Settings. Wird NICHT in der DB
    persistiert — der Aufrufer muss den ersten Code via /api/auth/totp-confirm
    verifizieren, das aktiviert das Secret dann scharf.
    Verify-First-Activate-Second.

    QR wird hier serverseitig als SVG gerendert + base64-data-URL, damit
    das Secret nicht an einen externen QR-Service raussickert — der otpauth-URI
    enthält das Secret im Klartext. SVG vermeidet Pillow-Dependency.

    409 wenn TOTP für den User schon aktiv ist — erst /totp-disable nötig."""
    from utils import auth_users as au
    import qrcode
    import qrcode.image.svg as qrcode_svg
    import base64
    from io import BytesIO

    user = request.state.user
    user_id = int(user.get("id", 0))
    if user_id <= 0:
        raise HTTPException(status_code=403, detail="TOTP-Init nur für eingeloggte User")
    db_user = au.get_user_by_id(user_id)
    if not db_user:
        raise HTTPException(status_code=401, detail="User nicht mehr gefunden")
    if db_user["totp_secret"]:
        raise HTTPException(
            status_code=409,
            detail="TOTP ist bereits aktiv — erst deaktivieren, dann neu einrichten.",
        )
    secret = au.generate_totp_secret()
    uri = au.totp_provisioning_uri(secret, db_user["username"])

    img = qrcode.make(uri, image_factory=qrcode_svg.SvgImage)
    buf = BytesIO()
    img.save(buf)
    qr_data_url = "data:image/svg+xml;base64," + base64.b64encode(buf.getvalue()).decode("ascii")

    return {"secret": secret, "uri": uri, "qr_data_url": qr_data_url}


@router.post("/api/auth/totp-confirm")
async def auth_totp_confirm(req: TotpConfirmRequest, request: Request,
                             _: None = Depends(require_token)):
    """Verifiziert den ersten TOTP-Code BEVOR das Secret scharf in der DB
    landet. So kann sich ein User mit kaputtem Authenticator-Setup nicht
    aussperren — TOTP wird nur aktiviert wenn er nachweisen kann dass
    seine App den Code korrekt generiert."""
    from utils import auth_users as au
    user = request.state.user
    user_id = int(user.get("id", 0))
    if user_id <= 0:
        raise HTTPException(status_code=403, detail="TOTP-Confirm nur für eingeloggte User")

    if not au.verify_totp_code(req.secret, req.code):
        raise HTTPException(status_code=401, detail="TOTP-Code falsch oder abgelaufen")

    au.set_totp_secret(user_id, req.secret)
    return {"ok": True}


@router.post("/api/auth/totp-disable")
async def auth_totp_disable(req: TotpDisableRequest, request: Request,
                             _: None = Depends(require_token)):
    """Deaktiviert TOTP für den eingeloggten User. Verlangt Password-Re-Verify
    plus aktuellen TOTP-Code (wenn 2FA gerade aktiv ist) — sonst könnte ein
    geklauter Access-Token allein genügen, um 2FA auszuhebeln. Defense gegen
    Session-Hijack."""
    from utils import auth_users as au
    user = request.state.user
    user_id = int(user.get("id", 0))
    if user_id <= 0:
        raise HTTPException(status_code=403, detail="TOTP-Disable nur für eingeloggte User")
    db_user = au.get_user_by_id(user_id)
    if not db_user:
        raise HTTPException(status_code=401, detail="User nicht mehr gefunden")

    if not au.verify_password(req.password, db_user["password_hash"]):
        raise HTTPException(status_code=401, detail="Passwort falsch")

    if db_user["totp_secret"]:
        secret = au.get_user_totp_secret(user_id)
        if not secret or not au.verify_totp_code(secret, req.totp_code or ""):
            raise HTTPException(
                status_code=401,
                detail="TOTP-Code fehlt oder ist falsch",
            )

    au.disable_totp(user_id)
    return {"ok": True}


class PatCreateRequest(BaseModel):
    name: str
    expires_in_days: Optional[int] = None  # None ⇒ unbegrenzt


def _user_id_from_request(request: Request) -> int:
    """Extrahiert die User-ID aus dem authentifizierten Request. Wirft 403
    für legacy/setup-Auth (kein User-Datensatz dahinter — PATs gehören zu
    einem konkreten User)."""
    user = request.state.user
    user_id = int(user.get("id", 0))
    if user_id <= 0:
        raise HTTPException(
            status_code=403,
            detail="API-Tokens sind nur für eingeloggte User mit Account verfügbar.",
        )
    return user_id


@router.post("/api/auth/pats")
async def auth_pats_create(req: PatCreateRequest, request: Request,
                            _: None = Depends(require_token),
                            __: None = Depends(require_admin)):
    """Erstellt einen neuen Personal Access Token. Plain-Token wird NUR
    in dieser Antwort zurückgegeben — Backend speichert nur den sha256-Hash.
    Caller (Frontend) muss den Plain-Token einmalig anzeigen + verwerfen.

    Admin-only: PATs sind System-zu-System-Credentials (Plugin-Auth) und
    sollten nur vom Admin angelegt werden. Reguläre User haben keinen
    Use-Case für eigene Long-Lived-Tokens."""
    from utils import auth_users as au
    user_id = _user_id_from_request(request)
    name = (req.name or "").strip()
    if not name or len(name) > 64:
        raise HTTPException(status_code=400, detail="Name muss 1–64 Zeichen lang sein.")

    expires_at_ms: Optional[int] = None
    if req.expires_in_days and req.expires_in_days > 0:
        expires_at_ms = int(time.time() * 1000) + req.expires_in_days * 24 * 60 * 60 * 1000

    try:
        pat = au.issue_pat(user_id, name, expires_at_ms=expires_at_ms)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return pat


@router.get("/api/auth/pats")
async def auth_pats_list(request: Request,
                          _: None = Depends(require_token),
                          __: None = Depends(require_admin)):
    """Liste aller PATs des eingeloggten Admins — ohne Plain-Token (gibt's nicht
    mehr, nur prefix für Identifikation)."""
    from utils import auth_users as au
    user_id = _user_id_from_request(request)
    return {"pats": au.list_pats(user_id)}


@router.delete("/api/auth/pats/{pat_id}")
async def auth_pats_revoke(pat_id: int, request: Request,
                            _: None = Depends(require_token),
                            __: None = Depends(require_admin)):
    """Hard-Delete eines PATs. Owner-Check ist im SQL — der Admin kann nur
    seine eigenen Tokens widerrufen, fremde IDs returnieren 404."""
    from utils import auth_users as au
    user_id = _user_id_from_request(request)
    ok = au.revoke_pat(pat_id, user_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Token nicht gefunden.")
    return {"ok": True}


# ─────────────────────────────────────────────────────────────────────
# User-Management (Admin-only)
# ─────────────────────────────────────────────────────────────────────

class UserCreateRequest(BaseModel):
    username: str
    password: str
    is_admin: bool = False


class UserPatchRequest(BaseModel):
    is_admin: Optional[bool] = None
    password: Optional[str] = None
    # Pflichtfeld bei Self-Password-Change. Bei Admin-Reset auf einen
    # anderen User wird es ignoriert (Admin-Privileg). Backend entscheidet
    # anhand user_id == me.id ob es required ist.
    current_password: Optional[str] = None


def _public_user_record(u: Dict[str, Any]) -> Dict[str, Any]:
    """Filter sensitive Felder (password_hash, totp_secret) bevor wir Records
    ans Frontend geben. totp_enabled wird als bool surfacet (statt das
    Secret selbst zu enthüllen)."""
    return {
        "id": u.get("id"),
        "username": u.get("username"),
        "is_admin": bool(u.get("is_admin")),
        "totp_enabled": bool(u.get("totp_enabled")) if "totp_enabled" in u else bool(u.get("totp_secret")),
        "created_at_ms": u.get("created_at_ms"),
        "last_login_at_ms": u.get("last_login_at_ms"),
    }


@router.get("/api/auth/users")
async def auth_users_list(_: None = Depends(require_token),
                           __: None = Depends(require_admin)):
    """Liste aller User. Admin-only."""
    from utils import auth_users as au
    return {"users": [_public_user_record(u) for u in au.list_users()]}


@router.post("/api/auth/users")
async def auth_users_create(req: UserCreateRequest,
                              _: None = Depends(require_token),
                              __: None = Depends(require_admin)):
    """Legt neuen User an. Admin-only.
    400 wenn Username schon existiert oder Validation fehlschlägt."""
    from utils import auth_users as au
    try:
        user = au.create_user(req.username, req.password, is_admin=req.is_admin)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"user": _public_user_record(user)}


@router.delete("/api/auth/users/{user_id}")
async def auth_users_delete(user_id: int, request: Request,
                              _: None = Depends(require_token),
                              __: None = Depends(require_admin)):
    """Hard-Delete. Schutz:
    - Self-Delete: nicht erlaubt (Self-Lockout-Risiko)
    - Last-Admin-Delete: nicht erlaubt (System wäre unverwaltbar)
    Cascade: User's PATs + refresh_tokens werden mit gelöscht."""
    from utils import auth_users as au
    me = request.state.user
    if int(me.get("id", 0)) == user_id:
        raise HTTPException(status_code=400, detail="Du kannst dich nicht selbst löschen.")
    target = au.get_user_by_id(user_id)
    if not target:
        raise HTTPException(status_code=404, detail="User nicht gefunden.")
    if bool(target.get("is_admin")) and au.admin_count() <= 1:
        raise HTTPException(
            status_code=400,
            detail="Letzter Admin — kann nicht gelöscht werden.",
        )
    au.delete_user(user_id)
    return {"ok": True}


@router.patch("/api/auth/users/{user_id}")
async def auth_users_patch(user_id: int, req: UserPatchRequest, request: Request,
                             _: None = Depends(require_token),
                             __: None = Depends(require_admin)):
    """Toggle Admin-Flag oder reset Password. Admin-only.
    Schutz: Last-Admin-Demotion (eigener oder fremder Account) wird verhindert."""
    from utils import auth_users as au
    target = au.get_user_by_id(user_id)
    if not target:
        raise HTTPException(status_code=404, detail="User nicht gefunden.")

    if req.is_admin is not None:
        # Demotion eines Admins blockieren wenn er der letzte ist (egal ob
        # Self oder von anderem Admin getriggert).
        if not req.is_admin and bool(target.get("is_admin")) and au.admin_count() <= 1:
            raise HTTPException(
                status_code=400,
                detail="Letzter Admin — kann nicht demoted werden.",
            )
        au.set_user_admin(user_id, req.is_admin)

    if req.password is not None:
        # Self-Password-Change verlangt Re-Verify mit current_password —
        # Defense gegen Session-Hijack, ein gestohlener Access-Token soll
        # nicht direkt das Master-Credential ändern können. Admin-Reset
        # auf einen anderen User braucht das nicht (Admin-Privileg).
        me = request.state.user
        is_self = int(me.get("id", 0)) == user_id
        if is_self:
            if not req.current_password:
                raise HTTPException(
                    status_code=400,
                    detail="Aktuelles Passwort ist erforderlich für eigene Passwort-Änderung.",
                )
            if not au.verify_password(req.current_password, target["password_hash"]):
                raise HTTPException(status_code=401, detail="Aktuelles Passwort ist falsch.")
        try:
            au.update_user_password(user_id, req.password)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))

    return {"ok": True}


# ─────────────────────────────────────────────────────────────────────
# Banned-IPs (Admin-only)
# ─────────────────────────────────────────────────────────────────────

@router.get("/api/auth/banned-ips")
async def auth_banned_ips_list(request: Request,
                                _: None = Depends(require_token),
                                __: None = Depends(require_admin)):
    """Liste aller lifetime-gebannten IPs (5+ Failed-Logins/24h). Admin-only."""
    from utils import auth_users as au
    return {"banned": au.list_banned_ips()}


@router.delete("/api/auth/banned-ips/{ip:path}")
async def auth_banned_ips_unban(ip: str, request: Request,
                                  _: None = Depends(require_token),
                                  __: None = Depends(require_admin)):
    """Entbannt eine IP. Path-Parameter mit `:path`-Converter, weil IPv6-Adressen
    Doppelpunkte enthalten und sonst von FastAPI's Default-Path-Matcher
    falsch geparst würden. 404 wenn IP nicht gebannt war."""
    from utils import auth_users as au
    ok = au.unban_ip(ip)
    if not ok:
        raise HTTPException(status_code=404, detail="IP war nicht gebannt.")
    return {"ok": True}


@router.get("/api/auth/me")
async def auth_me(request: Request, _: None = Depends(require_token)):
    """Currently authenticated user — Frontend nutzt das beim Mount um zu
    erkennen ob die Session noch valid ist und welche Berechtigungen
    der User hat."""
    user = request.state.user
    if user.get("auth_method") in ("legacy", "setup"):
        return {
            "id": user["id"],
            "username": user["username"],
            "is_admin": user["is_admin"],
            "auth_method": user["auth_method"],
            "totp_enabled": False,
        }
    from utils import auth_users as au
    db_user = au.get_user_by_id(user["id"])
    if not db_user:
        raise HTTPException(status_code=401, detail="User nicht mehr gefunden")
    return {
        "id": db_user["id"],
        "username": db_user["username"],
        "is_admin": bool(db_user["is_admin"]),
        "auth_method": user["auth_method"],
        "totp_enabled": bool(db_user["totp_secret"]),
        "last_login_at_ms": db_user["last_login_at_ms"],
    }
