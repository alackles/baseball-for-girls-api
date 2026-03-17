"""
app/draft.py
Snake draft engine: order generation, pick submission,
expiry checking, and autopick logic.
"""

import sqlite3
from datetime import datetime, timedelta, timezone
from flask import Flask

from app import get_db


# ---------------------------------------------------------------------------
# Snake order generation
# ---------------------------------------------------------------------------
def generate_snake_order(team_ids: list[int], rounds: int) -> list[dict]:
    """
    Returns a list of dicts: {pick_number, round, pick_in_round, team_id}
    for a full snake draft.
    """
    picks = []
    pick_number = 1
    for r in range(1, rounds + 1):
        order = team_ids if r % 2 == 1 else list(reversed(team_ids))
        for i, team_id in enumerate(order):
            picks.append(
                {
                    "pick_number": pick_number,
                    "round": r,
                    "pick_in_round": i + 1,
                    "team_id": team_id,
                }
            )
            pick_number += 1
    return picks


def initialize_draft(app: Flask):
    """
    Populate draft_picks table with the full snake order.
    Requires teams to already exist. Call once before draft begins.
    """
    db = get_db(app)
    cfg = app.config["CONFIG"]
    roster_size = cfg["roster_size"]

    teams = db.execute("SELECT id FROM teams ORDER BY id").fetchall()
    if not teams:
        raise ValueError("No teams found. Create teams before initializing draft.")

    team_ids = [t["id"] for t in teams]
    picks = generate_snake_order(team_ids, roster_size)

    for p in picks:
        db.execute(
            """
            INSERT OR IGNORE INTO draft_picks
                (pick_number, round, pick_in_round, team_id)
            VALUES (?, ?, ?, ?)
            """,
            (p["pick_number"], p["round"], p["pick_in_round"], p["team_id"]),
        )

    # Activate the first pick immediately
    timeout_hours = cfg.get("pick_timeout_hours", 12)
    expires_at = (
        datetime.now(timezone.utc) + timedelta(hours=timeout_hours)
    ).isoformat()
    db.execute(
        "UPDATE draft_picks SET expires_at=? WHERE pick_number=1",
        (expires_at,),
    )

    db.commit()


# ---------------------------------------------------------------------------
# Draft state
# ---------------------------------------------------------------------------
def get_draft_state(app: Flask) -> dict:
    """
    Returns:
      - completed picks (with player names)
      - current active pick (team, expires_at, time_remaining_seconds)
      - draft status: 'pending' | 'active' | 'complete'
    """
    db = get_db(app)

    all_picks = db.execute(
        """
        SELECT dp.pick_number, dp.round, dp.pick_in_round, dp.team_id,
               dp.mlbam_id, dp.picked_at, dp.expires_at, dp.autopicked,
               t.name AS team_name,
               p.name_full AS player_name, p.position
        FROM draft_picks dp
        JOIN teams t ON t.id = dp.team_id
        LEFT JOIN players p ON p.mlbam_id = dp.mlbam_id
        ORDER BY dp.pick_number
        """
    ).fetchall()

    picks = [dict(r) for r in all_picks]
    total = len(picks)
    completed = [p for p in picks if p["picked_at"] is not None]
    pending = [p for p in picks if p["picked_at"] is None]

    if not pending:
        status = "complete" if completed else "pending"
        current_pick = None
    else:
        current_pick = pending[0]
        status = "active"
        if current_pick["expires_at"]:
            expires = datetime.fromisoformat(current_pick["expires_at"])
            now = datetime.now(timezone.utc)
            current_pick["time_remaining_seconds"] = max(
                0, int((expires - now).total_seconds())
            )

    return {
        "status": status,
        "total_picks": total,
        "completed_picks": len(completed),
        "current_pick": current_pick,
        "pick_log": completed[-20:],  # last 20 for the UI
    }


# ---------------------------------------------------------------------------
# Submit a pick
# ---------------------------------------------------------------------------
def submit_pick(team_id: int, mlbam_id: int, app: Flask) -> dict:
    """
    Validate and record a draft pick.
    Returns {"ok": True} or {"ok": False, "error": "..."}.
    """
    db = get_db(app)
    cfg = app.config["CONFIG"]

    # Get the active pick
    active = db.execute(
        "SELECT * FROM draft_picks WHERE picked_at IS NULL ORDER BY pick_number LIMIT 1"
    ).fetchone()

    if not active:
        return {"ok": False, "error": "Draft is complete."}

    if active["team_id"] != team_id:
        return {
            "ok": False,
            "error": f"It is not your pick. Current pick belongs to team {active['team_id']}.",
        }

    # Check expiry
    if active["expires_at"]:
        expires = datetime.fromisoformat(active["expires_at"])
        if datetime.now(timezone.utc) > expires:
            return {"ok": False, "error": "Pick has expired. Autopick will fire shortly."}

    # Check player is available
    taken = db.execute(
        "SELECT 1 FROM draft_picks WHERE mlbam_id=? AND picked_at IS NOT NULL",
        (mlbam_id,),
    ).fetchone()
    if taken:
        return {"ok": False, "error": "Player already drafted."}

    on_roster = db.execute(
        "SELECT 1 FROM rosters WHERE mlbam_id=?", (mlbam_id,)
    ).fetchone()
    if on_roster:
        return {"ok": False, "error": "Player already on a roster."}

    now = datetime.now(timezone.utc).isoformat()

    # Record pick
    db.execute(
        "UPDATE draft_picks SET mlbam_id=?, picked_at=? WHERE pick_number=?",
        (mlbam_id, now, active["pick_number"]),
    )

    # Add to rosters
    db.execute(
        "INSERT INTO rosters (team_id, mlbam_id, slot, added_at) VALUES (?, ?, 'active', ?)",
        (team_id, mlbam_id, now),
    )

    # Activate next pick
    _activate_next_pick(db, active["pick_number"], cfg)
    db.commit()

    return {"ok": True, "pick_number": active["pick_number"]}


# ---------------------------------------------------------------------------
# Autopick
# ---------------------------------------------------------------------------
def process_expired_picks(app: Flask):
    """
    Called every 5 minutes by APScheduler.
    Fires autopick for any expired active pick.
    """
    import os
    from pathlib import Path

    db_path = os.environ.get(
        "DATABASE_PATH",
        str(Path(__file__).parent.parent / "fantasy.db"),
    )
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")

    cfg = app.config["CONFIG"]
    now = datetime.now(timezone.utc)

    active = conn.execute(
        "SELECT * FROM draft_picks WHERE picked_at IS NULL ORDER BY pick_number LIMIT 1"
    ).fetchone()

    if not active or not active["expires_at"]:
        conn.close()
        return

    expires = datetime.fromisoformat(active["expires_at"])
    if now <= expires:
        conn.close()
        return

    # Fire autopick
    player_id = _autopick_player(conn, active["team_id"], cfg)
    if player_id is None:
        conn.close()
        return

    now_iso = now.isoformat()
    conn.execute(
        "UPDATE draft_picks SET mlbam_id=?, picked_at=?, autopicked=1 WHERE pick_number=?",
        (player_id, now_iso, active["pick_number"]),
    )
    conn.execute(
        "INSERT OR IGNORE INTO rosters (team_id, mlbam_id, slot, added_at) VALUES (?, ?, 'active', ?)",
        (active["team_id"], player_id, now_iso),
    )
    _activate_next_pick(conn, active["pick_number"], cfg)
    conn.commit()
    conn.close()


def _autopick_player(conn: sqlite3.Connection,
                     team_id: int, cfg: dict) -> int | None:
    """
    1. First available player from the team's draft queue.
    2. Fallback: best available to fill an unmet roster minimum.
    3. Final fallback: best available by name (alphabetical proxy).
    """
    # Draft queue
    queue = conn.execute(
        """
        SELECT dq.mlbam_id FROM draft_queue dq
        LEFT JOIN draft_picks dp ON dp.mlbam_id = dq.mlbam_id AND dp.picked_at IS NOT NULL
        LEFT JOIN rosters r ON r.mlbam_id = dq.mlbam_id
        WHERE dq.team_id = ? AND dp.mlbam_id IS NULL AND r.mlbam_id IS NULL
        ORDER BY dq.rank
        LIMIT 1
        """,
        (team_id,),
    ).fetchone()
    if queue:
        return queue["mlbam_id"]

    # Unmet minimums
    minimums = cfg.get("roster_minimums", {})
    current_roster = conn.execute(
        """
        SELECT p.position FROM rosters r
        JOIN players p ON p.mlbam_id = r.mlbam_id
        WHERE r.team_id = ?
        """,
        (team_id,),
    ).fetchall()
    position_counts: dict[str, int] = {}
    for row in current_roster:
        pos = row["position"] or "UTIL"
        position_counts[pos] = position_counts.get(pos, 0) + 1

    for pos, min_count in minimums.items():
        if position_counts.get(pos, 0) < min_count:
            candidate = conn.execute(
                """
                SELECT p.mlbam_id FROM players p
                LEFT JOIN draft_picks dp ON dp.mlbam_id = p.mlbam_id AND dp.picked_at IS NOT NULL
                LEFT JOIN rosters r ON r.mlbam_id = p.mlbam_id
                WHERE p.position = ? AND dp.mlbam_id IS NULL AND r.mlbam_id IS NULL AND p.active = 1
                ORDER BY p.name_last
                LIMIT 1
                """,
                (pos,),
            ).fetchone()
            if candidate:
                return candidate["mlbam_id"]

    # Final fallback: any available player
    fallback = conn.execute(
        """
        SELECT p.mlbam_id FROM players p
        LEFT JOIN draft_picks dp ON dp.mlbam_id = p.mlbam_id AND dp.picked_at IS NOT NULL
        LEFT JOIN rosters r ON r.mlbam_id = p.mlbam_id
        WHERE dp.mlbam_id IS NULL AND r.mlbam_id IS NULL AND p.active = 1
        ORDER BY p.name_last
        LIMIT 1
        """
    ).fetchone()
    return fallback["mlbam_id"] if fallback else None


def _activate_next_pick(db, current_pick_number: int, cfg: dict):
    """Set expires_at on the next pick slot."""
    timeout_hours = cfg.get("pick_timeout_hours", 12)
    expires_at = (
        datetime.now(timezone.utc) + timedelta(hours=timeout_hours)
    ).isoformat()
    db.execute(
        "UPDATE draft_picks SET expires_at=? WHERE pick_number=?",
        (expires_at, current_pick_number + 1),
    )


# ---------------------------------------------------------------------------
# Roster minimum validation (at draft completion)
# ---------------------------------------------------------------------------
def validate_roster_minimums(team_id: int, app: Flask) -> dict:
    db = get_db(app)
    minimums = app.config["CONFIG"].get("roster_minimums", {})

    roster = db.execute(
        """
        SELECT p.position FROM rosters r
        JOIN players p ON p.mlbam_id = r.mlbam_id
        WHERE r.team_id = ?
        """,
        (team_id,),
    ).fetchall()

    counts: dict[str, int] = {}
    for row in roster:
        pos = row["position"] or "UTIL"
        counts[pos] = counts.get(pos, 0) + 1

    violations = {}
    for pos, minimum in minimums.items():
        actual = counts.get(pos, 0)
        if actual < minimum:
            violations[pos] = {"required": minimum, "actual": actual}

    return {"valid": len(violations) == 0, "violations": violations}
