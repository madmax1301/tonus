"""In-Memory Rate-Limiter als FastAPI-Dependency (Audit M-1, 2026-05-12).

Sliding-Window-Counter pro (client_ip, route) — verhindert DoS-Bursts und
Quota-Burn auf den Bulk-Endpoints (CSV-Import, Spotify-History-Import, URL-
Download, Track-Download). Nutzt die ``utils.auth.client_ip``-Helper mit dem
H-7 trusted-proxy-Check, damit Bans nicht auf der Reverse-Proxy-IP collapsieren
(slowapi's eingebauter ``get_remote_address`` würde das tun).

Design-Notes
------------
* **In-Memory, kein Redis** — Tonus läuft typisch als Single-Container; eine
  Process-Restart leert den State, was bei einem Patch-Deploy gewünscht ist
  ("frischer Limit-Counter nach Restart, kein hängender Ban").
* **deque mit pop-from-left** — O(1) für append + O(k) für Expiry-Cleanup,
  k = Anzahl entfernter alter Einträge. Klassisches Sliding-Window-Pattern.
* **Bounded Memory** — wenn ein Client nichts mehr macht, leert sich seine
  Deque automatisch beim nächsten Request derselben IP+Route, weil alle
  Timestamps älter als Window sind und gepopt werden.
* **Per-Route-Key** — selbe IP kann gleichzeitig nahe am CSV-Import-Limit
  UND am URL-Download-Limit sein, ohne sich gegenseitig zu blockieren.
"""
from __future__ import annotations

import time
from collections import defaultdict, deque
from typing import Callable, Deque, Dict

from fastapi import HTTPException, Request, status


_rate_limit_state: Dict[str, Deque[float]] = defaultdict(deque)


def make_rate_limiter(max_calls: int, window_seconds: int) -> Callable[[Request], None]:
    """Erzeugt eine FastAPI-Dependency die bei Limit-Exceed 429 throwt.

    Args:
        max_calls: Maximale Calls pro Window.
        window_seconds: Window-Größe in Sekunden.

    Returns:
        Dependency-Funktion, mit ``Depends(...)`` einsetzbar.

    Beispiel:
        >>> _limit_csv = make_rate_limiter(20, 3600)
        >>> @app.post("/api/import/csv")
        ... async def import_csv(req: CsvImportRequest, _: None = Depends(_limit_csv)):
        ...     ...
    """
    from utils.auth import client_ip  # lazy import — vermeidet Circular

    def dep(request: Request) -> None:
        ip = client_ip(request) or "unknown"
        key = f"{ip}:{request.url.path}"
        now = time.time()
        window = _rate_limit_state[key]
        # Alte Timestamps aus dem Window-Tail wegpoppen (sliding cleanup)
        cutoff = now - window_seconds
        while window and window[0] < cutoff:
            window.popleft()
        if len(window) >= max_calls:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail=f"Rate limit exceeded: {max_calls} requests / {window_seconds}s",
                headers={"Retry-After": str(window_seconds)},
            )
        window.append(now)

    return dep
