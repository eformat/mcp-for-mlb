"""MLB MCP Server — Major League Baseball Data Gateway.

All data access goes through Trino (Iceberg lakehouse on MinIO S3).
Metadata tools (describe_datasets, get_methodology) return hardcoded
domain knowledge. The query_trino tool executes agent-generated SQL.

Data sources:
  - Lahman Baseball Database v2025 (1871-2025) loaded into Trino Iceberg tables
  - NOAA GHCN-D weather data for 210 US cities loaded into Trino Iceberg tables
  - Schema: lakehouse.mlb.*
"""

from __future__ import annotations

import os
import re

from fastmcp import FastMCP

mcp = FastMCP(
    name="mlb-data-server",
    instructions=(
        "Major League Baseball data server backed by the Lahman Baseball "
        "Database (1871-2025) and NOAA historical weather data. "
        "All data queries go through Trino SQL. Metadata tools provide "
        "methodology and dataset descriptions."
    ),
)

TRINO_HOST = os.environ.get("TRINO_QUERY_HOST", "trino")
TRINO_PORT = int(os.environ.get("TRINO_QUERY_PORT", "8080"))

_BLOCKED_SQL = re.compile(
    r"\b(INSERT|UPDATE|DELETE|DROP|ALTER|CREATE|TRUNCATE|MERGE|GRANT|REVOKE)\b",
    re.IGNORECASE,
)

_DATASET_ALIASES = {
    "hitting": "batting",
    "batting": "batting",
    "offense": "batting",
    "pitching": "pitching",
    "throwing": "pitching",
    "fielding": "fielding",
    "defense": "fielding",
    "teams": "teams",
    "team": "teams",
    "franchises": "teams",
    "awards": "awards",
    "mvp": "awards",
    "cy young": "awards",
    "gold glove": "awards",
    "hall of fame": "hall_of_fame",
    "hof": "hall_of_fame",
    "cooperstown": "hall_of_fame",
    "salaries": "salaries",
    "salary": "salaries",
    "pay": "salaries",
    "money": "salaries",
    "weather": "weather",
    "temperature": "weather",
    "rain": "weather",
    "parks": "parks",
    "stadiums": "parks",
    "ballparks": "parks",
    "postseason": "postseason",
    "playoffs": "postseason",
    "world series": "postseason",
    "ws": "postseason",
}

_METHODOLOGY = {
    "batting": {
        "collection_design": (
            "Official MLB game records compiled from box scores and play-by-play "
            "accounts. Season-level batting statistics aggregated per player per "
            "team per stint. Data from 1871 (National Association) through 2025."
        ),
        "data_scope": (
            "Regular season statistics only (postseason in separate batting_post table). "
            "One row per player-team-stint per season. 'stint' indicates the order of "
            "appearance with different teams in a season."
        ),
        "key_columns": (
            "playerID, yearID, stint, teamID, lgID, G, AB, R, H, doubles (2B), "
            "triples (3B), HR, RBI, SB, CS, BB, SO, IBB, HBP, SH, SF, GIDP"
        ),
        "computed_stats": (
            "Batting average (AVG) = H/AB; On-base percentage (OBP) = (H+BB+HBP)/(AB+BB+HBP+SF); "
            "Slugging (SLG) = (H-doubles-triples-HR + 2*doubles + 3*triples + 4*HR)/AB; "
            "OPS = OBP + SLG. These must be computed in SQL, they are not stored columns."
        ),
        "known_biases": [
            "Dead-ball era (pre-1920): lower offensive output due to ball construction and rules",
            "Integration era (1947+): excludes pre-integration Black players from AL/NL stats",
            "Expansion eras (1961, 1962, 1969, 1977, 1993, 1998): diluted pitching talent temporarily inflated offense",
            "Steroid era (~1993-2004): suspected performance enhancement inflated power numbers",
            "Pitch clock era (2023+): game pace changes may affect plate discipline stats",
            "Negro League data (added 2024): may be incomplete for some seasons/teams",
        ],
        "geographic_resolution": "Team/franchise level",
        "temporal_resolution": "Season-level aggregates (no game-by-game data)",
        "update_frequency": "Annually (Lahman Database release, typically December)",
    },
    "pitching": {
        "collection_design": (
            "Official MLB pitching records from box scores. Season-level statistics "
            "per pitcher per team per stint. IPouts represents outs recorded "
            "(divide by 3 for innings pitched)."
        ),
        "data_scope": (
            "Regular season statistics (postseason in pitching_post table). "
            "Includes wins, losses, saves, ERA, strikeouts, walks, and more."
        ),
        "key_columns": (
            "playerID, yearID, stint, teamID, lgID, W, L, G, GS, CG, SHO, SV, "
            "IPouts, H, ER, HR, BB, SO, BAOpp, ERA, IBB, WP, HBP, BK, BFP, GF, R"
        ),
        "computed_stats": (
            "Innings pitched = IPouts/3; WHIP = (BB+H)*3/IPouts; "
            "K/9 = SO*27/IPouts; BB/9 = BB*27/IPouts; HR/9 = HR*27/IPouts. "
            "ERA is stored directly."
        ),
        "known_biases": [
            "Mound height lowered from 15 to 10 inches in 1969 — significantly changed pitching dynamics",
            "Designated hitter rule: AL since 1973, NL adopted 2022 — affects pitcher batting and usage",
            "Relief pitching evolution: save rule introduced 1969, closer role evolved through 1980s-90s",
            "Pitch count awareness (2000s+): reduced complete games and innings per start",
            "Steroid era affected both pitchers and batters",
        ],
        "geographic_resolution": "Team/franchise level",
        "temporal_resolution": "Season-level aggregates",
        "update_frequency": "Annually",
    },
    "fielding": {
        "collection_design": (
            "Official fielding statistics from game records. Broken down by "
            "player, position, team, and season. InnOuts represents defensive "
            "innings (as outs, divide by 3 for innings)."
        ),
        "data_scope": (
            "Regular season fielding (postseason in fielding_post table). "
            "Position-specific: one row per player-position-team-stint per season."
        ),
        "key_columns": "playerID, yearID, stint, teamID, lgID, POS, G, GS, InnOuts, PO, A, E, DP",
        "computed_stats": "Fielding percentage = (PO+A)/(PO+A+E)",
        "known_biases": [
            "Error scoring has become stricter over time — historical error rates appear inflated",
            "Advanced defensive metrics (UZR, DRS, OAA) are NOT available in this dataset",
            "Zone rating (ZR) available for some but not all player-seasons",
        ],
        "geographic_resolution": "Team/franchise level",
        "temporal_resolution": "Season-level aggregates",
        "update_frequency": "Annually",
    },
    "teams": {
        "collection_design": (
            "Season-level team statistics including wins, losses, standings, "
            "and aggregate batting/pitching/fielding stats. Includes franchise "
            "history mapping through teams_franchises table."
        ),
        "data_scope": (
            "One row per team per season from 1871 to 2025. Includes park name, "
            "attendance, park factors (BPF, PPF), and cross-reference IDs."
        ),
        "key_columns": (
            "yearID, lgID, teamID, franchID, divID, Rank, G, W, L, DivWin, "
            "WCWin, LgWin, WSWin, R, AB, H, HR, ERA, name, park, attendance"
        ),
        "known_biases": [
            "Team relocations change teamID (e.g., Montreal Expos MON → Washington Nationals WAS)",
            "League structure changes: divisions added 1969, wild card 1994, second wild card 2012",
            "Strike-shortened seasons: 1981 (split season), 1994-95, 2020 (60-game COVID season)",
            "Negro League teams included from 1920 — data may be incomplete",
        ],
        "geographic_resolution": "Team/city level",
        "temporal_resolution": "Season-level",
        "update_frequency": "Annually",
    },
    "awards": {
        "collection_design": (
            "Awards granted to players and managers, plus detailed voting records. "
            "awards_players has binary win/loss; awards_share_players has vote totals."
        ),
        "data_scope": (
            "MVP (since 1931), Cy Young (since 1956), Rookie of the Year, Gold Glove, "
            "Silver Slugger, and many more. Vote share data available for major awards."
        ),
        "key_columns": (
            "awards_players: playerID, awardID, yearID, lgID, tie, notes; "
            "awards_share_players: awardID, yearID, lgID, playerID, pointsWon, pointsMax, votesFirst"
        ),
        "known_biases": [
            "Award criteria have evolved over time (e.g., MVP voting philosophy)",
            "Some awards did not exist in earlier eras",
            "Voting biases: narrative-driven, park factor effects, media market size",
        ],
        "geographic_resolution": "League level (AL/NL)",
        "temporal_resolution": "Annual",
        "update_frequency": "Annually",
    },
    "hall_of_fame": {
        "collection_design": (
            "Complete Hall of Fame voting history from the Baseball Writers' "
            "Association of America (BBWAA) and various committees."
        ),
        "data_scope": (
            "Every ballot appearance with votes received, votes needed, and "
            "induction status. Categories: Player, Manager, Pioneer/Executive, Umpire."
        ),
        "key_columns": "playerID, yearid, votedBy, ballots, needed, votes, inducted, category",
        "known_biases": [
            "BBWAA voting rules have changed (10-player limit, 10-year eligibility reduced from 15)",
            "Era Committees and Veterans Committees use different criteria",
            "PED (performance-enhancing drug) era players face voter backlash",
        ],
        "geographic_resolution": "N/A (national institution)",
        "temporal_resolution": "Annual voting cycles",
        "update_frequency": "Annually",
    },
    "salaries": {
        "collection_design": (
            "Player salary data from 1985 to 2016. Collected from public records "
            "and team disclosures."
        ),
        "data_scope": (
            "Annual salary in US dollars. One row per player-team per season. "
            "Does NOT include post-2016 data, signing bonuses, deferred payments, "
            "or minor league salaries."
        ),
        "key_columns": "yearID, teamID, lgID, playerID, salary",
        "known_biases": [
            "Data ends in 2016 — no modern salary information",
            "Does not account for inflation (1985 dollar ≠ 2016 dollar)",
            "Excludes performance bonuses, deferred compensation, and endorsements",
            "Pre-free agency salaries (before 1976) not included",
        ],
        "geographic_resolution": "Team level",
        "temporal_resolution": "Annual",
        "update_frequency": "Dataset frozen at 2016",
    },
    "weather": {
        "collection_design": (
            "NOAA Global Historical Climatology Network Daily (GHCN-D) data "
            "for approximately 210 US cities. Compiled by Carnegie Mellon "
            "University from NOAA's Applied Climate Information System (ACIS)."
        ),
        "data_scope": (
            "Daily maximum temperature (tmax, °F), minimum temperature (tmin, °F), "
            "and precipitation (prcp, inches). Records span approximately 1872-2019 "
            "but vary significantly by station."
        ),
        "key_columns": (
            "weather_stations: station_id, city_name, latitude, longitude, station_name, start_date, end_date; "
            "weather_daily: station_id, observation_date, tmax, tmin, prcp"
        ),
        "known_biases": [
            "Not all stations have continuous records — gaps exist",
            "Urban heat island effect may affect temperature readings at city stations",
            "Station relocations over time may introduce discontinuities",
            "Trace precipitation recorded as 0 (originally coded as 'T')",
            "Weather data ends around 2019 — no recent years",
        ],
        "geographic_resolution": "City/weather station level (210 US cities)",
        "temporal_resolution": "Daily observations",
        "update_frequency": "Dataset covers ~1872-2019 (static)",
    },
    "parks": {
        "collection_design": (
            "Directory of all major league ballparks with location information. "
            "Links to home_games table for per-park attendance and game counts."
        ),
        "data_scope": (
            "346 ballparks from 1871 to present. Includes aliases for parks with "
            "multiple names. home_games links parks to teams and seasons."
        ),
        "key_columns": (
            "parks: parkkey, parkname, parkalias, city, state, country; "
            "home_games: yearkey, teamkey, parkkey, games, attendance, spanfirst, spanlast"
        ),
        "known_biases": [
            "Park dimensions and altitude significantly affect offensive statistics (park factors)",
            "Retractable roof stadiums blur indoor/outdoor distinction",
            "Weather correlation requires matching park city/state to weather station city",
        ],
        "geographic_resolution": "Individual ballpark",
        "temporal_resolution": "Season-level for home_games",
        "update_frequency": "Annually",
    },
    "postseason": {
        "collection_design": (
            "Postseason statistics from batting_post, pitching_post, fielding_post, "
            "and series_post tables. series_post has series-level results."
        ),
        "data_scope": (
            "All playoff and World Series data. Round codes: WS (World Series), "
            "ALCS/NLCS (League Championship), ALDS/NLDS (Division Series), "
            "ALWC/NLWC (Wild Card). Negro League rounds: NWS, NLC, etc."
        ),
        "key_columns": (
            "series_post: yearID, round, teamIDwinner, teamIDloser, wins, losses, ties; "
            "batting_post/pitching_post: same as regular season tables plus round column"
        ),
        "known_biases": [
            "Postseason format has expanded over time (fewer rounds in earlier eras)",
            "Small sample sizes — postseason stats are volatile",
            "World Series was best-of-9 in some early years",
        ],
        "geographic_resolution": "Team level",
        "temporal_resolution": "Per-series and per-game aggregates",
        "update_frequency": "Annually",
    },
}


def _resolve_dataset(query: str) -> str | None:
    return _DATASET_ALIASES.get(query.strip().lower())


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------

@mcp.tool(description=(
    "Execute a read-only SQL query against the MLB Iceberg lakehouse in Trino. "
    "This is the primary data access tool — use it for ALL data queries.\n\n"
    "Schema: lakehouse.mlb\n\n"
    "BASEBALL TABLES (Lahman Database v2025, 1871-2025):\n"
    "1. batting — season batting stats per player-team-stint\n"
    "   Columns: playerID, yearID, stint, teamID, lgID, G, AB, R, H, doubles, triples, HR, RBI, SB, CS, BB, SO, IBB, HBP, SH, SF, GIDP\n"
    "2. pitching — season pitching stats per player-team-stint\n"
    "   Columns: playerID, yearID, stint, teamID, lgID, W, L, G, GS, CG, SHO, SV, IPouts, H, ER, HR, BB, SO, BAOpp, ERA, IBB, WP, HBP, BK, BFP, GF, R, SH, SF, GIDP\n"
    "3. fielding — season fielding stats per player-position-team-stint\n"
    "   Columns: playerID, yearID, stint, teamID, lgID, POS, G, GS, InnOuts, PO, A, E, DP, PB, WP, SB, CS, ZR\n"
    "4. people — player biographical data (master table)\n"
    "   Columns: playerID, nameFirst, nameLast, nameGiven, birthYear, birthMonth, birthDay, birthCity, birthState, birthCountry, weight, height, bats, throws, debut, finalGame\n"
    "5. teams — season team stats and standings\n"
    "   Columns: yearID, lgID, teamID, franchID, divID, Rank, G, W, L, DivWin, WCWin, LgWin, WSWin, R, AB, H, doubles, triples, HR, BB, SO, ERA, name, park, attendance, BPF, PPF\n"
    "6. parks — ballpark directory\n"
    "   Columns: parkkey, parkname, parkalias, city, state, country\n"
    "7. teams_franchises — franchise history\n"
    "   Columns: franchID, franchName, active, NAassoc\n"
    "8. appearances — games at each position per player-team-season\n"
    "9. batting_post — postseason batting (same cols as batting + round)\n"
    "10. pitching_post — postseason pitching (same cols as pitching + round)\n"
    "11. fielding_post — postseason fielding (same cols + round, TP)\n"
    "12. series_post — postseason series results\n"
    "    Columns: yearID, round, teamIDwinner, lgIDwinner, teamIDloser, lgIDloser, wins, losses, ties\n"
    "13. allstar_full — All-Star game appearances\n"
    "14. awards_players — awards won by players (awardID, yearID, lgID)\n"
    "15. awards_share_players — award voting details (pointsWon, pointsMax, votesFirst)\n"
    "16. awards_managers — manager awards\n"
    "17. awards_share_managers — manager award voting\n"
    "18. hall_of_fame — Hall of Fame voting history (inducted Y/N, votes, ballots)\n"
    "19. managers — manager season records (W, L, rank)\n"
    "20. home_games — games and attendance per park per season\n"
    "    Columns: yearkey, leaguekey, teamkey, parkkey, spanfirst, spanlast, games, openings, attendance\n"
    "21. salaries — player salaries 1985-2016\n"
    "22. schools — college/university directory\n"
    "23. college_playing — player college associations\n"
    "24. fielding_of — outfield position splits (historical)\n"
    "25. fielding_of_split — detailed outfield splits\n"
    "26. managers_half — split-season manager records\n"
    "27. teams_half — split-season team records\n\n"
    "WEATHER TABLES (NOAA GHCN-D, ~1872-2019):\n"
    "28. weather_stations — station metadata (station_id, city_name, latitude, longitude, station_name, start_date, end_date)\n"
    "29. weather_daily — daily observations (station_id, observation_date, tmax °F, tmin °F, prcp inches)\n\n"
    "KEY JOIN PATTERNS:\n"
    "- Player stats with names: JOIN people ON batting.playerID = people.playerID\n"
    "- Team franchise names: JOIN teams_franchises ON teams.franchID = teams_franchises.franchID\n"
    "- Park weather: JOIN parks p ON home_games.parkkey = p.parkkey "
    "JOIN weather_stations ws ON LOWER(p.city) = LOWER(ws.city_name) "
    "JOIN weather_daily wd ON ws.station_id = wd.station_id\n\n"
    "COMPUTED STATS (not stored, must calculate in SQL):\n"
    "- Batting Average: CAST(H AS DOUBLE)/NULLIF(AB, 0)\n"
    "- ERA from raw: CAST(ER AS DOUBLE)*27/NULLIF(IPouts, 0)\n"
    "- Innings Pitched: CAST(IPouts AS DOUBLE)/3\n"
    "- OBP: CAST(H+BB+HBP AS DOUBLE)/NULLIF(AB+BB+HBP+SF, 0)\n"
    "- SLG: CAST(H-doubles-triples-HR + 2*doubles + 3*triples + 4*HR AS DOUBLE)/NULLIF(AB, 0)\n"
    "- WHIP: CAST(BB+H AS DOUBLE)*3/NULLIF(IPouts, 0)\n"
))
async def query_trino(sql: str) -> dict:
    """Execute a read-only SQL query against Trino.

    Args:
        sql: SQL query to execute. Must be SELECT only.
    """
    if _BLOCKED_SQL.search(sql):
        return {
            "results": [],
            "error": "Only SELECT queries are allowed. Write operations are blocked.",
            "data_freshness": {
                "dataset_name": "MLB Iceberg Lakehouse",
                "dataset_url": "https://www.seanlahman.com/baseball-archive/statistics/",
            },
        }

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

        return {
            "results": results,
            "columns": columns,
            "row_count": len(results),
            "sql_executed": sql,
            "truncated": len(results) == 1000,

            "methodology": (
                "Data from the Lahman Baseball Database which compiles official "
                "MLB game records and box scores. Statistics are season-level "
                "aggregates. Weather data from NOAA GHCN-D daily observations."
            ),

            "data_freshness": {
                "dataset_name": "Lahman Baseball Database v2025 + NOAA GHCN-D",
                "dataset_url": "https://www.seanlahman.com/baseball-archive/statistics/",
                "dataset_updated": "December 2025 (baseball), ~2019 (weather)",
            },

            "citation": {
                "source": "Sean Lahman Baseball Database v2025; NOAA GHCN-D via CMU",
                "url": "https://www.seanlahman.com/baseball-archive/statistics/",
            },

            "caveats": [
                "Statistics are season-level aggregates — no game-by-game or pitch-by-pitch data.",
                "Negro League data (added 2024) may be incomplete for some seasons.",
                "Salary data covers 1985-2016 only.",
                "Weather data covers ~1872-2019 and station coverage varies by city.",
                "Era context matters: comparing across eras requires accounting for rule changes.",
            ],
        }

    except Exception as exc:
        return {
            "results": [],
            "error": str(exc),
            "sql_executed": sql,
            "data_freshness": {
                "dataset_name": "MLB Iceberg Lakehouse",
                "dataset_url": "https://www.seanlahman.com/baseball-archive/statistics/",
            },
        }


@mcp.tool(description=(
    "Describe and compare MLB datasets available for a topic. Use when "
    "the user asks what data is available, or when you need to understand "
    "dataset characteristics before writing SQL."
))
async def describe_datasets(topic: str = "") -> dict:
    """List available MLB datasets and their characteristics.

    Args:
        topic: Optional topic filter (e.g., 'batting', 'pitching', 'fielding',
               'postseason', 'awards', 'teams', 'weather', 'all').
    """
    topic_lower = topic.strip().lower() if topic else "all"

    datasets = {
        "batting": {
            "tables": ["batting", "batting_post"],
            "years_available": "1871-2025",
            "key_features": [
                "Season-level batting stats per player-team-stint",
                "Computed stats: AVG, OBP, SLG, OPS must be calculated in SQL",
                "Postseason batting in separate batting_post table",
            ],
            "join_with": "people (for player names), teams (for team context)",
        },
        "pitching": {
            "tables": ["pitching", "pitching_post"],
            "years_available": "1871-2025",
            "key_features": [
                "IPouts = outs recorded (divide by 3 for innings pitched)",
                "ERA stored directly; WHIP, K/9 must be computed",
            ],
            "join_with": "people (for names), teams (for context)",
        },
        "fielding": {
            "tables": ["fielding", "fielding_post", "fielding_of", "fielding_of_split"],
            "years_available": "1871-2025",
            "key_features": [
                "Position-specific stats (POS column)",
                "Advanced metrics (UZR, DRS, OAA) NOT available",
            ],
            "join_with": "people, appearances (for position games breakdown)",
        },
        "teams": {
            "tables": ["teams", "teams_franchises", "teams_half"],
            "years_available": "1871-2025",
            "key_features": [
                "Team standings, aggregate stats, park factors",
                "Franchise mapping: teams.franchID → teams_franchises.franchID",
            ],
            "join_with": "parks (via team name or home_games), managers",
        },
        "postseason": {
            "tables": ["series_post", "batting_post", "pitching_post", "fielding_post"],
            "years_available": "1884-2025",
            "key_features": [
                "Series results with round codes: WS, ALCS, NLCS, ALDS, NLDS, ALWC, NLWC",
                "Individual player postseason stats",
            ],
            "join_with": "people (names), teams (team context)",
        },
        "awards": {
            "tables": ["awards_players", "awards_share_players", "awards_managers", "awards_share_managers"],
            "years_available": "1877-2025",
            "key_features": [
                "Award winners and voting details",
                "awardID values: 'Most Valuable Player', 'Cy Young Award', 'Gold Glove', etc.",
            ],
            "join_with": "people (names)",
        },
        "hall_of_fame": {
            "tables": ["hall_of_fame"],
            "years_available": "1936-2025",
            "key_features": [
                "Complete voting history with vote counts",
                "inducted = 'Y' for Hall of Famers",
            ],
            "join_with": "people (names)",
        },
        "weather": {
            "tables": ["weather_stations", "weather_daily"],
            "years_available": "~1872-2019",
            "key_features": [
                "Daily tmax/tmin (°F) and precipitation (inches) for 210 US cities",
                "Match to ballparks via parks.city = weather_stations.city_name",
            ],
            "join_with": "parks + home_games (for game-day weather analysis)",
        },
        "parks": {
            "tables": ["parks", "home_games"],
            "years_available": "1871-2025",
            "key_features": [
                "346 ballparks with location data",
                "home_games links parks to teams and seasons with attendance",
            ],
            "join_with": "teams (via home_games.teamkey), weather_stations (via city)",
        },
        "salaries": {
            "tables": ["salaries"],
            "years_available": "1985-2016",
            "key_features": [
                "Annual player salary in USD",
                "Data ends at 2016 — no modern salaries",
            ],
            "join_with": "people (names), teams (team context)",
        },
        "biographical": {
            "tables": ["people", "schools", "college_playing", "appearances"],
            "years_available": "1871-2025",
            "key_features": [
                "Player bio: birth/death dates, height, weight, handedness",
                "College associations, position appearances per season",
            ],
            "join_with": "batting, pitching, fielding (via playerID)",
        },
    }

    if topic_lower not in ("all", ""):
        resolved = _resolve_dataset(topic_lower)
        if resolved and resolved in datasets:
            filtered = {resolved: datasets[resolved]}
        else:
            filtered = {
                k: v for k, v in datasets.items()
                if topic_lower in k or topic_lower in str(v.get("tables", []))
            }
            if not filtered:
                filtered = datasets
    else:
        filtered = datasets

    return {
        "availability": [
            {"dataset": k, **v} for k, v in filtered.items()
        ],
        "methodology": (
            "All baseball data from the Lahman Baseball Database — compiled "
            "from official MLB game records and box scores. Weather data from "
            "NOAA GHCN-D daily observations at US weather stations."
        ),
        "cross_dataset_context": {
            "comparison_notes": (
                "Comparing across eras requires context: rule changes, integration, "
                "expansion, designated hitter, and PED era all affect statistics. "
                "The playerID column links most tables."
            ),
        },
        "data_freshness": {
            "dataset_name": "Lahman Baseball Database v2025",
            "dataset_url": "https://www.seanlahman.com/baseball-archive/statistics/",
            "dataset_updated": "December 2025",
        },
        "citation": {
            "source": "Sean Lahman Baseball Database v2025",
            "url": "https://www.seanlahman.com/baseball-archive/statistics/",
        },
    }


@mcp.tool(description=(
    "Retrieve detailed methodology for a specific MLB dataset. Use when "
    "assessing data quality, understanding era context, or explaining "
    "statistical comparisons."
))
async def get_methodology(dataset_name: str) -> dict:
    """Get deep methodology for a specific MLB dataset.

    Args:
        dataset_name: Dataset name (e.g., 'batting', 'pitching', 'weather',
                      'awards', 'hall_of_fame', 'salaries', 'parks', 'postseason').
    """
    resolved = _resolve_dataset(dataset_name)
    if not resolved:
        resolved = dataset_name.strip().lower()

    if resolved not in _METHODOLOGY:
        available = ", ".join(sorted(_METHODOLOGY.keys()))
        return {
            "methodology_structured": None,
            "terminology_note": (
                f"'{dataset_name}' could not be mapped to a known dataset. "
                f"Available: {available}."
            ),
            "data_freshness": {
                "dataset_name": "Lahman Baseball Database v2025",
                "dataset_url": "https://www.seanlahman.com/baseball-archive/statistics/",
            },
        }

    meth = _METHODOLOGY[resolved]

    return {
        "methodology_structured": {
            "dataset": resolved,
            "data_type": "Official MLB records" if resolved != "weather" else "NOAA GHCN-D observations",
            "collection_design": meth["collection_design"],
            "data_scope": meth.get("data_scope", ""),
            "key_columns": meth.get("key_columns", ""),
            "computed_stats": meth.get("computed_stats", ""),
            "known_biases": meth["known_biases"],
            "geographic_resolution": meth["geographic_resolution"],
            "temporal_resolution": meth["temporal_resolution"],
            "update_frequency": meth["update_frequency"],
        },
        "methodology": meth["collection_design"],
        "data_freshness": {
            "dataset_name": f"MLB {resolved}",
            "dataset_url": "https://www.seanlahman.com/baseball-archive/statistics/",
            "dataset_updated": "December 2025",
        },
        "citation": {
            "source": "Sean Lahman Baseball Database v2025",
            "url": "https://www.seanlahman.com/baseball-archive/statistics/",
        },
    }


# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------

@mcp.custom_route("/health", methods=["GET"])
async def health(request):
    from starlette.responses import JSONResponse
    return JSONResponse({"status": "ok"})


if __name__ == "__main__":
    mcp.run(transport="streamable-http", host="0.0.0.0", port=9090)
