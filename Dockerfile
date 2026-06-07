# syntax=docker/dockerfile:1.6

# ── Stage 1: Frontend-Build ────────────────────────────────────────
# --platform=$BUILDPLATFORM pinnt den FE-Build auf die NATIVE Builder-Arch
# (amd64 auf GitHub-Runner), egal für welche TARGETPLATFORM gebaut wird.
# Output (/fe/build) ist statisch (adapter-static, HTML/JS/CSS) und damit
# arch-unabhängig — wird unten in beide Runtime-Images kopiert.
# KRITISCH seit vite 8 (v0.4.10): vite nutzt rolldown (Rust-Bundler), dessen
# natives Binary unter QEMU-arm64-Emulation hängt → multi-arch-Build lief
# >25min ohne Ende. Mit BUILDPLATFORM läuft der FE-Build nie mehr emuliert.
FROM --platform=$BUILDPLATFORM node:20-alpine AS fe
WORKDIR /fe
COPY frontend/package.json frontend/package-lock.json* ./
RUN npm ci --no-audit --no-fund
COPY frontend/ ./
ENV BASE_PATH=""
RUN npm run build

# ── Stage 2: Backend-Runtime ───────────────────────────────────────
FROM python:3.11-slim AS runtime

# ffmpeg in debian:trixie hat libgbm1/libgl1-mesa-dri/libglx-mesa0/mesa-
# libgallium als hard-deps — Versuch sie zu purgen würde ffmpeg mit-killen.
# Daher bleiben die 4 Mesa-CRITICAL-CVEs (CVE-2026-40393, will_not_fix
# upstream) Teil des Images. Mitigation: Mesa wird im Backend nirgends
# aufgerufen (kein OpenGL/VAAPI), also ist die Vuln nicht im Code-Pfad.
# Bei Wunsch nach kleinerer Attack-Surface wäre ein eigener ffmpeg-Build
# ohne GPU-deps oder Wechsel zu Alpine die nächste Eskalations-Stufe.
RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg ca-certificates \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY backend/requirements.txt ./backend/requirements.txt
RUN pip install --no-cache-dir -r backend/requirements.txt

COPY backend/ ./backend/
COPY --from=fe /fe/build ./backend/frontend/build
RUN mkdir -p /app/downloads/temp /app/data

# Bytecode-Cache-Schreiben deaktivieren — erlaubt später read_only:true in
# Compose ohne extra tmpfs für __pycache__ definieren zu müssen
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/app \
    API_HOST=0.0.0.0 \
    API_PORT=8088

# H-1 Audit 2026-05-12: Non-root Runtime-User (UID/GID 1000:1000).
# Existing Bind-Mounts auf dem Host müssen auf UID 1000 chown'd sein,
# sonst kann Tonus nicht in /app/data oder /app/downloads schreiben:
#   sudo chown -R 1000:1000 <host-path-für-docker_data/tonus>
# Auch der Navidrome-Music-Path braucht r/w für UID 1000.
# Login-shell ist /usr/sbin/nologin (defense-in-depth gegen exec-Pfade),
# Home-Dir wird NICHT angelegt (-M, kleinere Surface).
RUN groupadd -g 1000 tonus \
 && useradd -u 1000 -g 1000 -M -s /usr/sbin/nologin tonus \
 && chown -R 1000:1000 /app
USER 1000:1000

EXPOSE 8088
WORKDIR /app/backend
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8088"]
