"""
app/players.py
Player pool management: search, Chadwick register loading, available players.
"""

import csv
import io
import os
import sqlite3
import requests
from flask import Flask, current_app

from app import get_db
from app.mlb import search_players as mlb_search, get_player

CHADWICK_BASE = "https://raw.githubusercontent.com/chadwickbureau/register/master/data"
CHADWICK_URLS = [f"{CHADWICK_BASE}/people-{c}.csv" for c in "0123456789abcdef"]# Chadwick Bureau register — maps MLBAM IDs to names and other source IDs

# Positions we care about for fantasy — map MLB API abbreviations to canonical
POSITION_MAP = {
    "C": "C",
    "1B": "1B",
    "2B": "2B",
    "3B": "3B",
    "SS": "SS",
    "LF": "OF",
    "CF": "OF",
    "RF": "OF",
    "OF": "OF",
    "DH": "DH",
    "SP": "SP",
    "RP": "RP",
    "P": "SP",   # default pitchers to SP; refine via game log if needed
}


def seed_from_chadwick(app: Flask, limit_active: bool = True):
    """
    Download Chadwick register and populate the players table.
    Should be run once before the draft.
    Only loads players with a key_mlbam (MLBAM ID).
    """
    print("Fetching Chadwick register (16 files)...")

    db_path = os.environ.get("DATABASE_PATH", str(
        __import__("pathlib").Path(__file__).parent.parent / "fantasy.db"
    ))
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys=ON")

    inserted = 0
    batch = []
    for i, url in enumerate(CHADWICK_URLS, 1):
        print(f"  Fetching file {i}/16...")
        resp = requests.get(url, timeout=30, stream=True)
        resp.raise_for_status()
        reader = csv.DictReader(line.decode("utf-8") for line in resp.iter_lines())
        for row in reader:
            mlbam_id = row.get("key_mlbam", "").strip()
            if not mlbam_id:
                continue
            try:
                mlbam_id = int(mlbam_id)
            except ValueError:
                continue

            fg_id = row.get("key_fangraphs", "").strip() or None
            first = (row.get("name_first") or "").strip()
            last = (row.get("name_last") or "").strip()
            if not first and not last:
                continue

            batch.append((mlbam_id, fg_id, first, last))
            if len(batch) >= 1000:
                conn.executemany(
                    """
                    INSERT OR IGNORE INTO players
                        (mlbam_id, fg_id, name_first, name_last, position, active)
                    VALUES (?, ?, ?, ?, NULL, 0)
                    """,
                    batch,
                )
                conn.commit()
                inserted += len(batch)
                batch = []

    if batch:
        conn.executemany(
            """
            INSERT OR IGNORE INTO players
                (mlbam_id, fg_id, name_first, name_last, position, active)
            VALUES (?, ?, ?, ?, NULL, 0)
            """,
            batch,
        )
        conn.commit()
        inserted += len(batch)

    conn.close()
    print(f"Seeded {inserted} players from Chadwick register.")


def enrich_player_from_api(mlbam_id: int, app: Flask):
    """
    Pull position, bats, throws from MLB Stats API and update the DB.
    Call this lazily when a player is searched or drafted.
    """
    p = get_player(mlbam_id)
    if not p:
        return
    db = get_db(app)
    pos = POSITION_MAP.get(p["position"], p["position"])
    db.execute(
        """
        UPDATE players
        SET position=?, bats=?, throws=?, team=?, active=?
        WHERE mlbam_id=?
        """,
        (pos, p["bats"], p["throws"], p.get("team", ""), 1 if p["active"] else 0, mlbam_id),
    )
    db.commit()


def search_players_local(query: str, position: str = None,
                         available_only: bool = False) -> list[dict]:
    """
    Full-text name search against local DB.
    Optionally filter by position or only undrafted players.
    """
    db = get_db(current_app)
    sql = """
        SELECT p.mlbam_id, p.name_full, p.position, p.team, p.bats, p.throws,
               p.active,
               CASE WHEN r.mlbam_id IS NULL THEN 1 ELSE 0 END AS available,
               r.team_id
        FROM players p
        LEFT JOIN rosters r ON r.mlbam_id = p.mlbam_id
        WHERE p.name_full LIKE ? AND p.active = 1
    """
    params = [f"%{query}%"]

    if position:
        sql += " AND p.position = ?"
        params.append(position)

    if available_only:
        sql += " AND r.mlbam_id IS NULL"

    sql += " ORDER BY p.name_last, p.name_first LIMIT 50"

    rows = db.execute(sql, params).fetchall()
    return [dict(r) for r in rows]


def get_available_players(position: str = None, offset: int = 0,
                          limit: int = 50) -> list[dict]:
    """All undrafted players, optionally filtered by position."""
    db = get_db(current_app)
    sql = """
        SELECT p.mlbam_id, p.name_full, p.position, p.team, p.bats, p.throws
        FROM players p
        LEFT JOIN rosters r ON r.mlbam_id = p.mlbam_id
        WHERE r.mlbam_id IS NULL AND p.active = 1
    """
    params: list = []
    if position:
        sql += " AND p.position = ?"
        params.append(position)
    sql += " ORDER BY p.name_last, p.name_first LIMIT ? OFFSET ?"
    params += [limit, offset]
    rows = db.execute(sql, params).fetchall()
    return [dict(r) for r in rows]


def get_roster(team_id: int) -> list[dict]:
    db = get_db(current_app)
    rows = db.execute(
        """
        SELECT p.mlbam_id, p.name_full, p.position, r.slot, r.added_at
        FROM rosters r
        JOIN players p ON p.mlbam_id = r.mlbam_id
        WHERE r.team_id = ?
        ORDER BY p.position, p.name_last
        """,
        (team_id,),
    ).fetchall()
    return [dict(r) for r in rows]
