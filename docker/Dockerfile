# ============================================================
# Stage 1 — Python base
# ============================================================
FROM python:3.11-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

# curl for healthcheck, ca-certificates for HTTPS
RUN apt-get update && apt-get install -y --no-install-recommends \
        curl \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# ============================================================
# Stage 2 — Python dependencies + Playwright browser
# ============================================================
FROM base AS deps

COPY requirements.txt .
RUN pip install --upgrade pip && pip install -r requirements.txt

# Install Chromium and all required OS libraries in one step.
# --with-deps lets Playwright manage the exact system packages it needs,
# avoiding manual package name issues (e.g. libasound2 → libasound2t64 on Bookworm).
RUN playwright install chromium --with-deps

# ============================================================
# Stage 3 — API server
# ============================================================
FROM deps AS api

COPY backend/ ./backend/
COPY workers/ ./workers/

EXPOSE 8000

# --reload enables live-reload when source is mounted as a volume (dev mode).
CMD ["uvicorn", "backend.api:app", "--host", "0.0.0.0", "--port", "8000", "--reload"]

# ============================================================
# Stage 4 — RQ Worker
# ============================================================
FROM deps AS worker

COPY backend/ ./backend/
COPY workers/ ./workers/

CMD ["python", "workers/worker.py"]
