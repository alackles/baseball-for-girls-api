# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Fantasy baseball league management platform for an async, points-based snake draft among friends.

- `app/` — Flask REST API + SQLite backend
- `static/index.html` — Single-page frontend (vanilla JS/HTML/CSS, ~1170 lines, no build step)
- `config.json` — League settings, scoring config (edit to customize the league)
- `schema.sql` — SQLite schema (applied on startup via `init_db`)
- `fantasy.db` — SQLite database (created on first run, not committed)

## Development Commands

```bash
# Setup (one-time)
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt
python seed_players.py  # Downloads Chadwick Bureau player register (~100k players, ~30s)

# Run locally
python run.py  # http://localhost:5000 — serves API + static/index.html

# Production
gunicorn "app:create_app()" --bind 0.0.0.0:$PORT

# Manually trigger nightly scoring (e.g. for testing)
python - <<'EOF'
from app import create_app
from app.scoring import write_daily_snapshot
app = create_app()
with app.app_context():
    write_daily_snapshot(app)
EOF

# Initialize the draft (after teams are created and queues set)
curl -X POST http://localhost:5000/api/draft/initialize
```

## Architecture

### Backend (`app/`)

**Application factory** in `app/__init__.py` initializes Flask, CORS, SQLite (WAL mode), and APScheduler with three background jobs:
- Every 5 min: autopick expired draft picks
- Nightly 05:01 UTC (12:01 AM Central): lock daily scores
- Monday 00:05 UTC: apply accepted trades

**Key modules:**
- `api.py` — All REST endpoints under `/api/` (blueprint `bp`). Helpers `_ok()` / `_err()` for uniform responses.
- `draft.py` — Snake draft engine, pick expiration, autopick from team queue
- `scoring.py` — Points formula from `config.json`, daily stat diffs, snapshot locking
- `trades.py` — Proposal/acceptance window enforcement (date-gated), deferred execution on Mondays
- `players.py` — Chadwick Bureau CSV import, MLB Stats API enrichment, name search
- `mlb.py` — Wrapper for public MLB Stats API (`statsapi.mlb.com/api/v1`), 1-hour in-process TTL cache

**Database:** SQLite with 9 tables. The `players` table uses `mlbam_id` (MLB Advanced Media ID) as the primary key and join key everywhere. `weekly_scores.breakdown_json` stores per-player point breakdown as JSON.

**Config:** `config.json` drives almost everything: `season`, `roster_size`, `il_slots`, `pick_timeout_hours`, `trade_window_open/close` (YYYY-MM-DD), `roster_minimums`, and all scoring point values including chaos events.

**Migrations:** Handled inline in `create_app()` via `ALTER TABLE ... ADD COLUMN` wrapped in try/except — safe to re-run.

### Frontend (`static/index.html`)

Single file, tab-based navigation (League, Teams, Draft, Trades, Scores). All data fetched via CORS from the backend API. The `BACKEND_URL` constant at the top of the `<script>` block must be updated when deploying.

## Deployment

- **Backend**: Render (free tier), env vars: `SECRET_KEY`, `DATABASE_PATH`, `FRONTEND_ORIGIN`
- **Frontend**: GitHub Pages (`alackles/baseball-for-girls`) — push `static/index.html` as `index.html`
- **CORS**: Configured for `https://alackles.github.io` + localhost

See `DEPLOY.md` for full step-by-step instructions.

## MLB Stats API

Public, unauthenticated. Endpoints used: `/players`, `/people/{id}/stats`, `/game/{gamePk}/feed/live`, `/schedule`. All responses cached 1 hour in-process via `mlb.py`. Avoid fetching stats in tight loops.

## Notes

- No test suite or linter configured.
- `run.py` uses `use_reloader=False` to prevent APScheduler from starting twice under the Werkzeug reloader.
- `seed_players.py` uses `INSERT OR IGNORE` — safe to re-run to pick up new call-ups.
- Trades are proposed immediately but roster changes are deferred to Monday by the scheduler.
