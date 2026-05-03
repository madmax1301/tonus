"""
Phase F — Multi-User-Auth Foundation.

Drei Auth-Pfade für /api/* Endpoints:

  1. **Browser-Session** via JWT-Bearer
     POST /api/auth/login (username, password, totp_code) → {access, refresh}
     Frontend hängt access-token an alle API-Calls. Bei 401 → /api/auth/refresh
     mit refresh-Token. Bei Logout → POST /api/auth/logout invalidiert das
     refresh-Token in der DB (access expired sowieso nach JWT_ACCESS_TTL_MIN).

  2. **Personal Access Token (PAT)** via Bearer
     User generiert in Settings einen Token (z.B. "Plex-Plugin"), bekommt EINMAL
     den Plain-Wert angezeigt und kopiert ihn in den Plugin-Config. Backend
     speichert nur sha256(token). PATs haben optional Scopes + Expiry.

  3. **Legacy static TONUS_API_TOKEN** via Bearer
     Backwards-compat für bestehende Configs. Wenn config.TONUS_API_TOKEN gesetzt
     ist, wird der Wert als impliziter PAT akzeptiert. Wird in Phase F.5 deprecated.

Crypto:
  - Passwords: argon2id (memory_cost=64 MiB, time_cost=3, parallelism=1) — robust
    gegen GPU-Cracking, akzeptable CPU-Zeit (~150 ms pro hash auf modernen CPUs).
  - JWT: HS256 mit auto-generiertem Secret aus auth_meta-Tabelle. Kann via
    JWT_SECRET-Env überschrieben werden.
  - TOTP: Standard 30s-Window, 6 Stellen. Secret in der DB liegt symmetrisch
    verschlüsselt (Fernet mit jwt_secret-Derivat als Key) — Schutz gegen
    Read-Only-DB-Leak.
  - PATs: 32 Byte random aus secrets.token_urlsafe → "tonus_pat_<base64url>".
    Beim Verify sha256-vergleich gegen pats.token_hash.
"""
from __future__ import annotations

import base64
import hashlib
import secrets
import time
from typing import Any, Dict, List, Optional

import jwt
import pyotp
from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError
from cryptography.fernet import Fernet, InvalidToken

import config
from utils.job_store import _db, _now_ms


# ─────────────────────────────────────────────────────────────────────
# JWT-Secret + symmetrischer TOTP-Schlüssel
# ─────────────────────────────────────────────────────────────────────


def get_or_init_jwt_secret() -> str:
    """JWT-Secret aus auth_meta-Tabelle. Wird beim ersten Start generiert.
    Per JWT_SECRET-Env überschreibbar (Replikations-Setup)."""
    if config.JWT_SECRET_OVERRIDE:
        return config.JWT_SECRET_OVERRIDE
    conn = _db()
    try:
        row = conn.execute(
            "SELECT value FROM auth_meta WHERE key = ?", ("jwt_secret",)
        ).fetchone()
        if row:
            return row["value"]
        # Generate + persist on first boot
        new_secret = secrets.token_urlsafe(64)
        conn.execute(
            "INSERT INTO auth_meta (key, value, updated_at_ms) VALUES (?, ?, ?)",
            ("jwt_secret", new_secret, _now_ms()),
        )
        conn.commit()
        return new_secret
    finally:
        conn.close()


def _totp_fernet() -> Fernet:
    """Symmetrischer Fernet-Key aus dem JWT-Secret abgeleitet.

    SHA-256 des JWT-Secrets → urlsafe-base64 → 32-Byte-Key — exakt was
    Fernet erwartet. Vorteil: kein zweites Secret nötig, beim Reset des
    JWT-Secrets müssen User TOTP neu setzen (was sicher ist).
    """
    digest = hashlib.sha256(get_or_init_jwt_secret().encode("utf-8")).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


# ─────────────────────────────────────────────────────────────────────
# Password-Hashing (argon2id)
# ─────────────────────────────────────────────────────────────────────

# Modul-globaler Hasher — argon2-cffi cached intern und ist thread-safe.
# Settings sind argon2-Defaults für interactive logins (~150 ms CPU).
_ph = PasswordHasher()


def hash_password(plain: str) -> str:
    """Hash a plaintext password with argon2id. Returns the encoded string,
    inkl. Algorithm-Marker + Salt + Parameters — selbst-beschreibend."""
    return _ph.hash(plain)


def verify_password(plain: str, encoded: str) -> bool:
    """True if plain matches the argon2-encoded hash."""
    try:
        return _ph.verify(encoded, plain)
    except VerifyMismatchError:
        return False
    except Exception:
        # Korruptes Hash, falsches Format etc. — niemals durchlassen.
        return False


# ─────────────────────────────────────────────────────────────────────
# JWT (Access + Refresh)
# ─────────────────────────────────────────────────────────────────────


def issue_jwt_pair(user_id: int, username: str, is_admin: bool) -> Dict[str, Any]:
    """Erzeugt access + refresh-Token. Persistiert refresh-jti in DB damit
    Logout/Rotation funktionieren. Access ist self-contained (keine DB-Lookup
    nötig beim Verify)."""
    secret = get_or_init_jwt_secret()
    now = int(time.time())
    access_exp = now + config.JWT_ACCESS_TTL_MIN * 60
    refresh_exp = now + config.JWT_REFRESH_TTL_DAYS * 24 * 60 * 60
    refresh_jti = secrets.token_urlsafe(16)

    access = jwt.encode(
        {
            "sub": str(user_id),
            "username": username,
            "is_admin": is_admin,
            "type": "access",
            "iat": now,
            "exp": access_exp,
        },
        secret,
        algorithm=config.JWT_ALGORITHM,
    )
    refresh = jwt.encode(
        {
            "sub": str(user_id),
            "type": "refresh",
            "jti": refresh_jti,
            "iat": now,
            "exp": refresh_exp,
        },
        secret,
        algorithm=config.JWT_ALGORITHM,
    )

    # Refresh-jti in DB persistieren — Logout / Rotation invalidiert
    conn = _db()
    try:
        conn.execute(
            "INSERT INTO refresh_tokens (jti, user_id, issued_at_ms, expires_at_ms) "
            "VALUES (?, ?, ?, ?)",
            (refresh_jti, user_id, _now_ms(), refresh_exp * 1000),
        )
        conn.commit()
    finally:
        conn.close()

    return {
        "access": access,
        "refresh": refresh,
        "access_expires_at": access_exp,
        "refresh_expires_at": refresh_exp,
    }


def decode_jwt(token: str, expected_type: str = "access") -> Optional[Dict[str, Any]]:
    """Decode + verify a JWT. Returns claims dict, or None if invalid/expired/
    wrong-type. Refresh-Tokens haben extra-Check gegen DB (revocation)."""
    try:
        claims = jwt.decode(token, get_or_init_jwt_secret(), algorithms=[config.JWT_ALGORITHM])
    except jwt.ExpiredSignatureError:
        return None
    except jwt.InvalidTokenError:
        return None
    if claims.get("type") != expected_type:
        return None
    if expected_type == "refresh":
        jti = claims.get("jti")
        if not jti:
            return None
        conn = _db()
        try:
            row = conn.execute(
                "SELECT revoked_at_ms FROM refresh_tokens WHERE jti = ?", (jti,)
            ).fetchone()
            if not row or row["revoked_at_ms"] is not None:
                return None
        finally:
            conn.close()
    return claims


def revoke_refresh_token(jti: str) -> None:
    """Mark a refresh-jti revoked. Idempotent — wenn bereits revoked, noop."""
    conn = _db()
    try:
        conn.execute(
            "UPDATE refresh_tokens SET revoked_at_ms = ? WHERE jti = ? AND revoked_at_ms IS NULL",
            (_now_ms(), jti),
        )
        conn.commit()
    finally:
        conn.close()


def revoke_all_user_refresh_tokens(user_id: int) -> int:
    """Logout-everywhere — invalidiert alle aktiven refresh-Tokens des Users.
    Returns count of revoked tokens. Sinnvoll bei Password-Change oder
    Sicherheits-Vorfall."""
    conn = _db()
    try:
        cur = conn.execute(
            "UPDATE refresh_tokens SET revoked_at_ms = ? "
            "WHERE user_id = ? AND revoked_at_ms IS NULL",
            (_now_ms(), user_id),
        )
        conn.commit()
        return cur.rowcount
    finally:
        conn.close()


# ─────────────────────────────────────────────────────────────────────
# TOTP — pyotp wrapper + DB-Storage
# ─────────────────────────────────────────────────────────────────────


def generate_totp_secret() -> str:
    """Random base32-Secret (160 bit) für einen neuen User-TOTP-Setup."""
    return pyotp.random_base32()


def encrypt_totp_secret(secret: str) -> str:
    """Symmetrisch verschlüsseln vor DB-Insert (data-at-rest-Schutz)."""
    return _totp_fernet().encrypt(secret.encode("utf-8")).decode("utf-8")


def decrypt_totp_secret(encrypted: str) -> Optional[str]:
    """Decrypt — None wenn Token korrupt oder JWT_SECRET geändert wurde."""
    try:
        return _totp_fernet().decrypt(encrypted.encode("utf-8")).decode("utf-8")
    except (InvalidToken, ValueError):
        return None


def totp_provisioning_uri(secret: str, username: str) -> str:
    """`otpauth://`-URI für QR-Code, lesbar von Authenticator-Apps."""
    return pyotp.totp.TOTP(secret).provisioning_uri(
        name=username,
        issuer_name=config.TOTP_ISSUER,
    )


def verify_totp_code(secret: str, code: str) -> bool:
    """6-stelligen Code gegen das Secret prüfen. ±1 Step Tolerance (30s vor/zurück)
    fängt Server-Clock-Drift bei mobilen Authenticators."""
    if not code or not secret:
        return False
    code = code.strip().replace(" ", "")
    if not code.isdigit() or len(code) != 6:
        return False
    return pyotp.TOTP(secret).verify(code, valid_window=1)


# ─────────────────────────────────────────────────────────────────────
# User-CRUD
# ─────────────────────────────────────────────────────────────────────


def create_user(
    username: str,
    password: str,
    is_admin: bool = False,
) -> Dict[str, Any]:
    """Insert. Wirft ValueError bei Duplicate-Username (UNIQUE constraint)."""
    if not username.strip() or len(username) > 64:
        raise ValueError("invalid username")
    if len(password) < 8:
        raise ValueError("password too short (min 8 chars)")
    pw_hash = hash_password(password)
    conn = _db()
    try:
        cur = conn.execute(
            "INSERT INTO users (username, password_hash, is_admin, created_at_ms) "
            "VALUES (?, ?, ?, ?)",
            (username.strip(), pw_hash, 1 if is_admin else 0, _now_ms()),
        )
        conn.commit()
        return get_user_by_id(cur.lastrowid) or {}
    except Exception as e:
        if "UNIQUE" in str(e):
            raise ValueError("username already exists") from e
        raise
    finally:
        conn.close()


def get_user_by_id(user_id: int) -> Optional[Dict[str, Any]]:
    conn = _db()
    try:
        row = conn.execute(
            "SELECT * FROM users WHERE id = ?", (user_id,)
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def get_user_by_username(username: str) -> Optional[Dict[str, Any]]:
    conn = _db()
    try:
        row = conn.execute(
            "SELECT * FROM users WHERE username = ?", (username.strip(),)
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def list_users() -> List[Dict[str, Any]]:
    conn = _db()
    try:
        rows = conn.execute(
            "SELECT id, username, is_admin, created_at_ms, last_login_at_ms, "
            "       (totp_secret IS NOT NULL) AS totp_enabled "
            "FROM users ORDER BY id ASC"
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def update_user_password(user_id: int, new_password: str) -> bool:
    if len(new_password) < 8:
        raise ValueError("password too short")
    conn = _db()
    try:
        cur = conn.execute(
            "UPDATE users SET password_hash = ? WHERE id = ?",
            (hash_password(new_password), user_id),
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def set_user_admin(user_id: int, is_admin: bool) -> bool:
    """Toggle is_admin-Flag. Caller (Endpoint) verantwortet den Last-Admin-
    Schutz — diese Funktion macht keinen Count-Check, weil sie auch
    intern für die initiale create_user(is_admin=True)-Pfade benutzt
    werden könnte."""
    conn = _db()
    try:
        cur = conn.execute(
            "UPDATE users SET is_admin = ? WHERE id = ?",
            (1 if is_admin else 0, user_id),
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def delete_user(user_id: int) -> bool:
    """Hard-Delete. Cascades manually (SQLite-FK ist nicht zwingend enforced):
    erst PATs + refresh_tokens des Users löschen, dann den User selbst.
    Caller verantwortet Last-Admin-Schutz und Self-Delete-Verbot."""
    conn = _db()
    try:
        conn.execute("DELETE FROM pats WHERE user_id = ?", (user_id,))
        conn.execute("DELETE FROM refresh_tokens WHERE user_id = ?", (user_id,))
        cur = conn.execute("DELETE FROM users WHERE id = ?", (user_id,))
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def set_totp_secret(user_id: int, plain_secret: str) -> None:
    """Aktiviert TOTP für den User. Speichert das Secret verschlüsselt."""
    enc = encrypt_totp_secret(plain_secret)
    conn = _db()
    try:
        conn.execute("UPDATE users SET totp_secret = ? WHERE id = ?", (enc, user_id))
        conn.commit()
    finally:
        conn.close()


def disable_totp(user_id: int) -> None:
    conn = _db()
    try:
        conn.execute("UPDATE users SET totp_secret = NULL WHERE id = ?", (user_id,))
        conn.commit()
    finally:
        conn.close()


def get_user_totp_secret(user_id: int) -> Optional[str]:
    """Decrypted TOTP secret oder None wenn der User keins hat / Decrypt failt."""
    conn = _db()
    try:
        row = conn.execute(
            "SELECT totp_secret FROM users WHERE id = ?", (user_id,)
        ).fetchone()
    finally:
        conn.close()
    if not row or not row["totp_secret"]:
        return None
    return decrypt_totp_secret(row["totp_secret"])


def touch_last_login(user_id: int) -> None:
    conn = _db()
    try:
        conn.execute(
            "UPDATE users SET last_login_at_ms = ? WHERE id = ?", (_now_ms(), user_id)
        )
        conn.commit()
    finally:
        conn.close()


def admin_count() -> int:
    """Wie viele Admins gibt's. Schützt vor "letzten Admin demoten/löschen"."""
    conn = _db()
    try:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM users WHERE is_admin = 1"
        ).fetchone()
        return int(row["n"]) if row else 0
    finally:
        conn.close()


def setup_required() -> bool:
    """True wenn noch kein User existiert — Setup-Flow zeigen."""
    conn = _db()
    try:
        row = conn.execute("SELECT COUNT(*) AS n FROM users").fetchone()
        return int(row["n"]) == 0 if row else True
    finally:
        conn.close()


# ─────────────────────────────────────────────────────────────────────
# PATs (Personal Access Tokens)
# ─────────────────────────────────────────────────────────────────────

PAT_PREFIX = "tonus_pat_"


def issue_pat(
    user_id: int,
    name: str,
    scopes: Optional[List[str]] = None,
    expires_at_ms: Optional[int] = None,
) -> Dict[str, Any]:
    """Generiere einen PAT. Returns dict mit Plain-Token (NUR jetzt verfügbar) +
    Metadata. Caller muss `token` dem User zeigen und es ihm überlassen, das
    in seine Plugin-Config zu kopieren."""
    if not name.strip() or len(name) > 64:
        raise ValueError("invalid PAT name")
    raw = secrets.token_urlsafe(32)
    plain = f"{PAT_PREFIX}{raw}"
    token_hash = hashlib.sha256(plain.encode("utf-8")).hexdigest()
    prefix_visible = plain[: len(PAT_PREFIX) + 6]  # tonus_pat_aB12cd
    scopes_json = ",".join(scopes) if scopes else None
    conn = _db()
    try:
        cur = conn.execute(
            "INSERT INTO pats (user_id, name, prefix, token_hash, scopes, "
            "                  created_at_ms, expires_at_ms) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (user_id, name.strip(), prefix_visible, token_hash, scopes_json,
             _now_ms(), expires_at_ms),
        )
        conn.commit()
        pat_id = cur.lastrowid
    finally:
        conn.close()
    return {
        "id": pat_id,
        "name": name.strip(),
        "prefix": prefix_visible,
        "token": plain,  # ← shown ONCE
        "expires_at_ms": expires_at_ms,
    }


def verify_pat(plain_token: str) -> Optional[Dict[str, Any]]:
    """Lookup PAT by plain-token-string. Returns user-dict + pat-id if valid,
    None if not. Touch last_used_at als Side-Effect."""
    if not plain_token or not plain_token.startswith(PAT_PREFIX):
        return None
    token_hash = hashlib.sha256(plain_token.encode("utf-8")).hexdigest()
    conn = _db()
    try:
        row = conn.execute(
            "SELECT p.id AS pat_id, p.user_id, p.name, p.scopes, p.expires_at_ms, "
            "       u.username, u.is_admin "
            "FROM pats p JOIN users u ON u.id = p.user_id "
            "WHERE p.token_hash = ?",
            (token_hash,),
        ).fetchone()
        if not row:
            return None
        # Expiry
        if row["expires_at_ms"] and row["expires_at_ms"] < _now_ms():
            return None
        # Touch last_used
        conn.execute(
            "UPDATE pats SET last_used_at_ms = ? WHERE id = ?",
            (_now_ms(), row["pat_id"]),
        )
        conn.commit()
        return dict(row)
    finally:
        conn.close()


def list_pats(user_id: int) -> List[Dict[str, Any]]:
    conn = _db()
    try:
        rows = conn.execute(
            "SELECT id, name, prefix, scopes, created_at_ms, last_used_at_ms, "
            "       expires_at_ms FROM pats WHERE user_id = ? ORDER BY created_at_ms DESC",
            (user_id,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def revoke_pat(pat_id: int, user_id: int) -> bool:
    """User kann nur seine eigenen PATs revokieren. Returns True wenn gelöscht."""
    conn = _db()
    try:
        cur = conn.execute(
            "DELETE FROM pats WHERE id = ? AND user_id = ?",
            (pat_id, user_id),
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


# ─────────────────────────────────────────────────────────────────────
# Login-Rate-Limit
# ─────────────────────────────────────────────────────────────────────


def record_login_attempt(username: str, source_ip: Optional[str], success: bool) -> None:
    conn = _db()
    try:
        conn.execute(
            "INSERT INTO login_attempts (username, source_ip, attempted_at_ms, success) "
            "VALUES (?, ?, ?, ?)",
            (username.strip(), source_ip, _now_ms(), 1 if success else 0),
        )
        # Cleanup: alles älter als 24h löschen damit die Tabelle nicht wächst.
        cutoff = _now_ms() - 24 * 60 * 60 * 1000
        conn.execute("DELETE FROM login_attempts WHERE attempted_at_ms < ?", (cutoff,))
        conn.commit()
    finally:
        conn.close()

    # Auto-Ban-Trigger: nach Insert prüfen ob die IP 5+ Failed-Logins in den
    # letzten 24h hat. Lifetime-Ban — Admin muss manuell unbannen.
    # Loopback ist immun (Container-internal-Calls dürfen nie sperren).
    if not success and source_ip and not _is_loopback_ip(source_ip):
        recent_fails = _count_failed_logins_for_ip(source_ip, window_ms=24 * 60 * 60 * 1000)
        if recent_fails >= 5:
            ban_ip(
                source_ip,
                reason=f"Auto-banned after {recent_fails} failed logins in 24h",
                failed_count=recent_fails,
            )


def is_rate_limited(username: str) -> bool:
    """True wenn der User in den letzten 15 min config.LOGIN_RATE_LIMIT_PER_15MIN
    failed-login-attempts hatte. Erfolgreiche Logins zählen NICHT mit (User
    sollte sich anmelden können wenn er mehrfach erfolgreich war)."""
    cutoff = _now_ms() - 15 * 60 * 1000
    conn = _db()
    try:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM login_attempts "
            "WHERE username = ? AND success = 0 AND attempted_at_ms >= ?",
            (username.strip(), cutoff),
        ).fetchone()
    finally:
        conn.close()
    return int(row["n"]) >= config.LOGIN_RATE_LIMIT_PER_15MIN if row else False


# ─────────────────────────────────────────────────────────────────────
# IP-Banning (Brute-Force-Defense, Lifetime)
# ─────────────────────────────────────────────────────────────────────

def _is_loopback_ip(ip: str) -> bool:
    """Loopback-Adressen werden nie automatisch gebannt — Container-internal-
    Calls (Worker-Self-Calls, Health-Checks) sollen nicht zum Self-Lockout
    führen. Admin kann sie aber manuell bannen wenn er das wirklich will
    (würde aber den Container quasi un-administrierbar machen)."""
    if not ip:
        return False
    return ip in ("127.0.0.1", "::1", "localhost", "0.0.0.0")


def _count_failed_logins_for_ip(ip: str, window_ms: int) -> int:
    """Anzahl der Fail-Login-Attempts pro IP innerhalb des Fensters.
    Nutzt die existing login_attempts-Tabelle ohne extra Index — das
    Volumen ist klein (24h-rolling, nur Login-Endpoint)."""
    cutoff = _now_ms() - window_ms
    conn = _db()
    try:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM login_attempts "
            "WHERE source_ip = ? AND success = 0 AND attempted_at_ms >= ?",
            (ip, cutoff),
        ).fetchone()
    finally:
        conn.close()
    return int(row["n"]) if row else 0


def is_ip_banned(ip: Optional[str]) -> bool:
    """Pre-Auth-Check. Loopback ist nie gebannt (siehe _is_loopback_ip)."""
    if not ip or _is_loopback_ip(ip):
        return False
    conn = _db()
    try:
        row = conn.execute(
            "SELECT 1 FROM banned_ips WHERE ip = ? LIMIT 1", (ip,)
        ).fetchone()
    finally:
        conn.close()
    return row is not None


def ban_ip(ip: str, reason: str = "manual", failed_count: int = 0) -> None:
    """Idempotent — INSERT OR IGNORE, weil PRIMARY KEY = ip. Re-Banning
    derselben IP überschreibt NICHT (banned_at_ms bleibt erste Erkennung)."""
    if _is_loopback_ip(ip):
        return  # safety-net: Loopback nie bannen, auch nicht auf direkten Aufruf
    conn = _db()
    try:
        conn.execute(
            "INSERT OR IGNORE INTO banned_ips (ip, reason, banned_at_ms, failed_count) "
            "VALUES (?, ?, ?, ?)",
            (ip, reason, _now_ms(), failed_count),
        )
        conn.commit()
    finally:
        conn.close()


def list_banned_ips() -> List[Dict[str, Any]]:
    """Alle Bans, neueste zuerst. Admin-only-Endpoint nutzt das."""
    conn = _db()
    try:
        rows = conn.execute(
            "SELECT ip, reason, banned_at_ms, failed_count FROM banned_ips "
            "ORDER BY banned_at_ms DESC"
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def unban_ip(ip: str) -> bool:
    """Returns True wenn ein Ban gelöscht wurde, False wenn IP nicht gebannt war."""
    conn = _db()
    try:
        cur = conn.execute("DELETE FROM banned_ips WHERE ip = ?", (ip,))
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()
