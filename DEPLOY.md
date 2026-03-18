# Fantasy Baseball — Setup & Deployment Guide

## Prerequisites
- Python 3.11+
- A GitHub account (you have: `alackles`)
- A free Render account (render.com)

---

## Part 1: Local Setup

### 1. Install dependencies
```bash
python -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate
pip install -r requirements.txt
```

### 2. Run locally
```bash
python run.py
```
The app is now running at `http://localhost:5000`.

### 3. Seed the player database
In a separate terminal (with venv active):
```bash
python seed_players.py
```
This downloads the Chadwick Bureau register (~100k players) and populates your
local `fantasy.db`. Takes about 30 seconds. Run once before the draft.

### 4. Create your 4 teams
Use the Settings tab in the frontend to name each team. Or seed them directly:
```bash
python - <<'EOF'
from app import create_app
from app import get_db

app = create_app()
with app.app_context():
    db = get_db(app)
    teams = [
        ("Team Chaos", "Alice"),
        ("The Sluggers", "Bob"),
        ("Earned Run Ave Maria", "Carol"),
        ("Walks and All", "Dave"),
    ]
    for name, owner in teams:
        db.execute("INSERT OR IGNORE INTO teams (name, owner) VALUES (?,?)", (name, owner))
    db.commit()
    print("Teams created.")
EOF
```

### 5. Initialize the draft
Once all 4 teams exist and everyone has set their queue, hit the initialize endpoint:
```bash
curl -X POST http://localhost:5000/api/draft/initialize
```
This generates the full snake order and starts the pick clock on Pick 1.

### 6. Testing the draft locally

Two workflows depending on whether you want to test against real data or a clean slate.

**Workflow A — Isolated test (clean slate, recommended before the real draft):**
Run against a separate database file so `fantasy.db` is never touched.
```bash
DATABASE_PATH=test_draft.db python run.py
# In a second terminal (venv active):
DATABASE_PATH=test_draft.db python seed_players.py
# Then create teams via the Settings tab, set queues, and initialize the draft
# When done: rm test_draft.db
```

**Workflow B — Reset against existing data:**
Snapshot `fantasy.db` first, then reset after the test draft.
```bash
cp fantasy.db fantasy.db.backup     # snapshot before test draft
# ... run draft via the UI ...
python reset_draft.py               # clears picks + rosters; teams/players/queues preserved
# Or restore the full snapshot instead: cp fantasy.db.backup fantasy.db
```

`reset_draft.py` also respects `DATABASE_PATH`, so `DATABASE_PATH=test_draft.db python reset_draft.py` works too.

---

## Part 2: Deploy the Backend to Render

### 1. Create a GitHub repo for the backend
```bash
git init
git add .
git commit -m "Initial commit"
git remote add origin https://github.com/alackles/baseball-for-girls-api
git push -u origin main
```

### 2. Create a Render Web Service
1. Go to [render.com](https://render.com) and sign in with GitHub
2. Click **New → Web Service**
3. Connect your `baseball-for-girls-api` repo
4. Configure:
   - **Environment**: Python 3
   - **Build Command**: `pip install -r requirements.txt`
   - **Start Command**: `gunicorn "app:create_app()" --bind 0.0.0.0:$PORT`
   - **Plan**: Free

### 3. Set environment variables on Render
In your Render service → **Environment** tab, add:

| Key | Value |
|-----|-------|
| `SECRET_KEY` | any long random string (e.g. run `python -c "import secrets; print(secrets.token_hex(32))"`) |
| `DATABASE_PATH` | `/opt/render/project/src/fantasy.db` |

### 4. Deploy and get your backend URL
After deploy succeeds, Render gives you a URL like:
`https://baseball-for-girls-api.onrender.com`

**Note on free tier**: Render free services spin down after 15 minutes of inactivity.
The first request after spin-down takes ~30 seconds. Acceptable for a casual league.

### 5. Seed the production database
After first deploy, SSH into Render (or use the Shell tab) and run:
```bash
python seed_players.py
```
Then create teams and initialize the draft the same way as local setup.

### 6. Deploying code changes mid-season
Render's free tier has an ephemeral filesystem — every redeploy wipes `fantasy.db`. Before pushing any code changes once the draft has started, snapshot the database via the export endpoint and restore it after the redeploy.

**Before pushing:**
1. Open the site in your browser and wait for it to fully load (wakes the service from sleep)
2. Navigate to `https://your-app.onrender.com/api/export` and use **Save As** to save the JSON as `backup.json`

**Push your changes and wait for Render to finish redeploying.**

**After redeploying:**
```bash
python - <<'EOF'
import json, urllib.request

with open("backup.json", "rb") as f:
    data = f.read()

req = urllib.request.Request(
    "https://baseball-for-girls.onrender.com/api/import?key=?",
    data=data,
    headers={"Content-Type": "application/json"},
)
with urllib.request.urlopen(req) as r:
    print(r.read().decode())
EOF
```

Replace `YOUR_SECRET_KEY` with the value you set in step 3. The import endpoint restores all teams, rosters, draft picks, queues, trades, and scores. It also re-seeds the small set of players referenced by your league (~60–100 rows), so a full `seed_players.py` re-run is not needed.

> **Note:** If you export mid-draft, any active pick timers will be expired when the DB is restored. The autopick scheduler will process them within 5 minutes — same behavior as if the timer had naturally run out.

---

## Part 3: Frontend

The frontend (`static/index.html`) is served directly by Flask at the root URL (`/`). No separate deployment is needed — your Render URL is both the API and the frontend.

`BACKEND_URL` in `index.html` is intentionally an empty string so that all API calls use relative URLs (same origin).

To update the frontend, edit `static/index.html`, commit, and push to your backend repo. Render redeploys automatically.

---

## MLB Stats API Notes

No API key or account required. The MLB Stats API is a public, undocumented
(but stable) endpoint operated by MLB. The app uses:

- `statsapi.mlb.com/api/v1/players` — player search
- `statsapi.mlb.com/api/v1/people/{id}/stats` — season stats + game logs
- `statsapi.mlb.com/api/v1/game/{gamePk}/feed/live` — play-by-play (chaos events)
- `statsapi.mlb.com/api/v1/schedule` — game schedule

Rate limiting is not officially documented. The app caches responses for 1 hour
and only fetches for rostered players, so a 4-team league will generate minimal
API traffic. No action needed on your end.

---

## Weekly Scoring

Scores are computed automatically every Sunday at midnight (UTC) by the
APScheduler job running inside your Render service. If you want to trigger
manually (e.g. for testing), you can call the scoring function directly:

```bash
python - <<'EOF'
from app import create_app
from app.scoring import write_weekly_snapshot
app = create_app()
with app.app_context():
    write_weekly_snapshot(app)
    print("Done.")
EOF
```

---

## Troubleshooting

**"Player not found in search"**: The Chadwick register only contains players
with professional records. If a very recent call-up is missing, run
`seed_players.py` again — it uses `INSERT OR IGNORE` so it's safe to re-run.

**Render cold start is slow**: Expected on free tier. The first request after
15 minutes of inactivity takes ~30 seconds. Subsequent requests are fast.

**Draft pick expired immediately**: Check that your server's system clock is
correct and that `pick_timeout_hours` in `config.json` is set to your preference.

**Trade window not opening**: Verify `trade_window_open` and `trade_window_close`
in `config.json` match the format `YYYY-MM-DD` and that Render's server timezone
is UTC (it is by default).
