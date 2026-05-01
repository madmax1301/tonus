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
RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg ca-certificates \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY backend/requirements.txt ./backend/requirements.txt
RUN pip install --no-cache-dir -r backend/requirements.txt

COPY backend/ ./backend/
COPY --from=fe /fe/build ./backend/frontend/build
RUN mkdir -p /app/downloads/temp

ENV PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app \
    API_HOST=0.0.0.0 \
    API_PORT=8088

EXPOSE 8088
WORKDIR /app/backend
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8088"]
