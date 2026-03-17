"""
app/trades.py
Trade proposal, acceptance, rejection, window enforcement,
and next-week-effective execution.
"""

import sqlite3
import os
from datetime import datetime, timezone
from pathlib import Path

from flask import Flask, current_app

from app import get_db
from app.scoring import _current_week


# ---------------------------------------------------------------------------
# Window enforcement
# ---------------------------------------------------------------------------
def trade_window_open(app: Flask) -> bool:
    cfg = app.config["CONFIG"]
    today = datetime.now(timezone.utc).date()
    window_open = datetime.fromisoformat(cfg["trade_window_open"]).date()
    window_close = datetime.fromisoformat(cfg["trade_window_close"]).date()
    return window_open <= today <= window_close


def trade_window_status(app: Flask) -> dict:
    cfg = app.config["CONFIG"]
    today = datetime.now(timezone.utc).date()
    window_open = datetime.fromisoformat(cfg["trade_window_open"]).date()
    window_close = datetime.fromisoformat(cfg["trade_window_close"]).date()
    is_open = window_open <= today <= window_close
    return {
        "open": is_open,
        "window_open": cfg["trade_window_open"],
        "window_close": cfg["trade_window_close"],
        "days_until_open": max(0, (window_open - today).days) if not is_open else 0,
    }


# ---------------------------------------------------------------------------
# Propose a trade
# ---------------------------------------------------------------------------
def propose_trade(
    proposing_team: int,
    receiving_team: int,
    offering_ids: list[int],   # mlbam_ids proposing team gives away
    requesting_ids: list[int], # mlbam_ids proposing team wants
    app: Flask,
) -> dict:
    if not trade_window_open(app):
        return {"ok": False, "error": "Trade window is not open."}

    db = get_db(app)
    cfg = app.config["CONFIG"]
    season = cfg["season"]
    now = datetime.now(timezone.utc).isoformat()

    # Validate ownership
    for mid in offering_ids:
        row = db.execute(
            "SELECT 1 FROM rosters WHERE team_id=? AND mlbam_id=?",
            (proposing_team, mid),
        ).fetchone()
        if not row:
            return {"ok": False, "error": f"Player {mid} not on proposing team's roster."}

    for mid in requesting_ids:
        row = db.execute(
            "SELECT 1 FROM rosters WHERE team_id=? AND mlbam_id=?",
            (receiving_team, mid),
        ).fetchone()
        if not row:
            return {"ok": False, "error": f"Player {mid} not on receiving team's roster."}

    effective_week = _current_week(season) + 1

    cur = db.execute(
        """
        INSERT INTO trades (proposed_at, status, proposing_team, receiving_team, effective_week)
        VALUES (?, 'pending', ?, ?, ?)
        """,
        (now, proposing_team, receiving_team, effective_week),
    )
    trade_id = cur.lastrowid

    for mid in offering_ids:
        db.execute(
            "INSERT INTO trade_players (trade_id, mlbam_id, from_team, to_team) VALUES (?, ?, ?, ?)",
            (trade_id, mid, proposing_team, receiving_team),
        )
    for mid in requesting_ids:
        db.execute(
            "INSERT INTO trade_players (trade_id, mlbam_id, from_team, to_team) VALUES (?, ?, ?, ?)",
            (trade_id, mid, receiving_team, proposing_team),
        )

    db.commit()
    return {"ok": True, "trade_id": trade_id}


# ---------------------------------------------------------------------------
# Accept / reject
# ---------------------------------------------------------------------------
def resolve_trade(trade_id: int, action: str, team_id: int, app: Flask) -> dict:
    """action: 'accept' or 'reject'"""
    if not trade_window_open(app):
        return {"ok": False, "error": "Trade window is not open."}

    db = get_db(app)
    trade = db.execute(
        "SELECT * FROM trades WHERE id=?", (trade_id,)
    ).fetchone()

    if not trade:
        return {"ok": False, "error": "Trade not found."}
    if trade["status"] != "pending":
        return {"ok": False, "error": f"Trade is already {trade['status']}."}
    if trade["receiving_team"] != team_id:
        return {"ok": False, "error": "Only the receiving team can accept or reject."}

    now = datetime.now(timezone.utc).isoformat()
    status = "accepted" if action == "accept" else "rejected"

    db.execute(
        "UPDATE trades SET status=?, resolved_at=? WHERE id=?",
        (status, now, trade_id),
    )
    db.commit()
    return {"ok": True, "status": status}


# ---------------------------------------------------------------------------
# Apply accepted trades (runs Monday 00:05 via APScheduler)
# ---------------------------------------------------------------------------
def apply_accepted_trades(app: Flask):
    """
    Execute all accepted trades whose effective_week <= current week.
    Swaps players between rosters.
    """
    db_path = os.environ.get(
        "DATABASE_PATH",
        str(Path(__file__).parent.parent / "fantasy.db"),
    )
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")

    cfg = app.config["CONFIG"]
    current_week = _current_week(cfg["season"])
    now = datetime.now(timezone.utc).isoformat()

    pending = conn.execute(
        """
        SELECT * FROM trades
        WHERE status='accepted' AND effective_week <= ?
        """,
        (current_week,),
    ).fetchall()

    for trade in pending:
        players = conn.execute(
            "SELECT * FROM trade_players WHERE trade_id=?", (trade["id"],)
        ).fetchall()

        for p in players:
            # Move player from from_team to to_team
            conn.execute(
                "DELETE FROM rosters WHERE team_id=? AND mlbam_id=?",
                (p["from_team"], p["mlbam_id"]),
            )
            conn.execute(
                """
                INSERT OR REPLACE INTO rosters (team_id, mlbam_id, slot, added_at)
                VALUES (?, ?, 'active', ?)
                """,
                (p["to_team"], p["mlbam_id"], now),
            )

        # Mark trade as executed by setting a completed status
        conn.execute(
            "UPDATE trades SET status='executed' WHERE id=?", (trade["id"],)
        )

    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# Read helpers
# ---------------------------------------------------------------------------
def get_all_trades(app: Flask) -> list[dict]:
    db = get_db(app)
    rows = db.execute(
        """
        SELECT t.id, t.proposed_at, t.resolved_at, t.status,
               t.effective_week,
               pt.name AS proposing_team_name,
               rt.name AS receiving_team_name
        FROM trades t
        JOIN teams pt ON pt.id = t.proposing_team
        JOIN teams rt ON rt.id = t.receiving_team
        ORDER BY t.proposed_at DESC
        """
    ).fetchall()
    trades = []
    for row in rows:
        trade = dict(row)
        players = db.execute(
            """
            SELECT tp.mlbam_id, tp.from_team, tp.to_team,
                   p.name_full, p.position,
                   ft.name AS from_team_name,
                   tt.name AS to_team_name
            FROM trade_players tp
            JOIN players p ON p.mlbam_id = tp.mlbam_id
            JOIN teams ft ON ft.id = tp.from_team
            JOIN teams tt ON tt.id = tp.to_team
            WHERE tp.trade_id=?
            """,
            (trade["id"],),
        ).fetchall()
        trade["players"] = [dict(p) for p in players]
        trades.append(trade)
    return trades
