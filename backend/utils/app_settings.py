"""App-Settings-Store — Key/Value-Persistenz für UI-editierbare Konfiguration.

Provider-Credentials (Spotify, Navidrome, ...) leben hier statt in
backend/.env, damit der Operator sie über das Tonus-UI ändern kann ohne
Container-Restart-Tooling.

Lese-Pfad: config.py liest beim Boot env-Defaults und ruft danach
``apply_db_overrides()`` auf, das DB-Werte über die Module-Globals
patcht. Services (DeezerService, SpotifyService, NavidromeService) lesen
``config.X``-Globals beim Constructor und sehen damit die DB-Werte.

Schreib-Pfad: Settings → Verbindungen-UI ruft Endpoints auf, die
``set_setting()`` direkt nutzen. Änderungen werden erst beim nächsten
Container-Restart wirksam — kein Hot-Reload (würde laufenden Worker
beeinträchtigen).

Encryption: Felder die als secret markiert werden (Passwords, Client-
Secrets, ARL-Cookies) werden mit dem JWT_SECRET-derived Fernet-Key
verschlüsselt. Bei JWT-Secret-Reset müssen die Werte neu eingegeben
werden — explizites Tradeoff vs. zweites Secret zu managen.
"""
from __future__ import annotations

import base64
import hashlib
from typing import Optional, Dict, Any, List

from cryptography.fernet import Fernet, InvalidToken

from utils.job_store import _db, _now_ms


def _fernet() -> Fernet:
    """Symmetrischer Fernet-Key aus JWT-Secret abgeleitet — selber Key wie
    bei TOTP-Secrets in auth_users.py. Ein Reset des JWT-Secrets
    invalidiert alle verschlüsselten App-Settings."""
    from utils.auth_users import get_or_init_jwt_secret

    digest = hashlib.sha256(get_or_init_jwt_secret().encode("utf-8")).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def get_setting(key: str, default: Optional[str] = None) -> Optional[str]:
    """Lese App-Setting. Wenn encrypted=1, wird der Wert vor Rückgabe
    entschlüsselt. Bei Decryption-Failure (z.B. nach JWT-Secret-Rotation)
    wird ``default`` zurückgegeben — der Operator muss den Wert in der UI
    neu eingeben."""
    conn = _db()
    try:
        row = conn.execute(
            "SELECT value, encrypted FROM app_settings WHERE key = ?", (key,)
        ).fetchone()
    finally:
        conn.close()
    if not row:
        return default
    raw = row["value"]
    if not row["encrypted"]:
        return raw
    try:
        return _fernet().decrypt(raw.encode("utf-8")).decode("utf-8")
    except (InvalidToken, ValueError):
        return default


def set_setting(key: str, value: str, *, encrypted: bool = False) -> None:
    """UPSERT. Bei encrypted=True wird value Fernet-verschlüsselt
    persistiert. Leere Strings werden als ``None`` behandelt — siehe
    ``delete_setting``-Pfad im Endpoint, wenn der Operator ein Feld
    leeren will."""
    stored = (
        _fernet().encrypt(value.encode("utf-8")).decode("utf-8")
        if encrypted
        else value
    )
    conn = _db()
    try:
        conn.execute(
            "INSERT INTO app_settings (key, value, encrypted, updated_at_ms) "
            "VALUES (?, ?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET "
            "    value = excluded.value, "
            "    encrypted = excluded.encrypted, "
            "    updated_at_ms = excluded.updated_at_ms",
            (key, stored, 1 if encrypted else 0, _now_ms()),
        )
        conn.commit()
    finally:
        conn.close()


def delete_setting(key: str) -> bool:
    """Hard-Delete. Returns True wenn ein Eintrag gelöscht wurde."""
    conn = _db()
    try:
        cur = conn.execute("DELETE FROM app_settings WHERE key = ?", (key,))
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def list_settings(prefix: Optional[str] = None) -> List[Dict[str, Any]]:
    """Listet alle Settings (oder alle mit Prefix). encrypted-Flag wird
    mitgeliefert — Caller (z.B. Provider-Endpoints) entscheidet ob er den
    Klartext entschlüsselt zeigt oder nur eine masked Version (`****`)."""
    conn = _db()
    try:
        if prefix:
            rows = conn.execute(
                "SELECT key, value, encrypted, updated_at_ms FROM app_settings "
                "WHERE key LIKE ? ORDER BY key",
                (f"{prefix}%",),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT key, value, encrypted, updated_at_ms FROM app_settings "
                "ORDER BY key"
            ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()
