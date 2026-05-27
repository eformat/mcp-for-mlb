"""LangChain tools for MLB baseball data.

All data access goes through Trino (Iceberg lakehouse on MinIO S3).
Metadata tools return hardcoded domain knowledge.
SpiceDB provides fine-grained permission checks on datasets.
"""

import json
import os
import re

import asyncio
import concurrent.futures

import grpc

from langchain_core.tools import tool
from langchain_spicedb.core import SpiceDBAuthorizer
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


class _AsyncBearerInterceptor(grpc.aio.UnaryUnaryClientInterceptor):
    def __init__(self, token):
        self._metadata = [("authorization", f"Bearer {token}")]

    async def intercept_unary_unary(self, continuation, client_call_details, request):
        metadata = list(client_call_details.metadata or []) + self._metadata
        new_details = grpc.aio.ClientCallDetails(
            client_call_details.method, client_call_details.timeout,
            metadata, client_call_details.credentials,
            client_call_details.wait_for_ready,
        )
        return await continuation(new_details, request)


class _InsecureSpiceDBAuthorizer(SpiceDBAuthorizer):
    """SpiceDBAuthorizer using a plaintext gRPC channel for in-cluster comms."""

    @property
    def client(self) -> SpiceDBClient:
        current_loop = None
        try:
            current_loop = asyncio.get_running_loop()
        except RuntimeError:
            pass

        if self._client is None or self._client_loop is not current_loop:
            if current_loop:
                channel = grpc.aio.insecure_channel(
                    self.spicedb_endpoint,
                    interceptors=[_AsyncBearerInterceptor(self.spicedb_token)],
                )
            else:
                channel = grpc.intercept_channel(
                    grpc.insecure_channel(self.spicedb_endpoint),
                    _BearerInterceptor(self.spicedb_token),
                )
            self._client = SpiceDBClient.__new__(SpiceDBClient)
            self._client.init_stubs(channel)
            self._client_loop = current_loop
        return self._client


_spicedb = _InsecureSpiceDBAuthorizer(
    spicedb_endpoint=os.environ.get("SPICEDB_ENDPOINT", "dev:50051"),
    spicedb_token=os.environ.get("SPICEDB_TOKEN", "averysecretpresharedkey"),
    subject_type="user",
    resource_type="dataset",
)


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
    def _check():
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(
                _spicedb.check_permission(
                    subject_id=subject_id,
                    resource_id=resource_id,
                    permission=permission,
                )
            )
        finally:
            loop.close()

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        allowed = pool.submit(_check).result(timeout=10)

    return json.dumps({
        "allowed": allowed,
        "subject": f"user:{subject_id}",
        "resource": f"dataset:{resource_id}",
        "permission": permission,
    })
