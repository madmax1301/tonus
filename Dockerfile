# syntax=docker/dockerfile:1.6

# ── Stage 1: Frontend-Build ────────────────────────────────────────
FROM node:20-alpine AS fe
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

EXPOSE 8088
WORKDIR /app/backend
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8088"]
