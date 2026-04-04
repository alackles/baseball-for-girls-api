"""
app/__init__.py
Flask application factory with APScheduler initialization.
"""

import os
import sqlite3
import json
from pathlib import Path

from flask import Flask, g
from apscheduler.schedulers.background import BackgroundScheduler

# ---------------------------------------------------------------------------
# Paths & config
# ---------------------------------------------------------------------------
ROOT = Path(__file__).parent.parent
CONFIG_PATH = ROOT / "config.json"
SCHEMA_PATH = ROOT / "schema.sql"
DEFAULT_DB_PATH = ROOT / "fantasy.db"


def load_config() -> dict:
    with open(CONFIG_PATH) as f:
        return json.load(f)


# ---------------------------------------------------------------------------
# Database helpers
# ---------------------------------------------------------------------------
def get_db(app: Flask) -> sqlite3.Connection:
    """Return a per-request SQLite connection stored on Flask's g object."""
    if "db" not in g:
        db_path = os.environ.get("DATABASE_PATH", str(DEFAULT_DB_PATH))
        g.db = sqlite3.connect(db_path, detect_types=sqlite3.PARSE_DECLTYPES)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys=ON")
        g.db.execute("PRAGMA journal_mode=WAL")
    return g.db


def close_db(e=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db(app: Flask):
    """Apply schema.sql to a fresh database."""
    db_path = os.environ.get("DATABASE_PATH", str(DEFAULT_DB_PATH))
    conn = sqlite3.connect(db_path)
    with open(SCHEMA_PATH) as f:
        conn.executescript(f.read())
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# Scheduler jobs (imported lazily to avoid circular imports)
# ---------------------------------------------------------------------------
def _autopick_job():
    """Fired every 5 minutes; processes any expired draft picks."""
    from app.draft import process_expired_picks
    # Build a minimal app context so jobs can use get_db
    app = _get_current_app()
    with app.app_context():
        process_expired_picks(app)


def _daily_snapshot_job():
    """Fired nightly at 12:01 AM Central (05:01 UTC); locks daily scores."""
    from app.scoring import write_daily_snapshot
    app = _get_current_app()
    with app.app_context():
        write_daily_snapshot(app)


def _apply_pending_trades_job():
    """Fired Monday 00:05; applies accepted trades for the new week."""
    from app.trades import apply_accepted_trades
    app = _get_current_app()
    with app.app_context():
        apply_accepted_trades(app)


# Module-level reference so jobs can retrieve the app instance
_app_instance = None


def _get_current_app() -> Flask:
    return _app_instance


# ---------------------------------------------------------------------------
# Application factory
# ---------------------------------------------------------------------------
def create_app() -> Flask:
    global _app_instance

    app = Flask(__name__, static_folder="../static", static_url_path="/")
    app.secret_key = os.environ.get("SECRET_KEY", "dev-insecure-key")
    app.config["CONFIG"] = load_config()

    # Migrate existing DBs: add color column if absent
    db_path = os.environ.get("DATABASE_PATH", str(DEFAULT_DB_PATH))
    _conn = sqlite3.connect(db_path)
    try:
        _conn.execute("ALTER TABLE teams ADD COLUMN color TEXT NOT NULL DEFAULT '#e85d26'")
        _conn.commit()
    except sqlite3.OperationalError:
        pass  # column already exists

    # Migrate: create bonus tables if absent (for DBs created before this feature)
    _conn.executescript("""
        CREATE TABLE IF NOT EXISTS bonus_proposals (
            id               INTEGER PRIMARY KEY,
            proposed_by_team INTEGER NOT NULL REFERENCES teams(id),
            mlbam_id         INTEGER NOT NULL REFERENCES players(mlbam_id),
            points           REAL NOT NULL,
            reason           TEXT NOT NULL,
            proposed_at      TEXT NOT NULL,
            status           TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending', 'approved', 'rejected')),
            resolved_at      TEXT
        );
        CREATE TABLE IF NOT EXISTS bonus_votes (
            proposal_id INTEGER NOT NULL REFERENCES bonus_proposals(id),
            team_id     INTEGER NOT NULL REFERENCES teams(id),
            vote        TEXT NOT NULL CHECK(vote IN ('approve', 'reject')),
            PRIMARY KEY (proposal_id, team_id)
        );
    """)
    _conn.commit()
    _conn.close()

    # Ensure DB schema is applied
    init_db(app)

    # Tear-down DB connection after each request
    app.teardown_appcontext(close_db)

    # Register blueprints
    from app.api import bp as api_bp
    app.register_blueprint(api_bp, url_prefix="/api")

    # Serve index.html at root for convenience during local dev
    @app.route("/")
    def index():
        return app.send_static_file("index.html")

    # Start background scheduler
    scheduler = BackgroundScheduler(daemon=True)
    scheduler.add_job(_autopick_job, "interval", minutes=5, id="autopick")
    scheduler.add_job(
        _daily_snapshot_job, "cron", hour=5, minute=1, id="daily_scores"
    )
    scheduler.add_job(
        _apply_pending_trades_job,
        "cron",
        day_of_week="mon",
        hour=0,
        minute=5,
        id="trades",
    )
    scheduler.start()

    _app_instance = app
    return app
