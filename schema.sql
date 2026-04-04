-- Fantasy Baseball Schema
-- Draft-and-hold, points-based, async snake draft

PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS teams (
    id      INTEGER PRIMARY KEY,
    name    TEXT NOT NULL UNIQUE,
    owner   TEXT NOT NULL,
    color   TEXT NOT NULL DEFAULT '#e85d26'
);

CREATE TABLE IF NOT EXISTS players (
    mlbam_id    INTEGER PRIMARY KEY,
    fg_id       TEXT,
    name_first  TEXT NOT NULL,
    name_last   TEXT NOT NULL,
    name_full   TEXT GENERATED ALWAYS AS (name_first || ' ' || name_last) STORED,
    position    TEXT,   -- C, 1B, 2B, 3B, SS, OF, SP, RP
    bats        TEXT,
    throws      TEXT,
    team        TEXT,
    active      INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS rosters (
    team_id     INTEGER NOT NULL REFERENCES teams(id),
    mlbam_id    INTEGER NOT NULL REFERENCES players(mlbam_id),
    slot        TEXT NOT NULL DEFAULT 'active' CHECK(slot IN ('active', 'IL')),
    added_at    TEXT NOT NULL,
    PRIMARY KEY (team_id, mlbam_id)
);

CREATE TABLE IF NOT EXISTS draft_picks (
    pick_number     INTEGER PRIMARY KEY,
    round           INTEGER NOT NULL,
    pick_in_round   INTEGER NOT NULL,
    team_id         INTEGER NOT NULL REFERENCES teams(id),
    mlbam_id        INTEGER REFERENCES players(mlbam_id),
    picked_at       TEXT,
    expires_at      TEXT,
    autopicked      INTEGER NOT NULL DEFAULT 0,
    UNIQUE(mlbam_id)
);

CREATE TABLE IF NOT EXISTS draft_queue (
    team_id     INTEGER NOT NULL REFERENCES teams(id),
    mlbam_id    INTEGER NOT NULL REFERENCES players(mlbam_id),
    rank        INTEGER NOT NULL,
    PRIMARY KEY (team_id, mlbam_id)
);

CREATE TABLE IF NOT EXISTS stat_cache (
    mlbam_id    INTEGER NOT NULL REFERENCES players(mlbam_id),
    season      INTEGER NOT NULL,
    stat_type   TEXT NOT NULL CHECK(stat_type IN ('batting', 'pitching')),
    fetched_at  TEXT NOT NULL,
    stats_json  TEXT NOT NULL,
    PRIMARY KEY (mlbam_id, season, stat_type)
);

-- Immutable after write — week close snapshots
CREATE TABLE IF NOT EXISTS weekly_scores (
    team_id         INTEGER NOT NULL REFERENCES teams(id),
    week_number     INTEGER NOT NULL,
    season          INTEGER NOT NULL,
    points          REAL NOT NULL,
    computed_at     TEXT NOT NULL,
    breakdown_json  TEXT,   -- per-player point breakdown for UI detail view
    PRIMARY KEY (team_id, week_number, season)
);

CREATE TABLE IF NOT EXISTS trades (
    id              INTEGER PRIMARY KEY,
    proposed_at     TEXT NOT NULL,
    resolved_at     TEXT,
    status          TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','accepted','rejected')),
    proposing_team  INTEGER NOT NULL REFERENCES teams(id),
    receiving_team  INTEGER NOT NULL REFERENCES teams(id),
    effective_week  INTEGER   -- next week number when trade takes effect
);

CREATE TABLE IF NOT EXISTS trade_players (
    trade_id    INTEGER NOT NULL REFERENCES trades(id),
    mlbam_id    INTEGER NOT NULL REFERENCES players(mlbam_id),
    from_team   INTEGER NOT NULL REFERENCES teams(id),
    to_team     INTEGER NOT NULL REFERENCES teams(id)
);

-- Community bonus point proposals and votes
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

-- Indexes for common query patterns
CREATE INDEX IF NOT EXISTS idx_rosters_team       ON rosters(team_id);
CREATE INDEX IF NOT EXISTS idx_draft_picks_team   ON draft_picks(team_id);
CREATE INDEX IF NOT EXISTS idx_stat_cache_season  ON stat_cache(season);
CREATE INDEX IF NOT EXISTS idx_weekly_scores_season ON weekly_scores(season);
CREATE INDEX IF NOT EXISTS idx_players_name       ON players(name_full);
CREATE INDEX IF NOT EXISTS idx_players_position   ON players(position);
