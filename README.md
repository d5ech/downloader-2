# Facebook Ad Library Creative Downloader

A production-ready tool for extracting and downloading creative assets (images, videos) from the [Facebook Ad Library](https://www.facebook.com/ads/library/).

## Architecture

```
┌─────────────────────────────────────────────────────────┐
│                     Next.js Frontend                     │
│        Submit URL → Poll Status → Download Assets        │
└────────────────────────┬────────────────────────────────┘
                         │ HTTP
┌────────────────────────▼────────────────────────────────┐
│                    FastAPI Backend                        │
│   POST /jobs  ·  GET /jobs/:id  ·  GET /jobs/:id/assets  │
└───────────┬────────────────────────────┬────────────────┘
            │ Enqueue                    │ Fetch result
┌───────────▼────────┐       ┌──────────▼──────────┐
│   Redis (RQ queue) │       │   Saved media files  │
└───────────┬────────┘       └──────────────────────┘
            │ Dequeue
┌───────────▼────────────────────────────────────────────┐
│                    RQ Worker(s)                          │
│   scraper.py (Playwright)  →  downloader.py (requests)   │
└────────────────────────────────────────────────────────┘
```

## Tech Stack

| Layer     | Technology                    |
|-----------|-------------------------------|
| Backend   | Python 3.11, FastAPI, Uvicorn |
| Scraping  | Playwright (Chromium)         |
| Downloads | requests                      |
| Queue     | Redis + RQ                    |
| Frontend  | Next.js 14, TypeScript, Tailwind CSS |
| Container | Docker, docker-compose        |

## Quick Start (Docker)

```bash
# 1. Clone and configure environment
cp .env.example .env
# Edit .env as needed

# 2. Start all services
docker compose -f docker/docker-compose.yml up --build

# 3. Open the UI
open http://localhost:3000

# 4. (Optional) RQ Dashboard
docker compose -f docker/docker-compose.yml --profile dev up
open http://localhost:9181
```

## Local Development (without Docker)

### Prerequisites

- Python 3.11+
- Node.js 20+
- Redis running locally on port 6379

### Backend

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium

# Terminal 1 — API server
uvicorn backend.api:app --reload --port 8000

# Terminal 2 — Worker
python workers/worker.py
```

### Frontend

```bash
cd frontend
npm install
npm run dev   # http://localhost:3000
```

## API Reference

| Method | Endpoint                              | Description                        |
|--------|---------------------------------------|------------------------------------|
| GET    | `/health`                             | Liveness probe                     |
| POST   | `/jobs`                               | Submit a new download job          |
| GET    | `/jobs/{job_id}`                      | Poll job status                    |
| GET    | `/jobs/{job_id}/assets`               | List downloaded asset metadata     |
| GET    | `/jobs/{job_id}/assets/{filename}`    | Stream / download a single asset   |
| DELETE | `/jobs/{job_id}`                      | Cancel a job                       |

### POST `/jobs` payload

```json
{
  "url": "https://www.facebook.com/ads/library/?...",
  "max_assets": 20
}
```

## Project Structure

```
.
├── backend/
│   ├── api.py          # FastAPI app, route definitions
│   ├── scraper.py      # Playwright browser automation
│   ├── downloader.py   # HTTP asset downloader (requests)
│   └── utils.py        # Shared helpers (Redis, logging, env)
├── workers/
│   ├── worker.py       # RQ worker entry point
│   └── tasks.py        # Background task definitions
├── frontend/
│   └── src/
│       ├── app/        # Next.js App Router pages
│       ├── components/ # React components
│       └── lib/        # API client (axios)
├── docker/
│   ├── Dockerfile          # Python (API + Worker)
│   ├── Dockerfile.frontend # Next.js
│   └── docker-compose.yml
├── media/
│   └── downloads/      # Downloaded assets (gitignored)
├── requirements.txt
├── .env.example
└── README.md
```

## Scaling Workers

```bash
# Run 3 parallel workers
docker compose -f docker/docker-compose.yml up --scale worker=3
```

## TODO / Next Steps

- [ ] Implement real DOM selectors in `scraper.py` (`_scrape_search_results`, `_scrape_single_ad`)
- [ ] Add authentication / API key middleware to FastAPI
- [ ] Implement ZIP bundling for bulk downloads
- [ ] Add webhook support for job completion notifications
- [ ] Write unit tests for scraper and downloader modules
- [ ] Add rate limiting to the API

## Legal Notice

This tool is intended for personal or research use. Always comply with
[Facebook's Terms of Service](https://www.facebook.com/terms.php) and the
[Ad Library usage policy](https://www.facebook.com/ads/library/). Do not use
this tool to harvest data at scale or for commercial purposes without
appropriate authorisation.
