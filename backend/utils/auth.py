"""Optionaler Bearer-Token-Schutz für Mutate-Endpoints.

Verhalten:
- Wenn ``config.TONUS_API_TOKEN`` leer: Dependency ist ein No-Op (offen wie heute).
- Wenn gesetzt: ``Authorization: Bearer <token>`` Header wird vergleichsfest geprüft.
- Auth-Failures geben 401 mit ``WWW-Authenticate: Bearer`` Header zurück.

Keine Logging des Tokens (auch nicht bei Failure) — Default für Secrets.
"""
from __future__ import annotations

import secrets
from typing import Optional

from fastapi import Header, HTTPException, status

import config


def require_token(authorization: Optional[str] = Header(default=None)) -> None:
    """FastAPI-Dependency. Per ``Depends(require_token)`` an Mutate-Routen anhängen."""
    expected = config.TONUS_API_TOKEN
    if not expected:
        return  # Auth disabled.

    # Erwartetes Format: "Bearer <token>". Wir lesen großzügig: leerer Header oder
    # falsches Schema → 401, ohne Hinweis was schief lief.
    provided: Optional[str] = None
    if authorization:
        parts = authorization.strip().split(None, 1)
        if len(parts) == 2 and parts[0].lower() == "bearer":
            provided = parts[1].strip()

    if not provided or not secrets.compare_digest(provided, expected):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing API token",
            headers={"WWW-Authenticate": "Bearer"},
        )


def auth_required() -> bool:
    """Helper für Health/Plugin-Endpoints, die im Body anzeigen wollen, ob das
    Backend einen Token erzwingt. Kein FastAPI-Dependency."""
    return bool(config.TONUS_API_TOKEN)
