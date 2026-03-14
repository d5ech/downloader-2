# ============================================================
# Stage 1 — Python base with system dependencies
# ============================================================
FROM python:3.11-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

# System packages required by Playwright and general tooling
RUN apt-get update && apt-get install -y --no-install-recommends \
        # Playwright runtime deps
        libnss3 libatk1.0-0 libatk-bridge2.0-0 libcups2 libxkbcommon0 \
        libxcomposite1 libxdamage1 libxrandr2 libgbm1 libasound2 \
        libpangocairo-1.0-0 libpango-1.0-0 libcairo2 libgtk-3-0 \
        # Misc
        curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# ============================================================
# Stage 2 — Python dependencies
# ============================================================
FROM base AS deps

COPY requirements.txt .
RUN pip install --upgrade pip && pip install -r requirements.txt

# Install Playwright browser binaries
RUN playwright install chromium --with-deps

# ============================================================
# Stage 3 — API server
# ============================================================
FROM deps AS api

COPY backend/ ./backend/
COPY workers/ ./workers/

EXPOSE 8000

CMD ["uvicorn", "backend.api:app", "--host", "0.0.0.0", "--port", "8000", "--reload"]

# ============================================================
# Stage 4 — RQ Worker
# ============================================================
FROM deps AS worker

COPY backend/ ./backend/
COPY workers/ ./workers/

CMD ["python", "workers/worker.py"]
