"""
app/api.py
All REST API routes.
"""

import json
from datetime import datetime, timezone

from flask import Blueprint, request, jsonify, current_app

from app import get_db
from app.players import (
    search_players_local,
    get_available_players,
    get_roster,
)
from app.draft import (
    get_draft_state,
    submit_pick,
    initialize_draft,
    validate_roster_minimums,
)
from app.trades import (
    propose_trade,
    resolve_trade,
    get_all_trades,
    trade_window_status,
)
from app.mlb import is_player_on_il

bp = Blueprint("api", __name__)


def _ok(data: dict = None, **kwargs):
    return jsonify({"ok": True, **(data or {}), **kwargs})


def _err(message: str, status: int = 400):
    return jsonify({"ok": False, "error": message}), status


# ---------------------------------------------------------------------------
# League
# ---------------------------------------------------------------------------
@bp.get("/league")
def league():
    db = get_db(current_app)
    cfg = current_app.config["CONFIG"]

    teams = db.execute("SELECT id, name, owner, color FROM teams ORDER BY id").fetchall()

    standings = db.execute(
        """
        SELECT team_id, SUM(points) AS total_points
        FROM weekly_scores
        WHERE season=?
        GROUP BY team_id
        ORDER BY total_points DESC
        """,
        (cfg["season"],),
    ).fetchall()
    standings_map = {r["team_id"]: r["total_points"] for r in standings}

    result = []
    for t in teams:
        result.append(
            {
                "id": t["id"],
                "name": t["name"],
                "owner": t["owner"],
                "color": t["color"],
                "total_points": standings_map.get(t["id"], 0.0),
            }
        )

    return _ok(
        teams=result,
        league_name=cfg["league_name"],
        season=cfg["season"],
        trade_window=trade_window_status(current_app),
        roster_minimums=cfg.get("roster_minimums", {}),
        roster_size=cfg.get("roster_size", 15),
        points=cfg.get("points", {}),
    )


@bp.post("/teams")
def create_team():
    db = get_db(current_app)
    data = request.get_json(silent=True) or {}
    name = data.get("name", "").strip()
    owner = data.get("owner", "").strip()
    color = data.get("color", "#e85d26").strip()
    if not name or not owner:
        return _err("Name and owner are required.")
    cur = db.execute("INSERT INTO teams (name, owner, color) VALUES (?,?,?)", (name, owner, color))
    db.commit()
    return _ok(team_id=cur.lastrowid)


@bp.patch("/teams/<int:team_id>")
def update_team(team_id: int):
    db = get_db(current_app)
    data = request.get_json(silent=True) or {}
    name = data.get("name", "").strip()
    owner = data.get("owner", "").strip()
    color = data.get("color", "").strip()

    if not name and not owner and not color:
        return _err("Provide at least one of: name, owner, color.")

    updates, params = [], []
    if name:
        updates.append("name=?")
        params.append(name)
    if owner:
        updates.append("owner=?")
        params.append(owner)
    if color:
        updates.append("color=?")
        params.append(color)
    params.append(team_id)

    db.execute(f"UPDATE teams SET {', '.join(updates)} WHERE id=?", params)
    db.commit()
    return _ok()


# ---------------------------------------------------------------------------
# Draft
# ---------------------------------------------------------------------------
@bp.get("/draft/state")
def draft_state():
    state = get_draft_state(current_app)
    return _ok(**state)


@bp.post("/draft/pick")
def draft_pick():
    data = request.get_json(silent=True) or {}
    team_id = data.get("team_id")
    mlbam_id = data.get("mlbam_id")
    if not team_id or not mlbam_id:
        return _err("team_id and mlbam_id required.")
    result = submit_pick(int(team_id), int(mlbam_id), current_app)
    if not result["ok"]:
        return _err(result["error"])
    return _ok(**result)


@bp.get("/draft/queue/<int:team_id>")
def get_queue(team_id: int):
    db = get_db(current_app)
    rows = db.execute(
        """
        SELECT dq.mlbam_id, dq.rank, p.name_full, p.position
        FROM draft_queue dq
        JOIN players p ON p.mlbam_id = dq.mlbam_id
        WHERE dq.team_id=?
        ORDER BY dq.rank
        """,
        (team_id,),
    ).fetchall()
    return _ok(queue=[dict(r) for r in rows])


@bp.post("/draft/queue/<int:team_id>")
def set_queue(team_id: int):
    """
    Replace the team's draft queue.
    Body: {"queue": [mlbam_id, mlbam_id, ...]}  (ordered, highest priority first)
    """
    data = request.get_json(silent=True) or {}
    queue = data.get("queue", [])
    db = get_db(current_app)
    db.execute("DELETE FROM draft_queue WHERE team_id=?", (team_id,))
    for rank, mlbam_id in enumerate(queue, start=1):
        db.execute(
            "INSERT OR REPLACE INTO draft_queue (team_id, mlbam_id, rank) VALUES (?,?,?)",
            (team_id, int(mlbam_id), rank),
        )
    db.commit()
    return _ok()


@bp.post("/draft/initialize")
def draft_initialize():
    """Admin endpoint: build the snake order and activate pick 1."""
    try:
        initialize_draft(current_app)
        return _ok(message="Draft initialized.")
    except Exception as e:
        return _err(str(e))


@bp.post("/draft/reset")
def draft_reset():
    """Admin endpoint: wipe draft picks and rosters, then re-initialize."""
    db = get_db(current_app)
    db.execute("DELETE FROM rosters")
    db.execute("DELETE FROM draft_picks")
    db.commit()
    try:
        initialize_draft(current_app)
        return _ok(message="Draft reset and re-initialized.")
    except Exception as e:
        return _err(str(e))


# ---------------------------------------------------------------------------
# Players
# ---------------------------------------------------------------------------
@bp.get("/players/search")
def player_search():
    q = request.args.get("q", "").strip()
    position = request.args.get("position")
    available_only = request.args.get("available_only", "false").lower() == "true"
    if len(q) < 2:
        return _err("Query must be at least 2 characters.")
    results = search_players_local(q, position=position, available_only=available_only)
    return _ok(players=results)


@bp.get("/players/available")
def players_available():
    position = request.args.get("position")
    offset = int(request.args.get("offset", 0))
    limit = int(request.args.get("limit", 50))
    players = get_available_players(position=position, offset=offset, limit=limit)
    return _ok(players=players)


# ---------------------------------------------------------------------------
# Rosters
# ---------------------------------------------------------------------------
@bp.get("/roster/<int:team_id>")
def roster(team_id: int):
    db = get_db(current_app)
    cfg = current_app.config["CONFIG"]
    season = cfg["season"]

    players = get_roster(team_id)

    # Attach season points per player from weekly_scores breakdown
    weeks = db.execute(
        "SELECT breakdown_json FROM weekly_scores WHERE team_id=? AND season=?",
        (team_id, season),
    ).fetchall()
    player_points: dict[str, float] = {}
    for week in weeks:
        breakdown = json.loads(week["breakdown_json"] or "{}")
        for mid, data in breakdown.items():
            player_points[mid] = player_points.get(mid, 0.0) + data.get("total", 0.0)

    for p in players:
        p["season_points"] = player_points.get(str(p["mlbam_id"]), 0.0)

    return _ok(roster=players)


@bp.post("/roster/<int:team_id>/add")
def roster_add(team_id: int):
    data = request.get_json(silent=True) or {}
    mlbam_id = data.get("mlbam_id")
    if not mlbam_id:
        return _err("mlbam_id required.")

    db = get_db(current_app)
    cfg = current_app.config["CONFIG"]
    roster_size = cfg["roster_size"]
    il_slots = cfg.get("il_slots", 2)

    # Check roster isn't full (active slots only)
    active_count = db.execute(
        "SELECT COUNT(*) FROM rosters WHERE team_id=? AND slot='active'",
        (team_id,),
    ).fetchone()[0]
    total_count = db.execute(
        "SELECT COUNT(*) FROM rosters WHERE team_id=?", (team_id,)
    ).fetchone()[0]
    max_roster = roster_size + il_slots

    if total_count >= max_roster:
        return _err(f"Roster full ({max_roster} players max including IL slots).")

    # Check player is available
    taken = db.execute(
        "SELECT 1 FROM rosters WHERE mlbam_id=?", (mlbam_id,)
    ).fetchone()
    if taken:
        return _err("Player is already on a roster.")

    now = datetime.now(timezone.utc).isoformat()
    db.execute(
        "INSERT INTO rosters (team_id, mlbam_id, slot, added_at) VALUES (?, ?, 'active', ?)",
        (team_id, int(mlbam_id), now),
    )
    db.commit()
    return _ok()


@bp.post("/roster/<int:team_id>/drop")
def roster_drop(team_id: int):
    data = request.get_json(silent=True) or {}
    mlbam_id = data.get("mlbam_id")
    if not mlbam_id:
        return _err("mlbam_id required.")

    db = get_db(current_app)
    result = db.execute(
        "DELETE FROM rosters WHERE team_id=? AND mlbam_id=?",
        (team_id, int(mlbam_id)),
    )
    if result.rowcount == 0:
        return _err("Player not on this roster.")
    db.commit()
    return _ok()


@bp.post("/roster/<int:team_id>/il")
def roster_il(team_id: int):
    """Move a player from active to IL slot (gated on MLB IL status)."""
    data = request.get_json(silent=True) or {}
    mlbam_id = data.get("mlbam_id")
    if not mlbam_id:
        return _err("mlbam_id required.")

    db = get_db(current_app)
    cfg = current_app.config["CONFIG"]
    il_slots = cfg.get("il_slots", 2)

    row = db.execute(
        "SELECT slot FROM rosters WHERE team_id=? AND mlbam_id=?",
        (team_id, int(mlbam_id)),
    ).fetchone()
    if not row:
        return _err("Player not on this roster.")
    if row["slot"] == "IL":
        return _err("Player is already on IL.")

    # Check IL slot availability
    il_count = db.execute(
        "SELECT COUNT(*) FROM rosters WHERE team_id=? AND slot='IL'", (team_id,)
    ).fetchone()[0]
    if il_count >= il_slots:
        return _err(f"IL slots full ({il_slots} max).")

    # Gate on MLB IL status
    if not is_player_on_il(int(mlbam_id)):
        return _err("Player is not on the MLB IL. Only MLB-IL-listed players can be moved to your IL slot.")

    db.execute(
        "UPDATE rosters SET slot='IL' WHERE team_id=? AND mlbam_id=?",
        (team_id, int(mlbam_id)),
    )
    db.commit()
    return _ok()


@bp.post("/roster/<int:team_id>/activate")
def roster_activate(team_id: int):
    """Move a player from IL back to active."""
    data = request.get_json(silent=True) or {}
    mlbam_id = data.get("mlbam_id")
    if not mlbam_id:
        return _err("mlbam_id required.")

    db = get_db(current_app)
    cfg = current_app.config["CONFIG"]
    roster_size = cfg["roster_size"]

    row = db.execute(
        "SELECT slot FROM rosters WHERE team_id=? AND mlbam_id=?",
        (team_id, int(mlbam_id)),
    ).fetchone()
    if not row:
        return _err("Player not on this roster.")
    if row["slot"] == "active":
        return _err("Player is already active.")

    # Check active roster isn't full
    active_count = db.execute(
        "SELECT COUNT(*) FROM rosters WHERE team_id=? AND slot='active'", (team_id,)
    ).fetchone()[0]
    if active_count >= roster_size:
        return _err(f"Active roster full ({roster_size}). Drop a player first.")

    db.execute(
        "UPDATE rosters SET slot='active' WHERE team_id=? AND mlbam_id=?",
        (team_id, int(mlbam_id)),
    )
    db.commit()
    return _ok()


# ---------------------------------------------------------------------------
# Standings
# ---------------------------------------------------------------------------
@bp.get("/standings")
def standings():
    db = get_db(current_app)
    cfg = current_app.config["CONFIG"]
    season = cfg["season"]

    rows = db.execute(
        """
        SELECT ws.team_id, t.name AS team_name, t.owner,
               ws.week_number, ws.points
        FROM weekly_scores ws
        JOIN teams t ON t.id = ws.team_id
        WHERE ws.season=?
        ORDER BY ws.week_number
        """,
        (season,),
    ).fetchall()

    # Approved bonus points per team
    bonus_rows = db.execute(
        """
        SELECT proposed_by_team AS team_id, SUM(points) AS bonus_total
        FROM bonus_proposals
        WHERE status='approved'
        GROUP BY proposed_by_team
        """
    ).fetchall()
    bonus_map = {r["team_id"]: r["bonus_total"] for r in bonus_rows}

    teams: dict = {}
    for r in rows:
        tid = r["team_id"]
        if tid not in teams:
            teams[tid] = {
                "team_id": tid,
                "team_name": r["team_name"],
                "owner": r["owner"],
                "total_points": 0.0,
                "bonus_points": bonus_map.get(tid, 0.0),
                "weekly": [],
            }
        teams[tid]["total_points"] += r["points"]
        teams[tid]["weekly"].append(
            {"period": r["week_number"], "points": r["points"]}
        )

    # Include any teams with bonus points but no weekly scores yet
    for tid, bonus in bonus_map.items():
        if tid not in teams:
            t_row = db.execute(
                "SELECT id, name, owner FROM teams WHERE id=?", (tid,)
            ).fetchone()
            if t_row:
                teams[tid] = {
                    "team_id": tid,
                    "team_name": t_row["name"],
                    "owner": t_row["owner"],
                    "total_points": 0.0,
                    "bonus_points": bonus,
                    "weekly": [],
                }

    for t in teams.values():
        t["total_points"] += t["bonus_points"]

    sorted_teams = sorted(
        teams.values(), key=lambda t: t["total_points"], reverse=True
    )
    for i, t in enumerate(sorted_teams):
        t["rank"] = i + 1

    return _ok(standings=sorted_teams, season=season)


@bp.get("/scores/<int:team_id>")
def scores(team_id: int):
    db = get_db(current_app)
    cfg = current_app.config["CONFIG"]
    rows = db.execute(
        """
        SELECT week_number, points, computed_at, breakdown_json
        FROM weekly_scores
        WHERE team_id=? AND season=?
        ORDER BY week_number
        """,
        (team_id, cfg["season"]),
    ).fetchall()
    weeks = []
    for r in rows:
        entry = {
            "period": r["week_number"],
            "points": r["points"],
            "computed_at": r["computed_at"],
            "breakdown": json.loads(r["breakdown_json"] or "{}"),
        }
        weeks.append(entry)
    return _ok(weeks=weeks)


@bp.get("/player/<int:mlbam_id>/breakdown")
def player_breakdown(mlbam_id: int):
    db = get_db(current_app)
    cfg = current_app.config["CONFIG"]
    season = int(request.args.get("season", cfg["season"]))
    rows = db.execute(
        """
        SELECT week_number, breakdown_json
        FROM weekly_scores
        WHERE season = ?
        ORDER BY week_number
        """,
        (season,),
    ).fetchall()
    pid = str(mlbam_id)
    events = []
    seen = set()  # deduplicate if player appears in multiple teams same day (shouldn't happen)
    for r in rows:
        breakdown = json.loads(r["breakdown_json"] or "{}")
        player_data = breakdown.get(pid)
        if not player_data:
            continue
        day = r["week_number"]
        if day in seen:
            continue
        seen.add(day)
        for ev in player_data.get("events", []):
            events.append({"day": day, **ev})
    return _ok(mlbam_id=mlbam_id, events=events)


# ---------------------------------------------------------------------------
# Trades
# ---------------------------------------------------------------------------
@bp.get("/trades")
def trades():
    all_trades = get_all_trades(current_app)
    return _ok(
        trades=all_trades,
        window=trade_window_status(current_app),
    )


@bp.post("/trades/propose")
def trades_propose():
    data = request.get_json(silent=True) or {}
    proposing_team = data.get("proposing_team")
    receiving_team = data.get("receiving_team")
    offering = data.get("offering", [])
    requesting = data.get("requesting", [])

    if not all([proposing_team, receiving_team, offering, requesting]):
        return _err("proposing_team, receiving_team, offering, and requesting are all required.")

    result = propose_trade(
        int(proposing_team),
        int(receiving_team),
        [int(x) for x in offering],
        [int(x) for x in requesting],
        current_app,
    )
    if not result["ok"]:
        return _err(result["error"])
    return _ok(**result)


@bp.post("/trades/<int:trade_id>/accept")
def trades_accept(trade_id: int):
    data = request.get_json(silent=True) or {}
    team_id = data.get("team_id")
    if not team_id:
        return _err("team_id required.")
    result = resolve_trade(trade_id, "accept", int(team_id), current_app)
    if not result["ok"]:
        return _err(result["error"])
    return _ok(**result)


@bp.post("/trades/<int:trade_id>/reject")
def trades_reject(trade_id: int):
    data = request.get_json(silent=True) or {}
    team_id = data.get("team_id")
    if not team_id:
        return _err("team_id required.")
    result = resolve_trade(trade_id, "reject", int(team_id), current_app)
    if not result["ok"]:
        return _err(result["error"])
    return _ok(**result)


# ---------------------------------------------------------------------------
# Bonus Proposals
# ---------------------------------------------------------------------------
BONUS_APPROVE_THRESHOLD = 3  # votes needed to approve
BONUS_REJECT_THRESHOLD = 1   # votes needed to reject


def _resolve_proposal_if_majority(db, proposal_id: int):
    """Auto-approve at 3 approve votes; auto-reject at 1 reject vote."""
    votes = db.execute(
        "SELECT vote, COUNT(*) AS cnt FROM bonus_votes WHERE proposal_id=? GROUP BY vote",
        (proposal_id,),
    ).fetchall()
    vote_counts = {r["vote"]: r["cnt"] for r in votes}
    approve_count = vote_counts.get("approve", 0)
    reject_count = vote_counts.get("reject", 0)

    if reject_count >= BONUS_REJECT_THRESHOLD:
        db.execute(
            "UPDATE bonus_proposals SET status='rejected', resolved_at=? WHERE id=?",
            (datetime.now(timezone.utc).isoformat(), proposal_id),
        )
    elif approve_count >= BONUS_APPROVE_THRESHOLD:
        db.execute(
            "UPDATE bonus_proposals SET status='approved', resolved_at=? WHERE id=?",
            (datetime.now(timezone.utc).isoformat(), proposal_id),
        )


@bp.get("/bonus-proposals")
def bonus_proposals_list():
    db = get_db(current_app)
    proposals = db.execute(
        """
        SELECT bp.id, bp.proposed_by_team, t.name AS proposer_name,
               bp.mlbam_id, p.name_full AS player_name, p.team AS player_team,
               bp.points, bp.reason, bp.proposed_at, bp.status, bp.resolved_at
        FROM bonus_proposals bp
        JOIN teams t ON t.id = bp.proposed_by_team
        JOIN players p ON p.mlbam_id = bp.mlbam_id
        ORDER BY bp.proposed_at DESC
        """
    ).fetchall()

    result = []
    for prop in proposals:
        votes = db.execute(
            """
            SELECT bv.team_id, t.name AS team_name, bv.vote
            FROM bonus_votes bv
            JOIN teams t ON t.id = bv.team_id
            WHERE bv.proposal_id=?
            """,
            (prop["id"],),
        ).fetchall()
        approve_count = sum(1 for v in votes if v["vote"] == "approve")
        reject_count = sum(1 for v in votes if v["vote"] == "reject")
        result.append({
            "id": prop["id"],
            "proposed_by_team": prop["proposed_by_team"],
            "proposer_name": prop["proposer_name"],
            "mlbam_id": prop["mlbam_id"],
            "player_name": prop["player_name"],
            "player_team": prop["player_team"],
            "points": prop["points"],
            "reason": prop["reason"],
            "proposed_at": prop["proposed_at"],
            "status": prop["status"],
            "resolved_at": prop["resolved_at"],
            "approve_count": approve_count,
            "reject_count": reject_count,
            "votes": [dict(v) for v in votes],
        })

    total_teams = db.execute("SELECT COUNT(*) FROM teams").fetchone()[0]
    return _ok(proposals=result, total_teams=total_teams)


@bp.post("/bonus-proposals")
def bonus_proposals_create():
    db = get_db(current_app)
    data = request.get_json(silent=True) or {}
    proposed_by_team = data.get("proposed_by_team")
    mlbam_id = data.get("mlbam_id")
    points = data.get("points")
    reason = (data.get("reason") or "").strip()

    if not all([proposed_by_team, mlbam_id, points is not None, reason]):
        return _err("proposed_by_team, mlbam_id, points, and reason are all required.")
    if not reason:
        return _err("reason is required.")

    team = db.execute("SELECT id FROM teams WHERE id=?", (int(proposed_by_team),)).fetchone()
    if not team:
        return _err("Team not found.")
    player = db.execute("SELECT mlbam_id FROM players WHERE mlbam_id=?", (int(mlbam_id),)).fetchone()
    if not player:
        return _err("Player not found.")

    now = datetime.now(timezone.utc).isoformat()
    cur = db.execute(
        """
        INSERT INTO bonus_proposals (proposed_by_team, mlbam_id, points, reason, proposed_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        (int(proposed_by_team), int(mlbam_id), float(points), reason, now),
    )
    db.commit()
    return _ok(proposal_id=cur.lastrowid)


@bp.post("/bonus-proposals/<int:proposal_id>/vote")
def bonus_proposals_vote(proposal_id: int):
    db = get_db(current_app)
    data = request.get_json(silent=True) or {}
    team_id = data.get("team_id")
    vote = data.get("vote")

    if not team_id or vote not in ("approve", "reject"):
        return _err("team_id and vote ('approve' or 'reject') are required.")

    proposal = db.execute(
        "SELECT status FROM bonus_proposals WHERE id=?", (proposal_id,)
    ).fetchone()
    if not proposal:
        return _err("Proposal not found.")
    if proposal["status"] != "pending":
        return _err(f"Proposal is already {proposal['status']}.")

    db.execute(
        """
        INSERT INTO bonus_votes (proposal_id, team_id, vote) VALUES (?, ?, ?)
        ON CONFLICT(proposal_id, team_id) DO UPDATE SET vote=excluded.vote
        """,
        (proposal_id, int(team_id), vote),
    )
    _resolve_proposal_if_majority(db, proposal_id)
    db.commit()
    return _ok()


# ---------------------------------------------------------------------------
# Export / Import  (for safe redeployment on ephemeral hosting)
# ---------------------------------------------------------------------------
@bp.get("/export")
def export_db():
    db = get_db(current_app)

    # Collect the mlbam_ids referenced anywhere in user data so we can include
    # just those player rows (avoids shipping the full ~100k-row players table).
    ref_ids = set()
    for row in db.execute(
        "SELECT mlbam_id FROM rosters"
        " UNION SELECT mlbam_id FROM draft_picks WHERE mlbam_id IS NOT NULL"
        " UNION SELECT mlbam_id FROM draft_queue"
        " UNION SELECT mlbam_id FROM trade_players"
    ).fetchall():
        ref_ids.add(row[0])

    players = []
    if ref_ids:
        placeholders = ",".join("?" * len(ref_ids))
        players = [
            dict(r)
            for r in db.execute(
                f"SELECT mlbam_id, fg_id, name_first, name_last, position, bats, throws, team, active"
                f" FROM players WHERE mlbam_id IN ({placeholders})",
                list(ref_ids),
            ).fetchall()
        ]

    def rows(table):
        return [dict(r) for r in db.execute(f"SELECT * FROM {table}").fetchall()]

    return _ok(
        exported_at=datetime.now(timezone.utc).isoformat(),
        players=players,
        teams=rows("teams"),
        rosters=rows("rosters"),
        draft_picks=rows("draft_picks"),
        draft_queue=rows("draft_queue"),
        trades=rows("trades"),
        trade_players=rows("trade_players"),
        weekly_scores=rows("weekly_scores"),
        bonus_proposals=rows("bonus_proposals"),
        bonus_votes=rows("bonus_votes"),
    )


@bp.post("/import")
def import_db():
    key = request.args.get("key", "")
    if key != current_app.config.get("SECRET_KEY", ""):
        return _err("forbidden", 403)

    raw = request.get_data(as_text=True)
    if not raw:
        return _err("JSON body required.")
    try:
        data = json.loads(raw)
    except ValueError:
        return _err("Invalid JSON body.")

    db = get_db(current_app)
    total = 0

    # Clear user tables in reverse FK order, then re-insert.
    db.execute("DELETE FROM bonus_votes")
    db.execute("DELETE FROM bonus_proposals")
    db.execute("DELETE FROM weekly_scores")
    db.execute("DELETE FROM trade_players")
    db.execute("DELETE FROM trades")
    db.execute("DELETE FROM draft_queue")
    db.execute("DELETE FROM draft_picks")
    db.execute("DELETE FROM rosters")
    db.execute("DELETE FROM teams")

    for p in data.get("players", []):
        db.execute(
            "INSERT OR REPLACE INTO players"
            " (mlbam_id, fg_id, name_first, name_last, position, bats, throws, team, active)"
            " VALUES (:mlbam_id,:fg_id,:name_first,:name_last,:position,:bats,:throws,:team,:active)",
            p,
        )
        total += 1

    for t in data.get("teams", []):
        db.execute(
            "INSERT INTO teams (id, name, owner, color) VALUES (:id,:name,:owner,:color)", t
        )
        total += 1

    for r in data.get("rosters", []):
        db.execute(
            "INSERT INTO rosters (team_id, mlbam_id, slot, added_at)"
            " VALUES (:team_id,:mlbam_id,:slot,:added_at)",
            r,
        )
        total += 1

    for p in data.get("draft_picks", []):
        db.execute(
            "INSERT INTO draft_picks"
            " (pick_number, round, pick_in_round, team_id, mlbam_id, picked_at, expires_at, autopicked)"
            " VALUES (:pick_number,:round,:pick_in_round,:team_id,:mlbam_id,:picked_at,:expires_at,:autopicked)",
            p,
        )
        total += 1

    for q in data.get("draft_queue", []):
        db.execute(
            "INSERT INTO draft_queue (team_id, mlbam_id, rank)"
            " VALUES (:team_id,:mlbam_id,:rank)",
            q,
        )
        total += 1

    for t in data.get("trades", []):
        db.execute(
            "INSERT INTO trades"
            " (id, proposed_at, resolved_at, status, proposing_team, receiving_team, effective_week)"
            " VALUES (:id,:proposed_at,:resolved_at,:status,:proposing_team,:receiving_team,:effective_week)",
            t,
        )
        total += 1

    for tp in data.get("trade_players", []):
        db.execute(
            "INSERT INTO trade_players (trade_id, mlbam_id, from_team, to_team)"
            " VALUES (:trade_id,:mlbam_id,:from_team,:to_team)",
            tp,
        )
        total += 1

    for ws in data.get("weekly_scores", []):
        db.execute(
            "INSERT INTO weekly_scores"
            " (team_id, week_number, season, points, computed_at, breakdown_json)"
            " VALUES (:team_id,:week_number,:season,:points,:computed_at,:breakdown_json)",
            ws,
        )
        total += 1

    for bp_row in data.get("bonus_proposals", []):
        db.execute(
            "INSERT INTO bonus_proposals"
            " (id, proposed_by_team, mlbam_id, points, reason, proposed_at, status, resolved_at)"
            " VALUES (:id,:proposed_by_team,:mlbam_id,:points,:reason,:proposed_at,:status,:resolved_at)",
            bp_row,
        )
        total += 1

    for bv in data.get("bonus_votes", []):
        db.execute(
            "INSERT INTO bonus_votes (proposal_id, team_id, vote)"
            " VALUES (:proposal_id,:team_id,:vote)",
            bv,
        )
        total += 1

    db.commit()
    return _ok(rows_imported=total)
