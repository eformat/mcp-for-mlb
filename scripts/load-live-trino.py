#!/usr/bin/env python3
"""Fetch 2026 MLB season data from Stats API and load into Trino Iceberg.

Pipeline: MLB Stats API → JSON cache → per-game Parquet → MinIO → Hive staging → CTAS Iceberg.

Per-game Parquet files are cached locally and in MinIO. Re-runs only
fetch new games from the API and write new Parquet files. The Trino
CTAS reads all Parquet files from MinIO in one pass.

Usage:
    python scripts/load-live-trino.py              # Incremental (new games only)
    python scripts/load-live-trino.py --force-refresh  # Re-fetch all from API

Environment variables:
    TRINO_HOST, TRINO_PORT, MINIO_ENDPOINT, MINIO_ACCESS_KEY, MINIO_SECRET_KEY
    MLB_SEASON       Season year (default: 2026)
    MLB_START_DATE   Start date YYYY-MM-DD (default: {season}-04-01)
    MLB_END_DATE     End date YYYY-MM-DD (default: today)
    CACHE_DIR        Local cache directory (default: data/live)
"""

import json
import os
import sys
import time
from datetime import date
import urllib.request
import urllib.error

import pandas as pd
from minio import Minio
from trino.dbapi import connect

TRINO_HOST = os.environ.get("TRINO_HOST", "localhost")
TRINO_PORT = int(os.environ.get("TRINO_PORT", "8080"))
MINIO_ENDPOINT = os.environ.get("MINIO_ENDPOINT", "localhost:9000")
MINIO_ACCESS_KEY = os.environ.get("MINIO_ACCESS_KEY", "minio")
MINIO_SECRET_KEY = os.environ.get("MINIO_SECRET_KEY", "minio1234")

MLB_SEASON = os.environ.get("MLB_SEASON", "2026")
MLB_START_DATE = os.environ.get("MLB_START_DATE", f"{MLB_SEASON}-04-01")
MLB_END_DATE = os.environ.get("MLB_END_DATE", date.today().isoformat())
CACHE_DIR = os.environ.get("CACHE_DIR", "data/live")
FORCE_REFRESH = "--force-refresh" in sys.argv

BUCKET = "mlb-data"
PARQUET_PREFIX = "parquet"
API_BASE = "https://statsapi.mlb.com"
REQUEST_DELAY = 0.3

# Local Parquet cache
PARQUET_CACHE = os.path.join(CACHE_DIR, "parquet")

TABLES = [
    "live_games", "live_boxscore_batting", "live_boxscore_pitching",
    "live_plays", "live_pitches", "live_standings", "live_lineups", "live_elo",
]


# ---------------------------------------------------------------------------
# API fetching (with JSON cache)
# ---------------------------------------------------------------------------

def _fetch_json(url, cache_path=None):
    if cache_path and os.path.exists(cache_path) and not FORCE_REFRESH:
        with open(cache_path) as f:
            return json.load(f)

    for attempt in range(3):
        try:
            resp = urllib.request.urlopen(url, timeout=60)
            data = json.loads(resp.read())
            break
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as e:
            if attempt < 2:
                time.sleep(2 * (attempt + 1))
                continue
            print(f"    WARN: Failed to fetch {url}: {e}", flush=True)
            return None

    if cache_path:
        os.makedirs(os.path.dirname(cache_path), exist_ok=True)
        with open(cache_path, "w") as f:
            json.dump(data, f)
    return data


def fetch_schedule(start_date, end_date):
    cache_path = os.path.join(CACHE_DIR, f"schedule_{start_date}_{end_date}.json")
    # Always refresh schedule to pick up newly completed games
    if os.path.exists(cache_path):
        os.remove(cache_path)
    url = f"{API_BASE}/api/v1/schedule?sportId=1&startDate={start_date}&endDate={end_date}&gameType=R"
    data = _fetch_json(url, cache_path)
    if not data:
        return []
    games = []
    for d in data.get("dates", []):
        for g in d.get("games", []):
            if g.get("status", {}).get("abstractGameState") == "Final":
                games.append(g)
    return games


def fetch_game_feed(game_pk):
    cache_path = os.path.join(CACHE_DIR, f"{game_pk}.json")
    if os.path.exists(cache_path) and not FORCE_REFRESH:
        with open(cache_path) as f:
            return json.load(f)
    time.sleep(REQUEST_DELAY)
    return _fetch_json(f"{API_BASE}/api/v1.1/game/{game_pk}/feed/live", cache_path)


# ---------------------------------------------------------------------------
# Data extraction (unchanged from original)
# ---------------------------------------------------------------------------

def _safe_int(v):
    if v is None or v == "":
        return None
    try:
        return int(v)
    except (ValueError, TypeError):
        return None


def _safe_float(v):
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (ValueError, TypeError):
        return None


def extract_game_row(g):
    return {
        "game_pk": g["gamePk"],
        "game_date": g.get("officialDate", g.get("gameDate", "")[:10]),
        "season": g.get("season", MLB_SEASON),
        "away_team_id": g["teams"]["away"]["team"]["id"],
        "away_team_name": g["teams"]["away"]["team"]["name"],
        "home_team_id": g["teams"]["home"]["team"]["id"],
        "home_team_name": g["teams"]["home"]["team"]["name"],
        "away_score": g["teams"]["away"].get("score"),
        "home_score": g["teams"]["home"].get("score"),
        "game_status": g["status"].get("detailedState", ""),
        "venue_name": g.get("venue", {}).get("name", ""),
        "venue_id": g.get("venue", {}).get("id"),
        "day_night": g.get("dayNight", ""),
        "game_type": g.get("gameType", ""),
        "scheduled_innings": g.get("scheduledInnings", 9),
    }


def extract_from_feed(game_pk, feed):
    batting_rows, pitching_rows, play_rows, pitch_rows, lineup_rows = [], [], [], [], []

    game_data = feed.get("gameData", {})
    weather = game_data.get("weather", {})
    weather_info = {
        "weather_condition": weather.get("condition", ""),
        "weather_temp": weather.get("temp", ""),
        "weather_wind": weather.get("wind", ""),
    }

    boxscore = feed.get("liveData", {}).get("boxscore", {}).get("teams", {})
    for side in ["away", "home"]:
        team_data = boxscore.get(side, {})
        team_id = team_data.get("team", {}).get("id")
        team_name = team_data.get("team", {}).get("name", "")
        for pid, pdata in team_data.get("players", {}).items():
            person = pdata.get("person", {})
            player_id = person.get("id")
            player_name = person.get("fullName", "")
            bat = pdata.get("stats", {}).get("batting", {})
            pit = pdata.get("stats", {}).get("pitching", {})

            if bat and bat.get("atBats") is not None:
                batting_rows.append({
                    "game_pk": game_pk, "player_id": player_id, "player_name": player_name,
                    "team_id": team_id, "team_name": team_name, "side": side,
                    "at_bats": _safe_int(bat.get("atBats")), "runs": _safe_int(bat.get("runs")),
                    "hits": _safe_int(bat.get("hits")), "doubles": _safe_int(bat.get("doubles")),
                    "triples": _safe_int(bat.get("triples")), "home_runs": _safe_int(bat.get("homeRuns")),
                    "rbi": _safe_int(bat.get("rbi")), "walks": _safe_int(bat.get("baseOnBalls")),
                    "strikeouts": _safe_int(bat.get("strikeOuts")),
                    "stolen_bases": _safe_int(bat.get("stolenBases")),
                    "caught_stealing": _safe_int(bat.get("caughtStealing")),
                    "hit_by_pitch": _safe_int(bat.get("hitByPitch")),
                    "sac_flies": _safe_int(bat.get("sacFlies")),
                    "ground_into_dp": _safe_int(bat.get("groundIntoDoublePlay")),
                    "plate_appearances": _safe_int(bat.get("plateAppearances")),
                    "total_bases": _safe_int(bat.get("totalBases")),
                    "avg": bat.get("avg", ""), "obp": bat.get("obp", ""),
                    "slg": bat.get("slg", ""), "ops": bat.get("ops", ""),
                })

            if pit and pit.get("inningsPitched") is not None:
                pitching_rows.append({
                    "game_pk": game_pk, "player_id": player_id, "player_name": player_name,
                    "team_id": team_id, "team_name": team_name,
                    "innings_pitched": pit.get("inningsPitched", ""),
                    "hits": _safe_int(pit.get("hits")), "runs": _safe_int(pit.get("runs")),
                    "earned_runs": _safe_int(pit.get("earnedRuns")),
                    "walks": _safe_int(pit.get("baseOnBalls")),
                    "strikeouts": _safe_int(pit.get("strikeOuts")),
                    "home_runs": _safe_int(pit.get("homeRuns")),
                    "pitch_count": _safe_int(pit.get("numberOfPitches")),
                    "strikes": _safe_int(pit.get("strikes")),
                    "balls": _safe_int(pit.get("balls")),
                    "era": pit.get("era", ""), "whip": pit.get("whip", ""),
                    "win": pit.get("wins") == 1 if pit.get("wins") is not None else None,
                    "loss": pit.get("losses") == 1 if pit.get("losses") is not None else None,
                    "save": pit.get("saves") == 1 if pit.get("saves") is not None else None,
                    "hold": pit.get("holds") == 1 if pit.get("holds") is not None else None,
                })

    all_plays = feed.get("liveData", {}).get("plays", {}).get("allPlays", [])
    for play in all_plays:
        result = play.get("result", {})
        about = play.get("about", {})
        matchup = play.get("matchup", {})

        play_rows.append({
            "game_pk": game_pk, "at_bat_index": about.get("atBatIndex"),
            "inning": about.get("inning"), "half_inning": about.get("halfInning", ""),
            "is_top_inning": about.get("isTopInning"),
            "batter_id": matchup.get("batter", {}).get("id"),
            "batter_name": matchup.get("batter", {}).get("fullName", ""),
            "pitcher_id": matchup.get("pitcher", {}).get("id"),
            "pitcher_name": matchup.get("pitcher", {}).get("fullName", ""),
            "event": result.get("event", ""), "event_type": result.get("eventType", ""),
            "description": (result.get("description", "") or "")[:500],
            "rbi": _safe_int(result.get("rbi")),
            "away_score": _safe_int(result.get("awayScore")),
            "home_score": _safe_int(result.get("homeScore")),
            "is_scoring_play": about.get("isScoringPlay"),
            "is_out": result.get("isOut"),
        })

        for event in play.get("playEvents", []):
            if not event.get("isPitch"):
                continue
            details = event.get("details", {})
            pitch_data = event.get("pitchData", {})
            coords = pitch_data.get("coordinates", {})
            breaks = pitch_data.get("breaks", {})
            count = event.get("count", {})
            pitch_rows.append({
                "game_pk": game_pk, "at_bat_index": about.get("atBatIndex"),
                "pitch_number": event.get("pitchNumber"),
                "pitcher_id": matchup.get("pitcher", {}).get("id"),
                "batter_id": matchup.get("batter", {}).get("id"),
                "pitch_type": details.get("type", {}).get("code", ""),
                "pitch_description": details.get("type", {}).get("description", ""),
                "call_code": details.get("call", {}).get("code", ""),
                "call_description": details.get("call", {}).get("description", ""),
                "start_speed": _safe_float(pitch_data.get("startSpeed")),
                "end_speed": _safe_float(pitch_data.get("endSpeed")),
                "spin_rate": _safe_float(breaks.get("spinRate")),
                "spin_direction": _safe_float(breaks.get("spinDirection")),
                "break_angle": _safe_float(breaks.get("breakAngle")),
                "break_length": _safe_float(breaks.get("breakLength")),
                "plate_x": _safe_float(coords.get("pX")),
                "plate_z": _safe_float(coords.get("pZ")),
                "pfx_x": _safe_float(coords.get("pfxX")),
                "pfx_z": _safe_float(coords.get("pfxZ")),
                "vx0": _safe_float(coords.get("vX0")),
                "vy0": _safe_float(coords.get("vY0")),
                "vz0": _safe_float(coords.get("vZ0")),
                "zone": _safe_int(pitch_data.get("zone")),
                "is_strike": details.get("isStrike"),
                "is_ball": details.get("isBall"),
                "is_in_play": details.get("isInPlay"),
                "balls": _safe_int(count.get("balls")),
                "strikes": _safe_int(count.get("strikes")),
                "outs": _safe_int(count.get("outs")),
                "inning": about.get("inning"),
                "half_inning": about.get("halfInning", ""),
            })

    for side in ["away", "home"]:
        team_data = boxscore.get(side, {})
        batting_order = team_data.get("battingOrder", [])
        for position, player_id in enumerate(batting_order, 1):
            pid_key = f"ID{player_id}"
            player_info = game_data.get("players", {}).get(pid_key, {})
            lineup_rows.append({
                "game_pk": game_pk,
                "side": side,
                "lineup_position": position,
                "player_id": player_id,
                "player_name": player_info.get("fullName", ""),
                "primary_position": player_info.get("primaryPosition", {}).get("abbreviation", ""),
            })

    return batting_rows, pitching_rows, play_rows, pitch_rows, lineup_rows, weather_info


def fetch_standings():
    today = date.today().isoformat()
    cache_path = os.path.join(CACHE_DIR, f"standings_{today}.json")
    if os.path.exists(cache_path):
        os.remove(cache_path)
    url = f"{API_BASE}/api/v1/standings?leagueId=103,104&season={MLB_SEASON}"
    data = _fetch_json(url, cache_path)
    if not data:
        return pd.DataFrame()
    rows = []
    for record in data.get("records", []):
        division = record.get("division", {})
        for tr in record.get("teamRecords", []):
            team = tr.get("team", {})
            lr = tr.get("leagueRecord", {})
            rows.append({
                "standings_date": today, "team_id": team.get("id"),
                "team_name": team.get("name", ""),
                "division_id": division.get("id"),
                "division_name": division.get("name", ""),
                "wins": _safe_int(lr.get("wins")), "losses": _safe_int(lr.get("losses")),
                "winning_pct": lr.get("pct", ""),
                "games_back": tr.get("gamesBack", ""),
                "wild_card_games_back": tr.get("wildCardGamesBack", ""),
                "runs_scored": _safe_int(tr.get("runsScored")),
                "runs_allowed": _safe_int(tr.get("runsAllowed")),
                "run_differential": _safe_int(tr.get("runDifferential")),
                "streak": tr.get("streak", {}).get("streakCode", ""),
                "division_rank": tr.get("divisionRank", ""),
                "league_rank": tr.get("leagueRank", ""),
            })
    return pd.DataFrame(rows)


def compute_elo_ratings(schedule_games):
    """Compute ELO ratings from completed game results, chronologically."""
    K = 6
    HOME_ADV = 24

    teams = set()
    games = []
    for g in schedule_games:
        away = g["teams"]["away"]["team"]["name"]
        home = g["teams"]["home"]["team"]["name"]
        away_score = g["teams"]["away"].get("score")
        home_score = g["teams"]["home"].get("score")
        if away_score is None or home_score is None:
            continue
        teams.add(away)
        teams.add(home)
        games.append({
            "date": g.get("officialDate", ""),
            "away": away, "home": home,
            "away_score": int(away_score), "home_score": int(home_score),
        })

    games.sort(key=lambda x: x["date"])
    ratings = {t: 1500.0 for t in teams}
    games_played = {t: 0 for t in teams}

    for g in games:
        ra, rh = ratings[g["away"]], ratings[g["home"]]
        ea = 1.0 / (1.0 + 10.0 ** ((rh + HOME_ADV - ra) / 400.0))
        sa = 1.0 if g["away_score"] > g["home_score"] else 0.0
        mov = abs(g["away_score"] - g["home_score"])
        mov_mult = max(1.0, (mov + 1) ** 0.3)
        ratings[g["away"]] += K * mov_mult * (sa - ea)
        ratings[g["home"]] += K * mov_mult * ((1 - sa) - (1 - ea))
        games_played[g["away"]] += 1
        games_played[g["home"]] += 1

    rows = []
    for team in sorted(teams):
        rows.append({
            "team_name": team,
            "elo_rating": round(ratings[team], 1),
            "games_played": games_played[team],
        })
    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# Per-game Parquet writing (local cache + MinIO upload)
# ---------------------------------------------------------------------------

def _write_game_parquet(game_pk, table_name, df):
    """Write per-game Parquet to local cache. Returns path."""
    table_dir = os.path.join(PARQUET_CACHE, table_name)
    os.makedirs(table_dir, exist_ok=True)
    path = os.path.join(table_dir, f"{game_pk}.parquet")
    df.to_parquet(path, index=False, engine="pyarrow")
    return path


def _game_parquet_exists(game_pk, table_name):
    """Check if per-game Parquet already exists locally."""
    return os.path.exists(os.path.join(PARQUET_CACHE, table_name, f"{game_pk}.parquet"))


def _sync_parquet_to_minio(minio_client, table_name):
    """Upload any local Parquet files not yet in MinIO."""
    s3_dir = f"{PARQUET_PREFIX}/{table_name}"
    local_dir = os.path.join(PARQUET_CACHE, table_name)
    if not os.path.isdir(local_dir):
        return 0

    # Get existing S3 keys
    existing = set()
    for obj in minio_client.list_objects(BUCKET, prefix=f"{s3_dir}/", recursive=True):
        existing.add(os.path.basename(obj.object_name))

    uploaded = 0
    for fname in sorted(os.listdir(local_dir)):
        if not fname.endswith(".parquet"):
            continue
        if fname in existing:
            continue
        local_path = os.path.join(local_dir, fname)
        minio_client.fput_object(BUCKET, f"{s3_dir}/{fname}", local_path)
        uploaded += 1

    return uploaded


def _staging_type(dtype):
    if dtype == "float64":
        return "DOUBLE"
    if dtype == "int64":
        return "BIGINT"
    if dtype == "bool":
        return "BOOLEAN"
    return "VARCHAR"


def _ctas_cast(col, dtype):
    if dtype == "float64":
        return f'TRY_CAST("{col}" AS DOUBLE) AS "{col}"'
    if dtype == "int64":
        return f'TRY_CAST("{col}" AS INTEGER) AS "{col}"'
    if dtype == "bool":
        return f'TRY_CAST("{col}" AS BOOLEAN) AS "{col}"'
    return f'CAST("{col}" AS VARCHAR) AS "{col}"'


def rebuild_iceberg_table(table_name, minio_client, staging_cur, lakehouse_cur):
    """Recreate Iceberg table from all Parquet files in MinIO."""
    s3_dir = f"{PARQUET_PREFIX}/{table_name}"

    # Read multiple sample Parquet files to detect widest types
    # (some games have NULLs which pandas writes as float64 vs int64)
    local_dir = os.path.join(PARQUET_CACHE, table_name)
    sample_files = [f for f in os.listdir(local_dir) if f.endswith(".parquet")] if os.path.isdir(local_dir) else []
    if not sample_files:
        print(f"  {table_name}: no Parquet files — skipping", flush=True)
        return 0

    # Merge schemas from several files to get the widest type per column
    import pyarrow as pa
    import pyarrow.parquet as pq
    schemas = []
    for f in sample_files[:20]:
        schemas.append(pq.read_schema(os.path.join(local_dir, f)))
    merged = pa.unify_schemas(schemas, promote_options="permissive")
    sample_df = pd.DataFrame({f.name: pd.Series(dtype="object") for f in merged})
    # Map arrow types to pandas-like dtype strings for staging
    # Use DOUBLE for all numeric columns — per-game Parquet files have
    # inconsistent int64/float64 types depending on whether NULLs are present.
    # DOUBLE safely reads both. We cast to INTEGER in the CTAS.
    arrow_to_staging = {}
    for field in merged:
        atype = str(field.type)
        if atype in ("double", "float", "int64", "int32"):
            arrow_to_staging[field.name] = "DOUBLE"
        elif atype == "bool":
            arrow_to_staging[field.name] = "BOOLEAN"
        else:
            arrow_to_staging[field.name] = "VARCHAR"

    staging_cur.execute(f'DROP TABLE IF EXISTS staging.mlb."{table_name}"')
    col_defs = ", ".join(f'"{c}" {arrow_to_staging[c]}' for c in arrow_to_staging)
    staging_cur.execute(f"""
        CREATE TABLE staging.mlb."{table_name}" ({col_defs})
        WITH (format = 'PARQUET', external_location = 's3://{BUCKET}/{s3_dir}/')
    """)

    lakehouse_cur.execute(f'DROP TABLE IF EXISTS lakehouse.mlb."{table_name}"')
    ctas_cols = []
    for c, stype in arrow_to_staging.items():
        if stype in ("DOUBLE", "BOOLEAN"):
            ctas_cols.append(f'"{c}"')
        else:
            ctas_cols.append(f'CAST("{c}" AS VARCHAR) AS "{c}"')
    lakehouse_cur.execute(f"""
        CREATE TABLE lakehouse.mlb."{table_name}" AS
        SELECT {", ".join(ctas_cols)} FROM staging.mlb."{table_name}"
    """)

    lakehouse_cur.execute(f'SELECT COUNT(*) FROM lakehouse.mlb."{table_name}"')
    count = lakehouse_cur.fetchone()[0]
    return count


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    t0 = time.time()
    os.makedirs(CACHE_DIR, exist_ok=True)
    os.makedirs(PARQUET_CACHE, exist_ok=True)

    print(f"MLB Stats API Ingestion (incremental)", flush=True)
    print(f"  Season: {MLB_SEASON}  Range: {MLB_START_DATE} to {MLB_END_DATE}", flush=True)
    print(f"  Cache: {CACHE_DIR}  Parquet: {PARQUET_CACHE}", flush=True)
    print(f"  Trino: {TRINO_HOST}:{TRINO_PORT}  MinIO: {MINIO_ENDPOINT}", flush=True)

    # 1. Fetch schedule
    print("\nFetching schedule...", flush=True)
    schedule_games = fetch_schedule(MLB_START_DATE, MLB_END_DATE)
    print(f"  {len(schedule_games)} completed games", flush=True)

    if not schedule_games:
        print("No completed games.", flush=True)
        return

    # 2. Find games that need processing (no local Parquet yet)
    new_game_pks = set()
    for g in schedule_games:
        gpk = g["gamePk"]
        if not _game_parquet_exists(gpk, "live_games") or not _game_parquet_exists(gpk, "live_lineups") or FORCE_REFRESH:
            new_game_pks.add(gpk)

    print(f"  {len(new_game_pks)} new games to process, {len(schedule_games) - len(new_game_pks)} cached", flush=True)

    # 3. Process new games — fetch JSON, extract, write per-game Parquet
    if new_game_pks:
        new_games = [g for g in schedule_games if g["gamePk"] in new_game_pks]
        print(f"\nProcessing {len(new_games)} new game feeds...", flush=True)

        for i, g in enumerate(new_games):
            gpk = g["gamePk"]
            feed = fetch_game_feed(gpk)
            if not feed:
                continue

            batting, pitching, plays, pitches, lineups, weather_info = extract_from_feed(gpk, feed)

            # Game row with weather
            game_row = extract_game_row(g)
            game_row.update(weather_info)
            _write_game_parquet(gpk, "live_games", pd.DataFrame([game_row]))

            if batting:
                _write_game_parquet(gpk, "live_boxscore_batting", pd.DataFrame(batting))
            if pitching:
                _write_game_parquet(gpk, "live_boxscore_pitching", pd.DataFrame(pitching))
            if plays:
                _write_game_parquet(gpk, "live_plays", pd.DataFrame(plays))
            if pitches:
                _write_game_parquet(gpk, "live_pitches", pd.DataFrame(pitches))
            if lineups:
                _write_game_parquet(gpk, "live_lineups", pd.DataFrame(lineups))

            if (i + 1) % 50 == 0 or i == len(new_games) - 1:
                print(f"  {i+1}/{len(new_games)} games processed", flush=True)

    # 4. Standings (always refresh)
    print("\nFetching standings...", flush=True)
    standings_df = fetch_standings()
    if not standings_df.empty:
        standings_dir = os.path.join(PARQUET_CACHE, "live_standings")
        os.makedirs(standings_dir, exist_ok=True)
        # Standings is a single file, always overwritten
        standings_df.to_parquet(os.path.join(standings_dir, "standings.parquet"), index=False, engine="pyarrow")
    print(f"  {len(standings_df)} team records", flush=True)

    # 4b. Compute ELO ratings from game results
    print("\nComputing ELO ratings...", flush=True)
    elo_df = compute_elo_ratings(schedule_games)
    if not elo_df.empty:
        elo_dir = os.path.join(PARQUET_CACHE, "live_elo")
        os.makedirs(elo_dir, exist_ok=True)
        elo_df.to_parquet(os.path.join(elo_dir, "elo.parquet"), index=False, engine="pyarrow")
    print(f"  {len(elo_df)} team ELO ratings", flush=True)

    # 5. Sync Parquet to MinIO (upload only new files)
    print("\nSyncing Parquet to MinIO...", flush=True)
    minio_client = Minio(MINIO_ENDPOINT, access_key=MINIO_ACCESS_KEY,
                         secret_key=MINIO_SECRET_KEY, secure=False)
    total_uploaded = 0
    from minio.deleteobjects import DeleteObject
    for table in TABLES:
        if table in ("live_standings", "live_elo"):
            s3_dir = f"{PARQUET_PREFIX}/{table}"
            old = list(minio_client.list_objects(BUCKET, prefix=f"{s3_dir}/", recursive=True))
            if old:
                list(minio_client.remove_objects(BUCKET, [DeleteObject(o.object_name) for o in old]))
            fname = "standings.parquet" if table == "live_standings" else "elo.parquet"
            local_path = os.path.join(PARQUET_CACHE, table, fname)
            if os.path.exists(local_path):
                minio_client.fput_object(BUCKET, f"{s3_dir}/{fname}", local_path)
                total_uploaded += 1
        else:
            uploaded = _sync_parquet_to_minio(minio_client, table)
            total_uploaded += uploaded
    print(f"  Uploaded {total_uploaded} new Parquet files", flush=True)

    # 6. Rebuild Iceberg tables from staging
    print(f"\nRebuilding Iceberg tables...", flush=True)
    staging_conn = connect(host=TRINO_HOST, port=TRINO_PORT, user="admin",
                           catalog="staging", schema="mlb")
    lakehouse_conn = connect(host=TRINO_HOST, port=TRINO_PORT, user="admin",
                             catalog="lakehouse", schema="mlb")
    staging_cur = staging_conn.cursor()
    lakehouse_cur = lakehouse_conn.cursor()
    staging_cur.execute("CREATE SCHEMA IF NOT EXISTS staging.mlb")
    lakehouse_cur.execute("CREATE SCHEMA IF NOT EXISTS lakehouse.mlb")

    total_rows = 0
    for table in TABLES:
        count = rebuild_iceberg_table(table, minio_client, staging_cur, lakehouse_cur)
        print(f"  {table}: {count:,} rows", flush=True)
        total_rows += count

    elapsed = time.time() - t0
    print(f"\nLoaded {total_rows:,} rows across {len(TABLES)} tables in {elapsed:.1f}s", flush=True)

    # Verification
    print("\nVerification:", flush=True)
    try:
        lakehouse_cur.execute("""
            SELECT game_date, away_team_name, home_team_name, away_score, home_score
            FROM lakehouse.mlb.live_games ORDER BY game_date DESC LIMIT 5
        """)
        print("  Last 5 games:")
        for r in lakehouse_cur.fetchall():
            print(f"    {r[0]}: {r[1]} {r[3]} @ {r[2]} {r[4]}")
    except Exception as e:
        print(f"  Games query failed: {e}")

    try:
        lakehouse_cur.execute("""
            SELECT team_name, wins, losses, winning_pct, division_name
            FROM lakehouse.mlb.live_standings
            ORDER BY CAST(winning_pct AS DOUBLE) DESC LIMIT 5
        """)
        print("  Top 5 teams:")
        for r in lakehouse_cur.fetchall():
            print(f"    {r[0]}: {r[1]}-{r[2]} ({r[3]}) [{r[4]}]")
    except Exception as e:
        print(f"  Standings query failed: {e}")

    staging_conn.close()
    lakehouse_conn.close()
    print("\nDone!", flush=True)


if __name__ == "__main__":
    main()
