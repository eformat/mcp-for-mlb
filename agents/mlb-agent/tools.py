"""LangChain tools for MLB baseball data.

All data access goes through Trino (Iceberg lakehouse on MinIO S3).
Metadata tools return hardcoded domain knowledge.
SpiceDB provides fine-grained permission checks on datasets.
"""

import json
import os
import re

import grpc

from langchain_core.tools import tool
from authzed.api.v1 import Client as SpiceDBClient

TRINO_HOST = os.environ.get("TRINO_QUERY_HOST", "trino")
TRINO_PORT = int(os.environ.get("TRINO_QUERY_PORT", "8080"))

_BLOCKED_SQL = re.compile(
    r"\b(INSERT|UPDATE|DELETE|DROP|ALTER|CREATE|TRUNCATE|MERGE|GRANT|REVOKE)\b",
    re.IGNORECASE,
)

_DATASET_ALIASES = {
    "hitting": "batting", "batting": "batting", "offense": "batting",
    "pitching": "pitching", "throwing": "pitching",
    "fielding": "fielding", "defense": "fielding",
    "teams": "teams", "team": "teams",
    "awards": "awards", "mvp": "awards", "cy young": "awards",
    "hall of fame": "hall_of_fame", "hof": "hall_of_fame",
    "salaries": "salaries", "salary": "salaries",
    "weather": "weather", "temperature": "weather",
    "parks": "parks", "stadiums": "parks", "ballparks": "parks",
    "postseason": "postseason", "playoffs": "postseason", "world series": "postseason",
}


@tool
def query_trino(sql: str) -> str:
    """Execute a read-only SQL query against the MLB Iceberg lakehouse in Trino.
    USE THIS TOOL for ALL data questions.

    Schema: lakehouse.mlb

    Main tables:
    - batting (playerID, yearID, stint, teamID, lgID, G, AB, R, H, doubles, triples, HR, RBI, SB, CS, BB, SO, IBB, HBP, SH, SF, GIDP)
    - pitching (playerID, yearID, stint, teamID, lgID, W, L, G, GS, CG, SHO, SV, IPouts, H, ER, HR, BB, SO, BAOpp, ERA)
    - fielding (playerID, yearID, stint, teamID, lgID, POS, G, GS, InnOuts, PO, A, E, DP)
    - people (playerID, nameFirst, nameLast, birthYear, weight, height, bats, throws, debut, finalGame)
    - teams (yearID, lgID, teamID, franchID, divID, Rank, G, W, L, DivWin, WCWin, LgWin, WSWin, R, AB, H, HR, ERA, name, park, attendance)
    - parks (parkkey, parkname, city, state, country)
    - series_post (yearID, round, teamIDwinner, teamIDloser, wins, losses, ties)
    - batting_post, pitching_post, fielding_post (same cols + round)
    - awards_players (playerID, awardID, yearID, lgID)
    - awards_share_players (awardID, yearID, lgID, playerID, pointsWon, pointsMax, votesFirst)
    - hall_of_fame (playerID, yearid, votedBy, ballots, needed, votes, inducted, category)
    - salaries (yearID, teamID, lgID, playerID, salary) — 1985-2016 only
    - home_games (yearkey, teamkey, parkkey, games, attendance, spanfirst, spanlast)
    - weather_stations (station_id, city_name, latitude, longitude)
    - weather_daily (station_id, observation_date, tmax, tmin, prcp)
    - pitch_pitches (ab_id, pitch_num, pitch_type, start_speed, spin_rate, pfx_x, pfx_z, px, pz, zone, type, code) — 3.6M pitches 2015-2019
    - pitch_atbats (ab_id, g_id, batter_id, pitcher_id, event, inning, stand, p_throws) — 926K at-bats
    - pitch_games (g_id, date, home_team, away_team, venue_name, weather, wind, attendance) — 12K games
    - pitch_player_names (id, first_name, last_name) — player ID lookup
    - statcast_pitches (game_pk, pitcher, batter, player_name, pitch_type, pitch_name, release_speed, release_spin_rate, launch_speed, launch_angle, bat_speed) — 27K pitches 2024-2025 postseason

    Computed stats (not stored):
    - AVG = CAST(H AS DOUBLE)/NULLIF(AB,0)
    - ERA from raw = CAST(ER AS DOUBLE)*27/NULLIF(IPouts,0)
    - Innings = CAST(IPouts AS DOUBLE)/3
    - OBP = CAST(H+BB+HBP AS DOUBLE)/NULLIF(AB+BB+HBP+SF,0)
    - SLG = CAST(H-doubles-triples-HR+2*doubles+3*triples+4*HR AS DOUBLE)/NULLIF(AB,0)

    Only SELECT queries allowed.
    """
    if _BLOCKED_SQL.search(sql):
        return json.dumps({"error": "Only SELECT queries allowed."})

    try:
        from trino.dbapi import connect as trino_connect

        conn = trino_connect(
            host=TRINO_HOST, port=TRINO_PORT, user="admin",
            catalog="lakehouse", schema="mlb",
        )
        cur = conn.cursor()
        cur.execute(sql)
        columns = [desc[0] for desc in cur.description] if cur.description else []
        rows = cur.fetchmany(1000)
        conn.close()

        results = [dict(zip(columns, row)) for row in rows]

        return json.dumps({
            "results": results,
            "row_count": len(results),
            "sql_executed": sql,
            "methodology": "Lahman Baseball Database — official MLB game records and box scores. Season-level aggregates.",
            "caveats": [
                "Statistics are season-level aggregates, no game-by-game data.",
                "Negro League data may be incomplete.",
                "Era context matters for cross-era comparisons.",
            ],
        })
    except Exception as exc:
        return json.dumps({"error": str(exc), "sql_executed": sql})


@tool
def describe_datasets(topic: str = "") -> str:
    """List available MLB datasets and their characteristics.

    Args:
        topic: Filter by 'batting', 'pitching', 'fielding', 'postseason', 'awards', 'teams', 'weather', or 'all'.
    """
    datasets = {
        "batting": {"tables": ["batting", "batting_post"], "years": "1871-2025", "notes": "Season batting stats. Compute AVG, OBP, SLG in SQL."},
        "pitching": {"tables": ["pitching", "pitching_post"], "years": "1871-2025", "notes": "IPouts/3 = innings pitched. ERA stored directly."},
        "fielding": {"tables": ["fielding", "fielding_post", "fielding_of", "fielding_of_split"], "years": "1871-2025", "notes": "Position-specific stats."},
        "teams": {"tables": ["teams", "teams_franchises"], "years": "1871-2025", "notes": "Season standings, aggregate stats, park factors."},
        "postseason": {"tables": ["series_post", "batting_post", "pitching_post", "fielding_post"], "years": "1884-2025", "notes": "Round codes: WS, ALCS, NLCS, ALDS, NLDS."},
        "awards": {"tables": ["awards_players", "awards_share_players"], "years": "1877-2025", "notes": "MVP, Cy Young, Gold Glove, etc."},
        "hall_of_fame": {"tables": ["hall_of_fame"], "years": "1936-2025", "notes": "Complete voting history."},
        "weather": {"tables": ["weather_stations", "weather_daily"], "years": "~1872-2019", "notes": "Daily tmax/tmin/prcp for 210 US cities."},
        "parks": {"tables": ["parks", "home_games"], "years": "1871-2025", "notes": "346 ballparks with attendance data."},
        "salaries": {"tables": ["salaries"], "years": "1985-2016", "notes": "Annual salary in USD. Data ends 2016."},
        "pitch": {"tables": ["pitch_pitches", "pitch_atbats", "pitch_games", "pitch_player_names", "statcast_pitches"], "years": "2015-2019, 2024-2025 postseason", "notes": "Pitch-level: velocity, spin, movement, location. Join via ab_id/g_id."},
    }
    topic_lower = topic.strip().lower() if topic else "all"
    resolved = _DATASET_ALIASES.get(topic_lower, topic_lower)
    if resolved not in ("all", ""):
        datasets = {k: v for k, v in datasets.items() if k == resolved or topic_lower in k}
        if not datasets:
            datasets = {k: v for k, v in datasets.items()}

    return json.dumps({
        "datasets": datasets,
        "join_key": "playerID links most tables. teamID+yearID for team context.",
    })


@tool
def get_methodology(dataset_name: str) -> str:
    """Get detailed methodology for a specific MLB dataset.

    Args:
        dataset_name: Dataset name (e.g., 'batting', 'pitching', 'weather', 'awards', 'postseason').
    """
    resolved = _DATASET_ALIASES.get(dataset_name.strip().lower(), dataset_name.strip().lower())
    methodologies = {
        "batting": "Official MLB box scores, season-level per player-team-stint. Dead-ball era (pre-1920), steroid era (~1993-2004), Negro League data added 2024.",
        "pitching": "Official MLB records. IPouts = outs recorded. Mound lowered 1969. DH: AL 1973, NL 2022. Relief role evolved 1980s-90s.",
        "fielding": "Official fielding records. Error scoring stricter over time. No advanced metrics (UZR/DRS/OAA).",
        "teams": "Season team aggregates 1871-2025. Strike seasons: 1981, 1994-95. COVID 60-game season 2020.",
        "awards": "Awards from 1877+. MVP since 1931, Cy Young since 1956. Vote share data available.",
        "hall_of_fame": "BBWAA voting + committee selections. 10-year eligibility rule. PED era voter backlash.",
        "salaries": "1985-2016 only. No inflation adjustment. Excludes bonuses and deferred pay.",
        "weather": "NOAA GHCN-D daily data ~1872-2019 for 210 US cities. Gaps exist. Trace precip = 0.",
        "parks": "346 ballparks 1871-present. Park factors (BPF/PPF) in teams table.",
        "postseason": "All playoff/WS data. Format expanded over time. Small sample sizes.",
        "pitch": "Statcast pitch-by-pitch data. 2015-2019 regular+post (3.6M pitches), 2024-2025 postseason only (27K). Velocity, spin, movement, location. No data 2020-2023.",
    }
    if resolved not in methodologies:
        return json.dumps({"error": f"Unknown dataset '{dataset_name}'. Available: {', '.join(sorted(methodologies.keys()))}"})

    return json.dumps({"dataset": resolved, "methodology": methodologies[resolved]})


class _BearerInterceptor(grpc.UnaryUnaryClientInterceptor):
    def __init__(self, token):
        self._metadata = [("authorization", f"Bearer {token}")]

    def intercept_unary_unary(self, continuation, client_call_details, request):
        metadata = list(client_call_details.metadata or []) + self._metadata
        new_details = client_call_details._replace(metadata=metadata)
        return continuation(new_details, request)


SPICEDB_ENDPOINT = os.environ.get("SPICEDB_ENDPOINT", "dev:50051")
SPICEDB_TOKEN = os.environ.get("SPICEDB_TOKEN", "averysecretpresharedkey")

_spicedb_channel = grpc.intercept_channel(
    grpc.insecure_channel(SPICEDB_ENDPOINT),
    _BearerInterceptor(SPICEDB_TOKEN),
)
_spicedb_client = SpiceDBClient.__new__(SpiceDBClient)
_spicedb_client.init_stubs(_spicedb_channel)


@tool
def check_dataset_permission(subject_id: str, resource_id: str, permission: str) -> str:
    """Check if a user has a specific permission on a dataset using SpiceDB.

    Args:
        subject_id: The user ID to check (e.g., 'admin', 'viewer').
        resource_id: The dataset name (e.g., 'batting', 'pitching', 'teams', 'weather').
        permission: The permission to check (e.g., 'query', 'view_metadata', 'export').

    Returns:
        JSON with 'allowed' (true/false) and details.
    """
    from authzed.api.v1 import (
        CheckPermissionRequest, CheckPermissionResponse,
        ObjectReference, SubjectReference,
    )

    resp = _spicedb_client.CheckPermission(
        CheckPermissionRequest(
            resource=ObjectReference(object_type="dataset", object_id=resource_id),
            permission=permission,
            subject=SubjectReference(
                object=ObjectReference(object_type="user", object_id=subject_id)
            ),
        )
    )
    allowed = resp.permissionship == CheckPermissionResponse.PERMISSIONSHIP_HAS_PERMISSION

    return json.dumps({
        "allowed": allowed,
        "subject": f"user:{subject_id}",
        "resource": f"dataset:{resource_id}",
        "permission": permission,
    })
