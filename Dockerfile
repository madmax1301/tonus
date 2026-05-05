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

# ffmpeg installieren, dann Mesa-GPU-Treiber wieder runterputzen — die ziehen
# CRITICAL CVE-2026-40393 ein und brauchen wir nicht (Audio-Conversion läuft
# pure CPU, kein VAAPI/CUDA). Wenn ein Mesa-Paket hard-dep von ffmpeg wäre,
# würde apt-get den purge verweigern und ffmpeg bliebe drin — der ffmpeg-
# version-Smoke-Test am Ende verifiziert dass das Binary noch funktioniert.
RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg ca-certificates \
 && apt-get remove -y --purge \
      libgbm1 \
      libgl1-mesa-dri \
      libglx-mesa0 \
      mesa-libgallium \
    || echo "WARN: Mesa-Pakete waren nicht entfernbar — ggf. hard-dep, manuell prüfen" \
 && apt-get autoremove -y --purge \
 && rm -rf /var/lib/apt/lists/* \
 && ffmpeg -version >/dev/null

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
