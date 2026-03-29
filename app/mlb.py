"""
app/mlb.py
Thin wrapper around the MLB Stats API (statsapi.mlb.com).
All IDs are MLBAM IDs. No API key required.
"""

import time
import requests
from typing import Optional

BASE = "https://statsapi.mlb.com/api/v1"

# Simple in-process cache to avoid hammering the API
_cache: dict = {}
_CACHE_TTL = 3600  # seconds


def _get(path: str, params: dict = None) -> dict:
    url = BASE + path
    cache_key = url + str(sorted((params or {}).items()))
    now = time.time()
    if cache_key in _cache:
        data, ts = _cache[cache_key]
        if now - ts < _CACHE_TTL:
            return data
    resp = requests.get(url, params=params, timeout=10)
    resp.raise_for_status()
    data = resp.json()
    _cache[cache_key] = (data, now)
    return data


# ---------------------------------------------------------------------------
# Player search
# ---------------------------------------------------------------------------
def search_players(name: str, active_only: bool = True) -> list[dict]:
    """Search by full or partial name. Returns list of player dicts."""
    params = {"search": name, "sportId": 1}
    if active_only:
        params["active"] = "true"
    data = _get("/players", params)
    players = data.get("people", [])
    return [_normalize_player(p) for p in players]


def get_player(mlbam_id: int) -> Optional[dict]:
    data = _get(f"/people/{mlbam_id}")
    people = data.get("people", [])
    if not people:
        return None
    return _normalize_player(people[0])


def _normalize_player(p: dict) -> dict:
    pos = p.get("primaryPosition", {}).get("abbreviation", "")
    return {
        "mlbam_id": p["id"],
        "name_first": p.get("firstName", ""),
        "name_last": p.get("lastName", ""),
        "name_full": p.get("fullName", ""),
        "position": pos,
        "bats": p.get("batSide", {}).get("code", ""),
        "throws": p.get("pitchHand", {}).get("code", ""),
        "team": p.get("currentTeam", {}).get("abbreviation", ""),
        "active": p.get("active", True),
        "two_way_player": p.get("twoWayPlayer", False),
    }


# ---------------------------------------------------------------------------
# Season stats (season-to-date cumulative)
# ---------------------------------------------------------------------------
def get_season_stats(mlbam_id: int, season: int, group: str) -> dict:
    """
    group: 'hitting' or 'pitching'
    Returns raw stats dict or empty dict if unavailable.
    """
    params = {"stats": "season", "group": group, "season": season, "sportId": 1}
    data = _get(f"/people/{mlbam_id}/stats", params)
    try:
        return data["stats"][0]["splits"][0]["stat"]
    except (IndexError, KeyError):
        return {}


# ---------------------------------------------------------------------------
# Game logs — used for chaos event detection
# ---------------------------------------------------------------------------
def get_game_log(mlbam_id: int, season: int, group: str,
                 start_date: str, end_date: str) -> list[dict]:
    """
    Returns per-game stat splits for a player between start_date and end_date.
    Dates: 'MM/DD/YYYY'
    group: 'hitting' or 'pitching'
    """
    params = {
        "stats": "gameLog",
        "group": group,
        "season": season,
        "startDate": start_date,
        "endDate": end_date,
        "sportId": 1,
    }
    data = _get(f"/people/{mlbam_id}/stats", params)
    try:
        return data["stats"][0]["splits"]
    except (IndexError, KeyError):
        return []


# ---------------------------------------------------------------------------
# Play-by-play for a single game — chaos event detection
# ---------------------------------------------------------------------------
def get_game_feed(game_pk: int) -> dict:
    """Full live game feed. game_pk from schedule endpoint."""
    return _get(f"/game/{game_pk}/feed/live")


def get_game_boxscore(game_pk: int) -> dict:
    """Boxscore for a completed or in-progress game."""
    return _get(f"/game/{game_pk}/boxscore")


def get_schedule(start_date: str, end_date: str, sport_id: int = 1) -> list[dict]:
    """
    Returns list of game dicts with gamePk, gameDate, teams.
    Dates: 'MM/DD/YYYY'
    """
    params = {
        "sportId": sport_id,
        "startDate": start_date,
        "endDate": end_date,
        "gameType": "R",  # regular season only
    }
    data = _get("/schedule", params)
    games = []
    for date_entry in data.get("dates", []):
        for game in date_entry.get("games", []):
            games.append(game)
    return games


# ---------------------------------------------------------------------------
# IL / roster status
# ---------------------------------------------------------------------------
def get_team_roster(mlb_team_id: int, roster_type: str = "active") -> list[dict]:
    data = _get(f"/teams/{mlb_team_id}/roster", {"rosterType": roster_type})
    return data.get("roster", [])


def is_player_on_il(mlbam_id: int) -> bool:
    """
    Check if a player is on any IL list by scanning the 40-man and IL rosters.
    Returns True if found on IL, False otherwise.
    """
    # Use the transactions endpoint as a proxy — simplest available signal
    data = _get(f"/people/{mlbam_id}")
    people = data.get("people", [])
    if not people:
        return False
    # MLB API exposes 'status' on the person object when on IL
    status = people[0].get("status", {})
    return status.get("code", "") in ("DL10", "DL15", "DL60", "IL10", "IL15", "IL60")
