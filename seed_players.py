"""
seed_players.py
One-time script: download Chadwick Bureau register and populate the players table.
Run once before the draft begins.

Usage:
    python seed_players.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from app import create_app
from app.players import seed_from_chadwick

from app.mlb import search_players as mlb_search

def enrich_active_roster(app):
    """Pull all active MLB players from the Stats API and update positions."""
    import sqlite3, os
    from pathlib import Path
    from app.mlb import _get

    print("Enriching active player positions from MLB Stats API...")

    # Build team ID → abbreviation map (currentTeam only returns id, not abbreviation)
    teams_data = _get("/teams", {"sportId": 1, "season": 2026})
    team_map = {t["id"]: t.get("abbreviation", "") for t in teams_data.get("teams", [])}

    data = _get("/sports/1/players", {"season": 2026, "gameType": "R"})
    people = data.get("people", [])

    db_path = os.environ.get("DATABASE_PATH",
                str(Path(__file__).parent / "fantasy.db"))
    conn = sqlite3.connect(db_path)

    from app.players import POSITION_MAP
    updated = 0
    for p in people:
        mlbam_id = p.get("id")
        pos_raw = p.get("primaryPosition", {}).get("abbreviation", "")
        pos = POSITION_MAP.get(pos_raw, pos_raw)
        bats = p.get("batSide", {}).get("code", "")
        throws = p.get("pitchHand", {}).get("code", "")
        team_id = p.get("currentTeam", {}).get("id")
        team = team_map.get(team_id, "")
        conn.execute(
            "UPDATE players SET position=?, bats=?, throws=?, team=?, active=1 WHERE mlbam_id=?",
            (pos, bats, throws, team, mlbam_id)
        )
        updated += 1

    conn.commit()
    conn.close()
    print(f"Enriched {updated} active players.")
    
if __name__ == "__main__":
    import sqlite3, os
    app = create_app()
    # Only seed if the player table is empty
    from app import get_db
    with app.app_context():
        db = get_db(app)
        count = db.execute("SELECT COUNT(*) FROM players").fetchone()[0]
        if count == 0:
            seed_from_chadwick(app)
            enrich_active_roster(app)
            print("Seeding complete.")
        else:
            print(f"Player table already populated ({count} players), skipping seed.")
        # Mark all seeded players inactive; enrich_active_roster will flip
        # currently-rostered MLB players back to active=1 with team data.
        db_path = os.environ.get("DATABASE_PATH",
                    str(Path(__file__).parent / "fantasy.db"))
        conn = sqlite3.connect(db_path)
        conn.execute("UPDATE players SET active=0")
        conn.commit()
        conn.close()
        enrich_active_roster(app)
        print("Done. Player pool is ready.")
