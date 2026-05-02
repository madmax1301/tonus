"""Authentication store — Multi-User + 2FA + Personal Access Tokens.

Schema lebt in derselben SQLite-DB wie download_jobs (Pfad: ``utils/job_store.JOBS_DB_PATH``).
Drei Tabellen:

* ``users`` — username, password_hash (argon2id), totp_secret + totp_enabled,
  is_admin, created_at_ms, last_login_at_ms.
* ``pats`` — Personal Access Tokens für Plugin/CLI. token_hash (sha256),
  prefix, name, user_id, created_at_ms, last_used_at_ms, revoked_at_ms.
* ``refresh_tokens`` — JWT-Refresh-Token-Tracking für Logout/Revocation.
  jti (UUID), user_id, expires_at_ms, revoked_at_ms.
* ``auth_meta`` — Singleton key/value für JWT_SECRET und Migration-Markers.
* ``login_attempts`` — In-Memory wäre einfacher, aber Container-Restart-resilient
  ist DB-basiert besser. Pro IP+username, mit Cleanup-Window.

Alles in WAL-Mode, kompatibel mit den bestehenden Job-Tabellen.
"""
from __future__ import annotations

import hashlib
import secrets
import sqlite3
import time
from typing import Any, Dict, List, Optional

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError

from utils.job_store import _db, _now_ms

_PH = PasswordHasher()  # argon2id with library defaults — strong + fast


# ─── Schema ────────────────────────────────────────────────────────────

def init_auth_db() -> None:
    """Idempotente Schema-Initialisierung. Wird einmal beim Server-Start aus app.py aufgerufen."""
    conn = _db()
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL UNIQUE,
                password_hash TEXT NOT NULL,
                totp_secret TEXT,
                totp_enabled INTEGER NOT NULL DEFAULT 0,
                is_admin INTEGER NOT NULL DEFAULT 0,
                created_at_ms INTEGER NOT NULL,
                last_login_at_ms INTEGER,
                disabled_at_ms INTEGER
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_users_username ON users(username)")

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS pats (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                name TEXT NOT NULL,
                prefix TEXT NOT NULL,
                token_hash TEXT NOT NULL,
                created_at_ms INTEGER NOT NULL,
                last_used_at_ms INTEGER,
                revoked_at_ms INTEGER,
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_pats_user ON pats(user_id)")
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_pats_hash ON pats(token_hash)")

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS refresh_tokens (
                jti TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL,
                expires_at_ms INTEGER NOT NULL,
                created_at_ms INTEGER NOT NULL,
                revoked_at_ms INTEGER,
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_refresh_user ON refresh_tokens(user_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_refresh_exp ON refresh_tokens(expires_at_ms)")

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS auth_meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at_ms INTEGER NOT NULL
            )
            """
        )

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS login_attempts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ip TEXT NOT NULL,
                username TEXT,
                success INTEGER NOT NULL,
                attempt_at_ms INTEGER NOT NULL
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_login_attempts_ip ON login_attempts(ip, attempt_at_ms)")

        conn.commit()
    finally:
        conn.close()


# ─── Meta (JWT-Secret etc.) ─────────────────────────────────────────────

def get_or_create_jwt_secret() -> str:
    """Liest das JWT-Secret aus auth_meta. Generiert ein neues 64-byte-secret beim ersten Aufruf."""
    conn = _db()
    try:
        row = conn.execute("SELECT value FROM auth_meta WHERE key = ?", ("jwt_secret",)).fetchone()
        if row:
            return row["value"]
        new_secret = secrets.token_urlsafe(64)
        conn.execute(
            "INSERT INTO auth_meta(key, value, updated_at_ms) VALUES(?, ?, ?)",
            ("jwt_secret", new_secret, _now_ms()),
        )
        conn.commit()
        return new_secret
    finally:
        conn.close()


# ─── User-CRUD ─────────────────────────────────────────────────────────

def hash_password(plain: str) -> str:
    return _PH.hash(plain)


def verify_password(stored_hash: str, plain: str) -> bool:
    try:
        _PH.verify(stored_hash, plain)
        return True
    except VerifyMismatchError:
        return False
    except Exception:
        return False


def needs_rehash(stored_hash: str) -> bool:
    try:
        return _PH.check_needs_rehash(stored_hash)
    except Exception:
        return False


def count_users() -> int:
    conn = _db()
    try:
        row = conn.execute("SELECT COUNT(*) AS c FROM users WHERE disabled_at_ms IS NULL").fetchone()
        return int(row["c"])
    finally:
        conn.close()


def setup_required() -> bool:
    """First-Run-Indikator: True wenn keine User existieren."""
    return count_users() == 0


def create_user(
    username: str,
    password: str,
    is_admin: bool = False,
    totp_secret: Optional[str] = None,
    totp_enabled: bool = False,
) -> int:
    if not username or not password:
        raise ValueError("username and password required")
    conn = _db()
    try:
        cur = conn.execute(
            """
            INSERT INTO users(username, password_hash, totp_secret, totp_enabled, is_admin, created_at_ms)
            VALUES(?, ?, ?, ?, ?, ?)
            """,
            (
                username.strip(),
                hash_password(password),
                totp_secret,
                1 if totp_enabled else 0,
                1 if is_admin else 0,
                _now_ms(),
            ),
        )
        conn.commit()
        return int(cur.lastrowid)
    finally:
        conn.close()


def get_user(user_id: int) -> Optional[Dict[str, Any]]:
    conn = _db()
    try:
        row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def get_user_by_username(username: str) -> Optional[Dict[str, Any]]:
    conn = _db()
    try:
        row = conn.execute(
            "SELECT * FROM users WHERE username = ? AND disabled_at_ms IS NULL",
            (username.strip(),),
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def list_users() -> List[Dict[str, Any]]:
    conn = _db()
    try:
        rows = conn.execute(
            "SELECT id, username, totp_enabled, is_admin, created_at_ms, last_login_at_ms, disabled_at_ms "
            "FROM users ORDER BY created_at_ms ASC"
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def update_user_password(user_id: int, new_password: str) -> None:
    conn = _db()
    try:
        conn.execute(
            "UPDATE users SET password_hash = ? WHERE id = ?",
            (hash_password(new_password), user_id),
        )
        conn.commit()
    finally:
        conn.close()


def update_user_totp(user_id: int, secret: Optional[str], enabled: bool) -> None:
    conn = _db()
    try:
        conn.execute(
            "UPDATE users SET totp_secret = ?, totp_enabled = ? WHERE id = ?",
            (secret, 1 if enabled else 0, user_id),
        )
        conn.commit()
    finally:
        conn.close()


def mark_login(user_id: int) -> None:
    conn = _db()
    try:
        conn.execute("UPDATE users SET last_login_at_ms = ? WHERE id = ?", (_now_ms(), user_id))
        conn.commit()
    finally:
        conn.close()


def disable_user(user_id: int) -> None:
    conn = _db()
    try:
        conn.execute("UPDATE users SET disabled_at_ms = ? WHERE id = ?", (_now_ms(), user_id))
        # Revoke alle PATs + Refresh-Tokens
        conn.execute("UPDATE pats SET revoked_at_ms = ? WHERE user_id = ? AND revoked_at_ms IS NULL", (_now_ms(), user_id))
        conn.execute("UPDATE refresh_tokens SET revoked_at_ms = ? WHERE user_id = ? AND revoked_at_ms IS NULL", (_now_ms(), user_id))
        conn.commit()
    finally:
        conn.close()


# ─── PAT-CRUD ─────────────────────────────────────────────────────────

PAT_PREFIX = "tonus_pat_"


def _hash_pat(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def create_pat(user_id: int, name: str) -> Dict[str, Any]:
    """Erzeugt PAT, gibt Plaintext-Token + Metadaten zurück. Plaintext nur einmal sichtbar.

    Token-Format: ``tonus_pat_<48 random urlsafe>``
    """
    raw = secrets.token_urlsafe(36)  # ~48 chars
    full = f"{PAT_PREFIX}{raw}"
    prefix_visible = full[: len(PAT_PREFIX) + 6]  # zeigt z.B. "tonus_pat_aB3xZq" für Listing
    conn = _db()
    try:
        cur = conn.execute(
            """
            INSERT INTO pats(user_id, name, prefix, token_hash, created_at_ms)
            VALUES(?, ?, ?, ?, ?)
            """,
            (user_id, name.strip() or "Unbenannt", prefix_visible, _hash_pat(full), _now_ms()),
        )
        conn.commit()
        return {
            "id": int(cur.lastrowid),
            "name": name.strip() or "Unbenannt",
            "prefix": prefix_visible,
            "token": full,  # plaintext, nur in dieser Antwort enthalten
            "created_at_ms": _now_ms(),
        }
    finally:
        conn.close()


def find_pat_by_token(token: str) -> Optional[Dict[str, Any]]:
    if not token or not token.startswith(PAT_PREFIX):
        return None
    h = _hash_pat(token)
    conn = _db()
    try:
        row = conn.execute(
            "SELECT * FROM pats WHERE token_hash = ? AND revoked_at_ms IS NULL",
            (h,),
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def mark_pat_used(pat_id: int) -> None:
    conn = _db()
    try:
        conn.execute("UPDATE pats SET last_used_at_ms = ? WHERE id = ?", (_now_ms(), pat_id))
        conn.commit()
    finally:
        conn.close()


def list_pats(user_id: int) -> List[Dict[str, Any]]:
    conn = _db()
    try:
        rows = conn.execute(
            "SELECT id, name, prefix, created_at_ms, last_used_at_ms, revoked_at_ms "
            "FROM pats WHERE user_id = ? ORDER BY created_at_ms DESC",
            (user_id,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def revoke_pat(pat_id: int, user_id: int) -> bool:
    conn = _db()
    try:
        cur = conn.execute(
            "UPDATE pats SET revoked_at_ms = ? WHERE id = ? AND user_id = ? AND revoked_at_ms IS NULL",
            (_now_ms(), pat_id, user_id),
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


# ─── Refresh-Tokens ────────────────────────────────────────────────────

def store_refresh_token(jti: str, user_id: int, expires_at_ms: int) -> None:
    conn = _db()
    try:
        conn.execute(
            "INSERT INTO refresh_tokens(jti, user_id, expires_at_ms, created_at_ms) VALUES(?, ?, ?, ?)",
            (jti, user_id, expires_at_ms, _now_ms()),
        )
        conn.commit()
    finally:
        conn.close()


def is_refresh_token_valid(jti: str) -> bool:
    conn = _db()
    try:
        row = conn.execute(
            "SELECT expires_at_ms, revoked_at_ms FROM refresh_tokens WHERE jti = ?",
            (jti,),
        ).fetchone()
        if not row:
            return False
        if row["revoked_at_ms"] is not None:
            return False
        if row["expires_at_ms"] < _now_ms():
            return False
        return True
    finally:
        conn.close()


def revoke_refresh_token(jti: str) -> None:
    conn = _db()
    try:
        conn.execute(
            "UPDATE refresh_tokens SET revoked_at_ms = ? WHERE jti = ? AND revoked_at_ms IS NULL",
            (_now_ms(), jti),
        )
        conn.commit()
    finally:
        conn.close()


def revoke_all_refresh_tokens(user_id: int) -> None:
    conn = _db()
    try:
        conn.execute(
            "UPDATE refresh_tokens SET revoked_at_ms = ? WHERE user_id = ? AND revoked_at_ms IS NULL",
            (_now_ms(), user_id),
        )
        conn.commit()
    finally:
        conn.close()


def cleanup_expired_refresh_tokens() -> int:
    """Periodischer Cleanup. Wird beim Server-Start aufgerufen."""
    conn = _db()
    try:
        cur = conn.execute("DELETE FROM refresh_tokens WHERE expires_at_ms < ?", (_now_ms(),))
        conn.commit()
        return cur.rowcount
    finally:
        conn.close()


# ─── Login Rate-Limit ──────────────────────────────────────────────────

def record_login_attempt(ip: str, username: Optional[str], success: bool) -> None:
    conn = _db()
    try:
        conn.execute(
            "INSERT INTO login_attempts(ip, username, success, attempt_at_ms) VALUES(?, ?, ?, ?)",
            (ip or "?", username, 1 if success else 0, _now_ms()),
        )
        conn.commit()
    finally:
        conn.close()


def recent_failed_attempts(ip: str, window_ms: int = 15 * 60 * 1000) -> int:
    """Zählt failed Login-Versuche aus dieser IP in den letzten N ms."""
    threshold = _now_ms() - window_ms
    conn = _db()
    try:
        row = conn.execute(
            "SELECT COUNT(*) AS c FROM login_attempts WHERE ip = ? AND success = 0 AND attempt_at_ms >= ?",
            (ip or "?", threshold),
        ).fetchone()
        return int(row["c"])
    finally:
        conn.close()


def cleanup_old_login_attempts(retain_ms: int = 24 * 60 * 60 * 1000) -> int:
    threshold = _now_ms() - retain_ms
    conn = _db()
    try:
        cur = conn.execute("DELETE FROM login_attempts WHERE attempt_at_ms < ?", (threshold,))
        conn.commit()
        return cur.rowcount
    finally:
        conn.close()
