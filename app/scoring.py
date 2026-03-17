"""
app/scoring.py
Points formula, weekly stat diffing, chaos event detection,
and weekly snapshot writer.
"""

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Optional

from flask import Flask

from app.mlb import (
    get_season_stats,
    get_game_log,
    get_schedule,
    get_game_feed,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _cfg(app: Flask) -> dict:
    return app.config["CONFIG"]


def _points_cfg(app: Flask) -> dict:
    return _cfg(app)["points"]


def _week_bounds(week_number: int, season: int) -> tuple[str, str]:
    """
    Return (start_date, end_date) strings ('MM/DD/YYYY') for a given
    fantasy week. Week 1 starts on Opening Day (approx March 20).
    We anchor to the Thursday nearest March 20 of the season.
    """
    # Approximate Opening Day — adjust if needed
    opening_day = datetime(season, 3, 27)
    start = opening_day + timedelta(weeks=week_number - 1)
    end = start + timedelta(days=6)
    fmt = "%m/%d/%Y"
    return start.strftime(fmt), end.strftime(fmt)


def _current_week(season: int) -> int:
    today = datetime.now(timezone.utc).date()
    opening_day = datetime(season, 3, 27).date()
    delta = (today - opening_day).days
    if delta < 0:
        return 0
    return delta // 7 + 1


def _db_conn(app: Flask) -> sqlite3.Connection:
    import os
    from pathlib import Path
    db_path = os.environ.get(
        "DATABASE_PATH",
        str(Path(__file__).parent.parent / "fantasy.db")
    )
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


# ---------------------------------------------------------------------------
# Base points formula
# ---------------------------------------------------------------------------
def compute_batting_points(stats: dict, pts: dict) -> float:
    """
    stats: MLB Stats API hitting stat dict (season-to-date or weekly diff).
    pts:   config points.batting dict.
    Singles must be inferred: hits - doubles - triples - homeRuns.
    """
    hits = int(stats.get("hits", 0))
    doubles = int(stats.get("doubles", 0))
    triples = int(stats.get("triples", 0))
    home_runs = int(stats.get("homeRuns", 0))
    singles = hits - doubles - triples - home_runs

    return (
        singles   * pts.get("single", 1)
        + doubles * pts.get("double", 2)
        + triples * pts.get("triple", 3)
        + home_runs * pts.get("home_run", 4)
        + int(stats.get("rbi", 0))           * pts.get("rbi", 1)
        + int(stats.get("runs", 0))           * pts.get("run", 1)
        + int(stats.get("baseOnBalls", 0))    * pts.get("walk", 1)
        + int(stats.get("stolenBases", 0))    * pts.get("stolen_base", 2)
        + int(stats.get("hitByPitch", 0))     * pts.get("hit_by_pitch", 1)
        + int(stats.get("strikeOuts", 0))     * pts.get("strikeout", -0.5)
    )


def compute_pitching_points(stats: dict, pts: dict,
                             multiplier: float = 1.0) -> float:
    """
    Innings pitched stored as float (e.g. 6.1 = 6 and 1/3 innings).
    multiplier: 2.0 for position-player pitching appearances.
    """
    ip_raw = float(stats.get("inningsPitched", 0) or 0)
    # Convert X.1 -> X + 1/3, X.2 -> X + 2/3
    ip_whole = int(ip_raw)
    ip_frac = round(ip_raw - ip_whole, 1)
    innings = ip_whole + (ip_frac / 0.3 * (1 / 3)) if ip_frac else ip_whole

    base = (
        innings                                    * pts.get("inning_pitched", 2)
        + int(stats.get("strikeOuts", 0))          * pts.get("strikeout", 1)
        + int(stats.get("wins", 0))                * pts.get("win", 4)
        + int(stats.get("saves", 0))               * pts.get("save", 5)
        + int(stats.get("holds", 0))               * pts.get("hold", 2)
        + int(stats.get("earnedRuns", 0))          * pts.get("earned_run", -2)
        + int(stats.get("baseOnBalls", 0))         * pts.get("walk_allowed", -0.5)
        + int(stats.get("hits", 0))                * pts.get("hit_allowed", -0.5)
        + int(stats.get("completeGames", 0))       * pts.get("complete_game", 3)
        + int(stats.get("shutouts", 0))            * pts.get("shutout_bonus", 3)
    )
    return base * multiplier


# ---------------------------------------------------------------------------
# Stat diffing
# ---------------------------------------------------------------------------
def _diff_stats(current: dict, previous: dict) -> dict:
    """Subtract previous snapshot from current for numeric stat fields."""
    result = {}
    all_keys = set(current) | set(previous)
    for k in all_keys:
        curr_val = current.get(k, 0)
        prev_val = previous.get(k, 0)
        try:
            result[k] = float(curr_val) - float(prev_val)
        except (TypeError, ValueError):
            result[k] = curr_val  # non-numeric fields carry forward unchanged
    return result


# ---------------------------------------------------------------------------
# Chaos event detection
# ---------------------------------------------------------------------------
def detect_chaos_events(mlbam_id: int, season: int,
                        start_date: str, end_date: str,
                        is_pitcher: bool,
                        chaos_pts: dict,
                        two_way: bool = False) -> float:
    """
    Scan game logs for a player over the week and return chaos bonus points.
    Handles: grand_slam, walk_off, stolen_base_of_home,
             no_hitter, perfect_game, immaculate_inning,
             abs_challenge, position_player_pitching multiplier bonus.
    """
    bonus = 0.0

    # Batting chaos (all position players + two-way)
    if not is_pitcher or two_way:
        game_logs = get_game_log(mlbam_id, season, "hitting", start_date, end_date)
        for split in game_logs:
            stat = split.get("stat", {})
            game_pk = split.get("game", {}).get("gamePk")

            # Grand slams
            bonus += int(stat.get("grandSlams", 0)) * chaos_pts.get("grand_slam", 8)

            # Walk-off: requires game feed
            if game_pk:
                bonus += _check_walk_off(mlbam_id, game_pk) * chaos_pts.get("walk_off", 5)
                # Stolen base of home
                bonus += _check_stolen_base_of_home(mlbam_id, game_pk) * chaos_pts.get("stolen_base_of_home", 10)
                # ABS challenges
                succ, unsucc = _check_abs_challenges(mlbam_id, game_pk)
                bonus += succ * chaos_pts.get("abs_challenge_successful", 3)
                bonus += unsucc * chaos_pts.get("abs_challenge_unsuccessful", -1)

    # Pitching chaos
    if is_pitcher or two_way:
        game_logs = get_game_log(mlbam_id, season, "pitching", start_date, end_date)
        for split in game_logs:
            stat = split.get("stat", {})
            game_pk = split.get("game", {}).get("gamePk")

            # No-hitter / perfect game (mutually exclusive bonus)
            if int(stat.get("noHitters", 0)) > 0:
                if int(stat.get("perfectGames", 0)) > 0:
                    bonus += chaos_pts.get("perfect_game", 50)
                else:
                    bonus += chaos_pts.get("no_hitter", 20)

            if game_pk:
                bonus += _check_immaculate_inning(mlbam_id, game_pk) * chaos_pts.get("immaculate_inning", 25)

    return bonus


def _check_walk_off(mlbam_id: int, game_pk: int) -> int:
    """Return 1 if this player had a walk-off PA, else 0."""
    try:
        feed = get_game_feed(game_pk)
        plays = feed.get("liveData", {}).get("plays", {}).get("allPlays", [])
        if not plays:
            return 0
        last_play = plays[-1]
        if not last_play.get("about", {}).get("isWalkOff", False):
            return 0
        batter_id = last_play.get("matchup", {}).get("batter", {}).get("id")
        return 1 if batter_id == mlbam_id else 0
    except Exception:
        return 0


def _check_stolen_base_of_home(mlbam_id: int, game_pk: int) -> int:
    """Return count of stolen bases of home for this player in this game."""
    try:
        feed = get_game_feed(game_pk)
        plays = feed.get("liveData", {}).get("plays", {}).get("allPlays", [])
        count = 0
        for play in plays:
            for event in play.get("playEvents", []):
                details = event.get("details", {})
                if details.get("eventType") == "stolen_base_home":
                    runner_id = details.get("runner", {}).get("id")
                    if runner_id == mlbam_id:
                        count += 1
        return count
    except Exception:
        return 0


def _check_abs_challenges(mlbam_id: int, game_pk: int) -> tuple[int, int]:
    """Return (successful_challenges, unsuccessful_challenges) for this batter."""
    try:
        feed = get_game_feed(game_pk)
        plays = feed.get("liveData", {}).get("plays", {}).get("allPlays", [])
        succ = unsucc = 0
        for play in plays:
            batter_id = play.get("matchup", {}).get("batter", {}).get("id")
            if batter_id != mlbam_id:
                continue
            for event in play.get("playEvents", []):
                details = event.get("details", {})
                etype = details.get("eventType", "")
                if "challenge" in etype.lower():
                    if details.get("challengeResult", "") == "overturned":
                        succ += 1
                    else:
                        unsucc += 1
        return succ, unsucc
    except Exception:
        return 0, 0


def _check_immaculate_inning(mlbam_id: int, game_pk: int) -> int:
    """
    Return 1 if this pitcher threw an immaculate inning (9 pitches, 9 Ks)
    in this game, else 0.
    """
    try:
        feed = get_game_feed(game_pk)
        plays = feed.get("liveData", {}).get("plays", {}).get("allPlays", [])

        # Group plays by inning
        from collections import defaultdict
        innings: dict = defaultdict(list)
        for play in plays:
            pitcher_id = play.get("matchup", {}).get("pitcher", {}).get("id")
            if pitcher_id != mlbam_id:
                continue
            inning = play.get("about", {}).get("inning")
            half = play.get("about", {}).get("halfInning")
            key = (inning, half)
            innings[key].append(play)

        for key, inning_plays in innings.items():
            if len(inning_plays) != 3:
                continue
            pitch_counts = []
            all_k = True
            for play in inning_plays:
                result = play.get("result", {}).get("eventType", "")
                if result != "strikeout":
                    all_k = False
                    break
                pc = play.get("pitchIndex", [])
                pitch_counts.append(len(play.get("playEvents", [])))
            if all_k and sum(pitch_counts) == 9:
                return 1
        return 0
    except Exception:
        return 0


def _check_position_player_pitching(mlbam_id: int, position: str,
                                     two_way: bool) -> bool:
    """True if this is a position player (not a pitcher, not two-way)."""
    return position not in ("SP", "RP", "P") and not two_way


# ---------------------------------------------------------------------------
# Weekly snapshot writer
# ---------------------------------------------------------------------------
def write_weekly_snapshot(app: Flask):
    """
    Called Sunday midnight by APScheduler.
    Computes points for each team for the week just completed and writes
    an immutable row to weekly_scores.
    """
    cfg = _cfg(app)
    season = cfg["season"]
    pts_cfg = _points_cfg(app)
    chaos_pts = pts_cfg["chaos"]
    week_number = _current_week(season)
    if week_number <= 0:
        return

    start_date, end_date = _week_bounds(week_number, season)
    conn = _db_conn(app)

    teams = conn.execute("SELECT id FROM teams").fetchall()
    for team_row in teams:
        team_id = team_row["id"]
        # Skip if already computed
        existing = conn.execute(
            "SELECT 1 FROM weekly_scores WHERE team_id=? AND week_number=? AND season=?",
            (team_id, week_number, season),
        ).fetchone()
        if existing:
            continue

        roster = conn.execute(
            """
            SELECT p.mlbam_id, p.position, p.throws
            FROM rosters r
            JOIN players p ON p.mlbam_id = r.mlbam_id
            WHERE r.team_id = ? AND r.slot = 'active'
            """,
            (team_id,),
        ).fetchall()

        team_points = 0.0
        breakdown = {}

        for player_row in roster:
            mlbam_id = player_row["mlbam_id"]
            position = player_row["position"] or ""
            is_pitcher = position in ("SP", "RP", "P")

            # Fetch current and previous stat snapshots
            curr_batting = _get_cached_stats(conn, mlbam_id, season, "batting")
            curr_pitching = _get_cached_stats(conn, mlbam_id, season, "pitching")

            # Pull fresh stats and update cache
            if not is_pitcher:
                fresh = get_season_stats(mlbam_id, season, "hitting")
                _update_stat_cache(conn, mlbam_id, season, "batting", fresh)
            else:
                fresh = get_season_stats(mlbam_id, season, "pitching")
                _update_stat_cache(conn, mlbam_id, season, "pitching", fresh)

            # Diff against previous snapshot
            if is_pitcher:
                prev = curr_pitching or {}
                curr = fresh
                diff = _diff_stats(curr, prev)

                # Check for position player pitching
                two_way = False  # resolved via API in enrich step
                multiplier = 1.0
                if _check_position_player_pitching(mlbam_id, position, two_way):
                    multiplier = chaos_pts.get("position_player_pitching_multiplier", 2.0)

                p_pts = compute_pitching_points(diff, pts_cfg["pitching"], multiplier)
            else:
                prev = curr_batting or {}
                curr = fresh
                diff = _diff_stats(curr, prev)
                p_pts = compute_batting_points(diff, pts_cfg["batting"])

            # Chaos events
            chaos_bonus = detect_chaos_events(
                mlbam_id, season, start_date, end_date,
                is_pitcher, chaos_pts
            )

            player_total = p_pts + chaos_bonus
            team_points += player_total
            breakdown[str(mlbam_id)] = {
                "base_pts": round(p_pts, 2),
                "chaos_pts": round(chaos_bonus, 2),
                "total": round(player_total, 2),
            }

        conn.execute(
            """
            INSERT OR IGNORE INTO weekly_scores
                (team_id, week_number, season, points, computed_at, breakdown_json)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                team_id,
                week_number,
                season,
                round(team_points, 2),
                datetime.now(timezone.utc).isoformat(),
                json.dumps(breakdown),
            ),
        )

    conn.commit()
    conn.close()


def _get_cached_stats(conn: sqlite3.Connection, mlbam_id: int,
                      season: int, stat_type: str) -> Optional[dict]:
    row = conn.execute(
        "SELECT stats_json FROM stat_cache WHERE mlbam_id=? AND season=? AND stat_type=?",
        (mlbam_id, season, stat_type),
    ).fetchone()
    if row:
        return json.loads(row["stats_json"])
    return None


def _update_stat_cache(conn: sqlite3.Connection, mlbam_id: int,
                       season: int, stat_type: str, stats: dict):
    conn.execute(
        """
        INSERT INTO stat_cache (mlbam_id, season, stat_type, fetched_at, stats_json)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(mlbam_id, season, stat_type)
        DO UPDATE SET stats_json=excluded.stats_json, fetched_at=excluded.fetched_at
        """,
        (
            mlbam_id,
            season,
            stat_type,
            datetime.now(timezone.utc).isoformat(),
            json.dumps(stats),
        ),
    )
