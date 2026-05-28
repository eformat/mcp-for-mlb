#!/usr/bin/env python3
"""Fetch 2026 MLB season data from Stats API and load into Trino Iceberg.

Pipeline: MLB Stats API → local JSON cache → pandas → Parquet → MinIO → Hive staging → CTAS Iceberg.

JSON responses are cached to data/live/{gamePk}.json so re-runs skip API calls.
Use --force-refresh to bypass the cache.

Usage:
    python scripts/load-live-trino.py
    python scripts/load-live-trino.py --force-refresh

Environment variables:
    TRINO_HOST, TRINO_PORT, MINIO_ENDPOINT, MINIO_ACCESS_KEY, MINIO_SECRET_KEY
    MLB_SEASON       Season year (default: 2026)
    MLB_START_DATE   Start date YYYY-MM-DD (default: {season}-04-01)
    MLB_END_DATE     End date YYYY-MM-DD (default: today)
    CACHE_DIR        Local JSON cache directory (default: data/live)
"""

import json
import os
import sys
import time
import tempfile
import shutil
from datetime import date, datetime
import urllib.request
import urllib.error

import pandas as pd
from minio import Minio
from minio.deleteobjects import DeleteObject
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


def _fetch_json(url, cache_path=None):
    """Fetch JSON from URL, using local cache if available."""
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
            print(f"    WARN: Failed to fetch {url} after 3 attempts: {e}", flush=True)
            return None

    if cache_path:
        os.makedirs(os.path.dirname(cache_path), exist_ok=True)
        with open(cache_path, "w") as f:
            json.dump(data, f)

    return data


def fetch_schedule(start_date, end_date):
    """Fetch all completed games in date range."""
    cache_path = os.path.join(CACHE_DIR, f"schedule_{start_date}_{end_date}.json")
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
    """Fetch live game feed, cached per gamePk."""
    cache_path = os.path.join(CACHE_DIR, f"{game_pk}.json")
    url = f"{API_BASE}/api/v1.1/game/{game_pk}/feed/live"

    if os.path.exists(cache_path) and not FORCE_REFRESH:
        with open(cache_path) as f:
            return json.load(f)

    time.sleep(REQUEST_DELAY)
    return _fetch_json(url, cache_path)


def extract_games(schedule_games):
    """Extract game-level rows from schedule data."""
    rows = []
    for g in schedule_games:
        rows.append({
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
        })
    return pd.DataFrame(rows)


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


def extract_from_feed(game_pk, feed):
    """Extract boxscore, plays, and pitches from a game feed."""
    batting_rows = []
    pitching_rows = []
    play_rows = []
    pitch_rows = []

    game_data = feed.get("gameData", {})
    weather = game_data.get("weather", {})
    weather_info = {
        "weather_condition": weather.get("condition", ""),
        "weather_temp": weather.get("temp", ""),
        "weather_wind": weather.get("wind", ""),
    }

    # Boxscore
    boxscore = feed.get("liveData", {}).get("boxscore", {}).get("teams", {})
    for side in ["away", "home"]:
        team_data = boxscore.get(side, {})
        team_id = team_data.get("team", {}).get("id")
        team_name = team_data.get("team", {}).get("name", "")
        players = team_data.get("players", {})

        for pid, pdata in players.items():
            person = pdata.get("person", {})
            player_id = person.get("id")
            player_name = person.get("fullName", "")
            bat_stats = pdata.get("stats", {}).get("batting", {})
            pitch_stats = pdata.get("stats", {}).get("pitching", {})

            if bat_stats and bat_stats.get("atBats") is not None:
                batting_rows.append({
                    "game_pk": game_pk,
                    "player_id": player_id,
                    "player_name": player_name,
                    "team_id": team_id,
                    "team_name": team_name,
                    "side": side,
                    "at_bats": _safe_int(bat_stats.get("atBats")),
                    "runs": _safe_int(bat_stats.get("runs")),
                    "hits": _safe_int(bat_stats.get("hits")),
                    "doubles": _safe_int(bat_stats.get("doubles")),
                    "triples": _safe_int(bat_stats.get("triples")),
                    "home_runs": _safe_int(bat_stats.get("homeRuns")),
                    "rbi": _safe_int(bat_stats.get("rbi")),
                    "walks": _safe_int(bat_stats.get("baseOnBalls")),
                    "strikeouts": _safe_int(bat_stats.get("strikeOuts")),
                    "stolen_bases": _safe_int(bat_stats.get("stolenBases")),
                    "caught_stealing": _safe_int(bat_stats.get("caughtStealing")),
                    "hit_by_pitch": _safe_int(bat_stats.get("hitByPitch")),
                    "sac_flies": _safe_int(bat_stats.get("sacFlies")),
                    "ground_into_dp": _safe_int(bat_stats.get("groundIntoDoublePlay")),
                    "plate_appearances": _safe_int(bat_stats.get("plateAppearances")),
                    "total_bases": _safe_int(bat_stats.get("totalBases")),
                    "avg": bat_stats.get("avg", ""),
                    "obp": bat_stats.get("obp", ""),
                    "slg": bat_stats.get("slg", ""),
                    "ops": bat_stats.get("ops", ""),
                })

            if pitch_stats and pitch_stats.get("inningsPitched") is not None:
                pitching_rows.append({
                    "game_pk": game_pk,
                    "player_id": player_id,
                    "player_name": player_name,
                    "team_id": team_id,
                    "team_name": team_name,
                    "innings_pitched": pitch_stats.get("inningsPitched", ""),
                    "hits": _safe_int(pitch_stats.get("hits")),
                    "runs": _safe_int(pitch_stats.get("runs")),
                    "earned_runs": _safe_int(pitch_stats.get("earnedRuns")),
                    "walks": _safe_int(pitch_stats.get("baseOnBalls")),
                    "strikeouts": _safe_int(pitch_stats.get("strikeOuts")),
                    "home_runs": _safe_int(pitch_stats.get("homeRuns")),
                    "pitch_count": _safe_int(pitch_stats.get("numberOfPitches")),
                    "strikes": _safe_int(pitch_stats.get("strikes")),
                    "balls": _safe_int(pitch_stats.get("balls")),
                    "era": pitch_stats.get("era", ""),
                    "whip": pitch_stats.get("whip", ""),
                    "win": pitch_stats.get("wins") == 1 if pitch_stats.get("wins") is not None else None,
                    "loss": pitch_stats.get("losses") == 1 if pitch_stats.get("losses") is not None else None,
                    "save": pitch_stats.get("saves") == 1 if pitch_stats.get("saves") is not None else None,
                    "hold": pitch_stats.get("holds") == 1 if pitch_stats.get("holds") is not None else None,
                })

    # Plays and pitches
    all_plays = feed.get("liveData", {}).get("plays", {}).get("allPlays", [])
    for play in all_plays:
        result = play.get("result", {})
        about = play.get("about", {})
        matchup = play.get("matchup", {})

        play_rows.append({
            "game_pk": game_pk,
            "at_bat_index": about.get("atBatIndex"),
            "inning": about.get("inning"),
            "half_inning": about.get("halfInning", ""),
            "is_top_inning": about.get("isTopInning"),
            "batter_id": matchup.get("batter", {}).get("id"),
            "batter_name": matchup.get("batter", {}).get("fullName", ""),
            "pitcher_id": matchup.get("pitcher", {}).get("id"),
            "pitcher_name": matchup.get("pitcher", {}).get("fullName", ""),
            "event": result.get("event", ""),
            "event_type": result.get("eventType", ""),
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
                "game_pk": game_pk,
                "at_bat_index": about.get("atBatIndex"),
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

    return batting_rows, pitching_rows, play_rows, pitch_rows, weather_info


def fetch_standings():
    """Fetch current standings."""
    today = date.today().isoformat()
    cache_path = os.path.join(CACHE_DIR, f"standings_{today}.json")
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
                "standings_date": today,
                "team_id": team.get("id"),
                "team_name": team.get("name", ""),
                "division_id": division.get("id"),
                "division_name": division.get("name", ""),
                "wins": _safe_int(lr.get("wins")),
                "losses": _safe_int(lr.get("losses")),
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


def load_table(name, df, minio_client, staging_cur, lakehouse_cur, tmpdir):
    """Write Parquet, upload to MinIO, staging table, CTAS into Iceberg."""
    if df.empty:
        print(f"  {name}: empty — skipping", flush=True)
        return 0

    print(f"  {name}: {len(df):,} rows", flush=True)

    s3_dir = f"{PARQUET_PREFIX}/{name}"
    old = list(minio_client.list_objects(BUCKET, prefix=f"{s3_dir}/", recursive=True))
    if old:
        list(minio_client.remove_objects(BUCKET, [DeleteObject(o.object_name) for o in old]))

    chunk_size = 1_000_000
    n_chunks = max(1, (len(df) + chunk_size - 1) // chunk_size)
    for i in range(n_chunks):
        chunk = df.iloc[i * chunk_size:(i + 1) * chunk_size]
        pq_name = f"{name}_{i:03d}.parquet"
        pq_path = os.path.join(tmpdir, pq_name)
        chunk.to_parquet(pq_path, index=False, engine="pyarrow")
        minio_client.fput_object(BUCKET, f"{s3_dir}/{pq_name}", pq_path)
        os.remove(pq_path)

    staging_cur.execute(f'DROP TABLE IF EXISTS staging.mlb."{name}"')
    col_defs = ", ".join(f'"{c}" {_staging_type(str(df[c].dtype))}' for c in df.columns)
    staging_cur.execute(f"""
        CREATE TABLE staging.mlb."{name}" ({col_defs})
        WITH (format = 'PARQUET', external_location = 's3://{BUCKET}/{s3_dir}/')
    """)

    lakehouse_cur.execute(f'DROP TABLE IF EXISTS lakehouse.mlb."{name}"')
    select_cols = ", ".join(_ctas_cast(c, str(df[c].dtype)) for c in df.columns)
    lakehouse_cur.execute(f"""
        CREATE TABLE lakehouse.mlb."{name}" AS
        SELECT {select_cols} FROM staging.mlb."{name}"
    """)

    lakehouse_cur.execute(f'SELECT COUNT(*) FROM lakehouse.mlb."{name}"')
    count = lakehouse_cur.fetchone()[0]
    print(f"    -> {count:,} rows loaded", flush=True)
    return count


def main():
    t0 = time.time()
    os.makedirs(CACHE_DIR, exist_ok=True)

    print(f"MLB Stats API Ingestion", flush=True)
    print(f"  Season: {MLB_SEASON}", flush=True)
    print(f"  Date range: {MLB_START_DATE} to {MLB_END_DATE}", flush=True)
    print(f"  Cache: {CACHE_DIR}", flush=True)
    print(f"  Force refresh: {FORCE_REFRESH}", flush=True)
    print(f"  Trino: {TRINO_HOST}:{TRINO_PORT}", flush=True)
    print(f"  MinIO: {MINIO_ENDPOINT}", flush=True)

    # 1. Fetch schedule
    print("\nFetching schedule...", flush=True)
    schedule_games = fetch_schedule(MLB_START_DATE, MLB_END_DATE)
    print(f"  {len(schedule_games)} completed games found", flush=True)

    if not schedule_games:
        print("No completed games — nothing to load.", flush=True)
        return

    # 2. Extract game-level data
    games_df = extract_games(schedule_games)

    # 3. Fetch and extract from each game feed
    print(f"\nProcessing {len(schedule_games)} game feeds...", flush=True)
    all_batting, all_pitching, all_plays, all_pitches = [], [], [], []
    weather_data = {}

    for i, g in enumerate(schedule_games):
        gpk = g["gamePk"]
        feed = fetch_game_feed(gpk)
        if not feed:
            continue

        batting, pitching, plays, pitches, weather_info = extract_from_feed(gpk, feed)
        all_batting.extend(batting)
        all_pitching.extend(pitching)
        all_plays.extend(plays)
        all_pitches.extend(pitches)
        weather_data[gpk] = weather_info

        if (i + 1) % 100 == 0 or i == len(schedule_games) - 1:
            cached = sum(1 for gg in schedule_games[:i+1]
                         if os.path.exists(os.path.join(CACHE_DIR, f"{gg['gamePk']}.json"))
                         and not FORCE_REFRESH)
            print(f"  {i+1}/{len(schedule_games)} games processed "
                  f"({len(all_pitches):,} pitches so far)", flush=True)

    # Add weather to games_df
    games_df["weather_condition"] = games_df["game_pk"].map(
        lambda pk: weather_data.get(pk, {}).get("weather_condition", ""))
    games_df["weather_temp"] = games_df["game_pk"].map(
        lambda pk: weather_data.get(pk, {}).get("weather_temp", ""))
    games_df["weather_wind"] = games_df["game_pk"].map(
        lambda pk: weather_data.get(pk, {}).get("weather_wind", ""))

    batting_df = pd.DataFrame(all_batting) if all_batting else pd.DataFrame()
    pitching_df = pd.DataFrame(all_pitching) if all_pitching else pd.DataFrame()
    plays_df = pd.DataFrame(all_plays) if all_plays else pd.DataFrame()
    pitches_df = pd.DataFrame(all_pitches) if all_pitches else pd.DataFrame()

    # 4. Fetch standings
    print("\nFetching standings...", flush=True)
    standings_df = fetch_standings()
    print(f"  {len(standings_df)} team records", flush=True)

    # 5. Load into Trino
    print(f"\nConnecting to Trino at {TRINO_HOST}:{TRINO_PORT}", flush=True)
    minio_client = Minio(MINIO_ENDPOINT, access_key=MINIO_ACCESS_KEY,
                         secret_key=MINIO_SECRET_KEY, secure=False)
    staging_conn = connect(host=TRINO_HOST, port=TRINO_PORT, user="admin",
                           catalog="staging", schema="mlb")
    lakehouse_conn = connect(host=TRINO_HOST, port=TRINO_PORT, user="admin",
                             catalog="lakehouse", schema="mlb")
    staging_cur = staging_conn.cursor()
    lakehouse_cur = lakehouse_conn.cursor()
    staging_cur.execute("CREATE SCHEMA IF NOT EXISTS staging.mlb")
    lakehouse_cur.execute("CREATE SCHEMA IF NOT EXISTS lakehouse.mlb")

    tmpdir = tempfile.mkdtemp(prefix="mlb-live-")
    total = 0

    print("\nLoading tables...", flush=True)
    total += load_table("live_games", games_df, minio_client, staging_cur, lakehouse_cur, tmpdir)
    total += load_table("live_boxscore_batting", batting_df, minio_client, staging_cur, lakehouse_cur, tmpdir)
    total += load_table("live_boxscore_pitching", pitching_df, minio_client, staging_cur, lakehouse_cur, tmpdir)
    total += load_table("live_plays", plays_df, minio_client, staging_cur, lakehouse_cur, tmpdir)
    total += load_table("live_pitches", pitches_df, minio_client, staging_cur, lakehouse_cur, tmpdir)
    total += load_table("live_standings", standings_df, minio_client, staging_cur, lakehouse_cur, tmpdir)

    elapsed = time.time() - t0
    print(f"\nLoaded {total:,} rows across 6 tables in {elapsed:.1f}s", flush=True)

    # Verification
    print("\nVerification:", flush=True)
    try:
        lakehouse_cur.execute("""
            SELECT game_date, away_team_name, home_team_name, away_score, home_score
            FROM lakehouse.mlb.live_games
            ORDER BY game_date DESC LIMIT 5
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
        print("  Top 5 teams by record:")
        for r in lakehouse_cur.fetchall():
            print(f"    {r[0]}: {r[1]}-{r[2]} ({r[3]}) [{r[4]}]")
    except Exception as e:
        print(f"  Standings query failed: {e}")

    shutil.rmtree(tmpdir, ignore_errors=True)
    staging_conn.close()
    lakehouse_conn.close()
    print("\nDone!", flush=True)


if __name__ == "__main__":
    main()
