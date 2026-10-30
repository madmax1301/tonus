"""Snapshots der App-Datenbank (jobs.db).

jobs.db hält alles, was Tonus nicht neu aufbauen kann: Nutzer, 2FA, PATs,
das JWT-Secret (aus dem auch der Fernet-Key für gespeicherte Provider-
Credentials abgeleitet wird), Settings und die Queue. Ein Snapshot läuft
über die SQLite-Online-Backup-API — konsistent auch bei aktivem WAL und
laufenden Workern, ohne den Server anzuhalten.

Zwei Arten:
  * "startup" — bei jedem Container-Start VOR init_jobs_db(), also vor
    jeder Schema-Migration. Wer `:dev` pullt, hat damit immer den Stand
    vor dem Update.
  * "daily"   — alle BACKUP_INTERVAL_H Stunden (0 = aus).
  * "manual"  — per Settings → Backend → „Jetzt sichern“.
Jede Art behält ihre letzten BACKUP_KEEP Dateien, damit häufige Restarts
die täglichen Snapshots nicht verdrängen.

Die Snapshots liegen im selben Volume wie jobs.db. Gegen einen Fehler beim
Update oder eine kaputte DB hilft das; gegen einen Plattenausfall nur, wenn
der Ordner mitgesichert wird (NAS-Backup von tonus-data/).
"""

from __future__ import annotations

import os
import re
import sqlite3
import threading
from datetime import datetime
from typing import Any, Dict, List, Optional

from utils.job_store import JOBS_DB_PATH

BACKUP_DIR = os.getenv("BACKUP_DIR") or os.path.join(os.path.dirname(JOBS_DB_PATH) or ".", "backups")
BACKUP_KEEP = max(1, int(os.getenv("BACKUP_KEEP", "7")))
BACKUP_INTERVAL_H = max(0.0, float(os.getenv("BACKUP_INTERVAL_H", "24")))

KINDS = ("startup", "daily", "manual")
# jobs-20261030-221500-startup.db — fester Aufbau, damit Download-Namen
# aus der URL ohne Path-Traversal-Risiko validiert werden können.
_NAME_RE = re.compile(r"^jobs-\d{8}-\d{6}(?:-\d+)?-(startup|daily|manual)\.db$")

_lock = threading.Lock()


def is_valid_name(name: str) -> bool:
    return bool(_NAME_RE.match(name or ""))


def snapshot(kind: str) -> Optional[str]:
    """Schreibt einen Snapshot und räumt alte derselben Art weg. Gibt den
    Dateinamen zurück, oder None wenn es (noch) keine jobs.db gibt."""
    if kind not in KINDS:
        raise ValueError(f"unknown backup kind: {kind}")
    if not os.path.isfile(JOBS_DB_PATH):
        return None
    with _lock:
        os.makedirs(BACKUP_DIR, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        name = f"jobs-{stamp}-{kind}.db"
        n = 1
        while os.path.exists(os.path.join(BACKUP_DIR, name)):
            n += 1
            name = f"jobs-{stamp}-{n}-{kind}.db"
        final = os.path.join(BACKUP_DIR, name)
        tmp = final + ".tmp"
        src = sqlite3.connect(JOBS_DB_PATH, timeout=30)
        try:
            dst = sqlite3.connect(tmp)
            try:
                src.backup(dst)
            finally:
                dst.close()
        finally:
            src.close()
        # Halbe Dateien sollen nie als Backup auftauchen.
        os.replace(tmp, final)
        _prune(kind)
        return name


def _prune(kind: str) -> None:
    mine = sorted(b["name"] for b in list_backups() if b["kind"] == kind)
    for name in mine[:-BACKUP_KEEP]:
        try:
            os.remove(os.path.join(BACKUP_DIR, name))
        except OSError:
            pass


def list_backups() -> List[Dict[str, Any]]:
    """Neueste zuerst."""
    if not os.path.isdir(BACKUP_DIR):
        return []
    out = []
    for name in os.listdir(BACKUP_DIR):
        m = _NAME_RE.match(name)
        if not m:
            continue
        st = os.stat(os.path.join(BACKUP_DIR, name))
        out.append({
            "name": name,
            "kind": m.group(1),
            "size_bytes": st.st_size,
            "created_at_ms": int(st.st_mtime * 1000),
        })
    out.sort(key=lambda b: (b["created_at_ms"], b["name"]), reverse=True)
    return out


def path_for(name: str) -> Optional[str]:
    if not is_valid_name(name):
        return None
    p = os.path.join(BACKUP_DIR, name)
    return p if os.path.isfile(p) else None


def start_scheduler(stop_event: threading.Event) -> Optional[threading.Thread]:
    """Täglicher Snapshot als Daemon-Thread. Fehler werden geloggt, nie
    geworfen — ein Backup-Problem darf den Server nicht stoppen."""
    if BACKUP_INTERVAL_H <= 0:
        return None
    interval_s = BACKUP_INTERVAL_H * 3600

    def _loop() -> None:
        while not stop_event.wait(timeout=interval_s):
            try:
                name = snapshot("daily")
                if name:
                    print(f"[backup] {name}", flush=True)
            except Exception as e:
                print(f"[backup] daily snapshot failed: {type(e).__name__}: {e}", flush=True)

    t = threading.Thread(target=_loop, name="backup-daily", daemon=True)
    t.start()
    return t
