#!/usr/bin/env python3
"""Load prediction history from Chainlit SQLite into Trino Iceberg.

Pipeline: Chainlit SQLite → parse predictions → match results → Parquet → MinIO → Iceberg.

Usage:
    python scripts/load-predictions-trino.py

Environment variables:
    TRINO_HOST        Trino coordinator host (default: localhost)
    TRINO_PORT        Trino coordinator port (default: 8080)
    MINIO_ENDPOINT    MinIO endpoint (default: localhost:9000)
    MINIO_ACCESS_KEY  Access key (default: minio)
    MINIO_SECRET_KEY  Secret key (default: minio1234)
    CHAINLIT_DB       Path to chainlit.db (default: data/predictions/chainlit.db)
"""

import hashlib
import os
import re
import sqlite3
import sys
import time

import pandas as pd
from minio import Minio
from trino.dbapi import connect

TRINO_HOST = os.environ.get("TRINO_HOST", "localhost")
TRINO_PORT = int(os.environ.get("TRINO_PORT", "8080"))
MINIO_ENDPOINT = os.environ.get("MINIO_ENDPOINT", "localhost:9000")
MINIO_ACCESS_KEY = os.environ.get("MINIO_ACCESS_KEY", "minio")
MINIO_SECRET_KEY = os.environ.get("MINIO_SECRET_KEY", "minio1234")
CHAINLIT_DB = os.environ.get("CHAINLIT_DB", "data/predictions/chainlit.db")

BUCKET = "mlb-data"
PARQUET_PREFIX = "parquet"
TABLE_NAME = "prediction_history"

INT_COLS = {"game_pk", "away_score", "home_score", "was_correct"}

MONTH_MAP = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}

TEAM_ALIASES = {
    "arizona diamondbacks": "Arizona Diamondbacks", "diamondbacks": "Arizona Diamondbacks", "d-backs": "Arizona Diamondbacks", "ari": "Arizona Diamondbacks", "az": "Arizona Diamondbacks",
    "atlanta braves": "Atlanta Braves", "braves": "Atlanta Braves", "atl": "Atlanta Braves",
    "arizona d-backs": "Arizona Diamondbacks", "d-backs": "Arizona Diamondbacks",
    "baltimore orioles": "Baltimore Orioles", "orioles": "Baltimore Orioles", "o's": "Baltimore Orioles", "bal": "Baltimore Orioles",
    "boston red sox": "Boston Red Sox", "red sox": "Boston Red Sox", "bos": "Boston Red Sox",
    "chicago cubs": "Chicago Cubs", "cubs": "Chicago Cubs", "chc": "Chicago Cubs",
    "chicago white sox": "Chicago White Sox", "white sox": "Chicago White Sox", "cws": "Chicago White Sox", "chw": "Chicago White Sox",
    "cincinnati reds": "Cincinnati Reds", "reds": "Cincinnati Reds", "cin": "Cincinnati Reds",
    "cleveland guardians": "Cleveland Guardians", "guardians": "Cleveland Guardians", "cle": "Cleveland Guardians",
    "colorado rockies": "Colorado Rockies", "rockies": "Colorado Rockies", "col": "Colorado Rockies",
    "detroit tigers": "Detroit Tigers", "tigers": "Detroit Tigers", "det": "Detroit Tigers", "detroit": "Detroit Tigers",
    "houston astros": "Houston Astros", "astros": "Houston Astros", "hou": "Houston Astros", "houston": "Houston Astros",
    "kansas city royals": "Kansas City Royals", "royals": "Kansas City Royals", "kc": "Kansas City Royals", "kansas city": "Kansas City Royals",
    "los angeles angels": "Los Angeles Angels", "angels": "Los Angeles Angels", "laa": "Los Angeles Angels", "la angels": "Los Angeles Angels",
    "los angeles dodgers": "Los Angeles Dodgers", "dodgers": "Los Angeles Dodgers", "lad": "Los Angeles Dodgers", "la dodgers": "Los Angeles Dodgers",
    "miami marlins": "Miami Marlins", "marlins": "Miami Marlins", "mia": "Miami Marlins", "miami": "Miami Marlins",
    "milwaukee brewers": "Milwaukee Brewers", "brewers": "Milwaukee Brewers", "mil": "Milwaukee Brewers", "milwaukee": "Milwaukee Brewers",
    "minnesota twins": "Minnesota Twins", "twins": "Minnesota Twins", "min": "Minnesota Twins", "minnesota": "Minnesota Twins",
    "new york mets": "New York Mets", "mets": "New York Mets", "nym": "New York Mets",
    "new york yankees": "New York Yankees", "yankees": "New York Yankees", "nyy": "New York Yankees", "ny yankees": "New York Yankees",
    "oakland athletics": "Athletics", "athletics": "Athletics", "a's": "Athletics", "oak": "Athletics", "oakland": "Athletics", "ath": "Athletics",
    "philadelphia phillies": "Philadelphia Phillies", "phillies": "Philadelphia Phillies", "phi": "Philadelphia Phillies", "philadelphia": "Philadelphia Phillies",
    "pittsburgh pirates": "Pittsburgh Pirates", "pirates": "Pittsburgh Pirates", "pit": "Pittsburgh Pirates", "pittsburgh": "Pittsburgh Pirates",
    "san diego padres": "San Diego Padres", "padres": "San Diego Padres", "sd": "San Diego Padres", "san diego": "San Diego Padres",
    "san francisco giants": "San Francisco Giants", "giants": "San Francisco Giants", "sf": "San Francisco Giants", "san francisco": "San Francisco Giants",
    "seattle mariners": "Seattle Mariners", "mariners": "Seattle Mariners", "sea": "Seattle Mariners", "seattle": "Seattle Mariners",
    "st. louis cardinals": "St. Louis Cardinals", "cardinals": "St. Louis Cardinals", "stl": "St. Louis Cardinals",
    "tampa bay rays": "Tampa Bay Rays", "rays": "Tampa Bay Rays", "tb": "Tampa Bay Rays", "tampa bay": "Tampa Bay Rays",
    "texas rangers": "Texas Rangers", "rangers": "Texas Rangers", "tex": "Texas Rangers", "texas": "Texas Rangers",
    "toronto blue jays": "Toronto Blue Jays", "blue jays": "Toronto Blue Jays", "tor": "Toronto Blue Jays", "toronto": "Toronto Blue Jays",
    "washington nationals": "Washington Nationals", "nationals": "Washington Nationals", "wsh": "Washington Nationals", "was": "Washington Nationals", "washington": "Washington Nationals",
    "boston": "Boston Red Sox", "cleveland": "Cleveland Guardians", "colorado": "Colorado Rockies",
    "cincinnati": "Cincinnati Reds", "baltimore": "Baltimore Orioles", "arizona": "Arizona Diamondbacks",
    "atlanta": "Atlanta Braves", "st. louis": "St. Louis Cardinals", "st louis": "St. Louis Cardinals",
}


def clean_markdown(text):
    """Strip markdown formatting artifacts from text."""
    if not text:
        return text
    text = text.replace("**", "")
    text = re.sub(r'[✅❌🏆📊]', '', text)
    text = text.strip()
    return text


def normalize_team(name):
    """Resolve a team name/alias to the canonical MLB Stats API name."""
    if not name:
        return None
    cleaned = clean_markdown(name).strip()
    cleaned = re.sub(r'^\d+\.\s*', '', cleaned)
    cleaned = cleaned.strip()
    return TEAM_ALIASES.get(cleaned.lower(), cleaned)


# Ambiguous city names that have two MLB teams.
# These can't be resolved by alias alone — need matchup context.
_AMBIGUOUS_CITIES = {
    "new york": ("New York Yankees", "New York Mets"),
    "chicago": ("Chicago Cubs", "Chicago White Sox"),
    "los angeles": ("Los Angeles Dodgers", "Los Angeles Angels"),
}

# Division/league opponents that disambiguate. If the opponent is in the
# same division/matchup pattern, pick the right team.
_CITY_HINTS = {
    # AL teams that distinguish which "New York" / "Chicago" / "LA"
    "Tampa Bay Rays": {"new york": "New York Yankees", "chicago": "Chicago White Sox", "los angeles": "Los Angeles Angels"},
    "Boston Red Sox": {"new york": "New York Yankees", "chicago": "Chicago White Sox"},
    "Baltimore Orioles": {"new york": "New York Yankees", "chicago": "Chicago White Sox"},
    "Toronto Blue Jays": {"new york": "New York Yankees", "chicago": "Chicago White Sox"},
    "Minnesota Twins": {"new york": "New York Yankees", "chicago": "Chicago White Sox"},
    "Cleveland Guardians": {"new york": "New York Yankees", "chicago": "Chicago White Sox"},
    "Detroit Tigers": {"new york": "New York Yankees", "chicago": "Chicago White Sox"},
    "Kansas City Royals": {"new york": "New York Yankees", "chicago": "Chicago White Sox"},
    "Houston Astros": {"new york": "New York Yankees", "chicago": "Chicago White Sox", "los angeles": "Los Angeles Angels"},
    "Texas Rangers": {"new york": "New York Yankees", "chicago": "Chicago White Sox", "los angeles": "Los Angeles Angels"},
    "Seattle Mariners": {"new york": "New York Yankees", "chicago": "Chicago White Sox", "los angeles": "Los Angeles Angels"},
    "Athletics": {"new york": "New York Yankees", "chicago": "Chicago White Sox", "los angeles": "Los Angeles Angels"},
    # NL teams
    "Atlanta Braves": {"new york": "New York Mets", "chicago": "Chicago Cubs", "los angeles": "Los Angeles Dodgers"},
    "Philadelphia Phillies": {"new york": "New York Mets", "chicago": "Chicago Cubs", "los angeles": "Los Angeles Dodgers"},
    "Miami Marlins": {"new york": "New York Mets", "chicago": "Chicago Cubs", "los angeles": "Los Angeles Dodgers"},
    "Washington Nationals": {"new york": "New York Mets", "chicago": "Chicago Cubs", "los angeles": "Los Angeles Dodgers"},
    "Milwaukee Brewers": {"new york": "New York Mets", "chicago": "Chicago Cubs", "los angeles": "Los Angeles Dodgers"},
    "St. Louis Cardinals": {"new york": "New York Mets", "chicago": "Chicago Cubs", "los angeles": "Los Angeles Dodgers"},
    "Cincinnati Reds": {"new york": "New York Mets", "chicago": "Chicago Cubs", "los angeles": "Los Angeles Dodgers"},
    "Pittsburgh Pirates": {"new york": "New York Mets", "chicago": "Chicago Cubs", "los angeles": "Los Angeles Dodgers"},
    "San Diego Padres": {"new york": "New York Mets", "chicago": "Chicago Cubs", "los angeles": "Los Angeles Dodgers"},
    "San Francisco Giants": {"new york": "New York Mets", "chicago": "Chicago Cubs", "los angeles": "Los Angeles Dodgers"},
    "Colorado Rockies": {"new york": "New York Mets", "chicago": "Chicago Cubs", "los angeles": "Los Angeles Dodgers"},
    "Arizona Diamondbacks": {"new york": "New York Mets", "chicago": "Chicago Cubs", "los angeles": "Los Angeles Dodgers"},
}


def resolve_ambiguous(away, home):
    """Resolve ambiguous city names using the opponent as context."""
    for team_ref, team_val in [(away, home), (home, away)]:
        if team_ref and team_ref.lower() in _AMBIGUOUS_CITIES:
            if team_val in _CITY_HINTS:
                resolved = _CITY_HINTS[team_val].get(team_ref.lower())
                if resolved:
                    if team_ref == away:
                        away = resolved
                    else:
                        home = resolved
    return away, home


def resolve_picked_team(picked, away, home):
    """Resolve an ambiguous picked team name using the matchup context.

    If the pick is just a city name like 'New York', figure out which
    team it refers to by checking which side of the matchup is from that city.
    """
    if not picked:
        return picked
    picked_lower = picked.lower()
    if picked_lower not in _AMBIGUOUS_CITIES:
        return picked
    candidates = _AMBIGUOUS_CITIES[picked_lower]
    for candidate in candidates:
        if candidate == away or candidate == home:
            return candidate
    return picked


def extract_game_date(thread_name, created_at):
    """Extract the game date from thread name or fall back to created_at + 1 day."""
    if not thread_name:
        return _offset_date(created_at, 1)

    m = re.search(r'(\d{1,2})\s+(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)', thread_name, re.I)
    if m:
        day = int(m.group(1))
        month = MONTH_MAP[m.group(2).lower()[:3]]
        year = 2026
        return f"{year}-{month:02d}-{day:02d}"

    return _offset_date(created_at, 1)


def _offset_date(iso_ts, days):
    """Add days to an ISO timestamp and return YYYY-MM-DD."""
    from datetime import datetime, timedelta
    try:
        dt = datetime.fromisoformat(iso_ts.replace("Z", "+00:00"))
        return (dt + timedelta(days=days)).strftime("%Y-%m-%d")
    except Exception:
        return None


def parse_predictions(output_text):
    """Parse prediction picks from agent output text.

    Handles three formats:
    A) ### headers: ### 1. **Away @ Home** — time / **Pick: TEAM** ✅
    B) Table with #: | # | Matchup | Pick | Confidence | Rationale |
    C) Table without #: | Matchup | Pick | Key Factors |
    """
    if not output_text:
        return []

    text = clean_markdown(output_text)
    predictions = []

    # Try table format first (Formats B and C)
    predictions = _parse_table_format(text)

    # Try ### header format (Format A)
    if not predictions:
        predictions = _parse_header_format(text)

    # Fallback: scan for "Pick:" lines
    if not predictions:
        predictions = _parse_pick_lines(text)

    return predictions


def _parse_table_format(text):
    """Parse markdown table with Matchup | Pick columns."""
    predictions = []
    lines = text.split("\n")

    header_idx = None
    matchup_col = None
    pick_col = None
    conf_col = None
    rationale_col = None

    for i, line in enumerate(lines):
        if "|" not in line:
            continue
        cells = [c.strip() for c in line.split("|")]
        cells = [c for c in cells if c]

        lower_cells = [c.lower() for c in cells]
        if "matchup" in lower_cells:
            header_idx = i
            matchup_col = lower_cells.index("matchup")
            pick_col = lower_cells.index("pick") if "pick" in lower_cells else None
            conf_col = lower_cells.index("confidence") if "confidence" in lower_cells else None
            rationale_col = None
            for label in ("rationale", "key factors", "reasoning"):
                if label in lower_cells:
                    rationale_col = lower_cells.index(label)
                    break
            continue

        if header_idx is None or pick_col is None:
            continue
        # Skip separator row
        if re.match(r'^[\s|:-]+$', line):
            continue

        if len(cells) <= max(matchup_col, pick_col):
            continue

        matchup_text = cells[matchup_col]
        pick_text = cells[pick_col]
        confidence = cells[conf_col].upper() if conf_col and conf_col < len(cells) else None
        rationale = cells[rationale_col] if rationale_col and rationale_col < len(cells) else None

        # Normalize confidence values
        if confidence:
            conf_map = {"HIGH": "STRONG", "MODERATE": "LEAN", "LOW": "COIN FLIP"}
            confidence = conf_map.get(confidence, confidence)

        m = re.search(r'(.+?)\s+@\s+(.+?)(?:\s*[—\-–]|\s*$)', matchup_text)
        if not m:
            continue

        away_team = normalize_team(m.group(1))
        home_team = normalize_team(m.group(2))
        picked_team = normalize_team(pick_text)

        if not picked_team:
            continue

        predictions.append({
            "away_team": away_team,
            "home_team": home_team,
            "picked_team": picked_team,
            "confidence": confidence,
            "away_pitcher": None,
            "home_pitcher": None,
            "reasoning_summary": rationale[:200] if rationale else None,
        })

    return predictions


def _parse_header_format(text):
    """Parse ### header blocks (Format A)."""
    predictions = []
    blocks = re.split(r'###\s*', text)

    for block in blocks:
        if not block.strip():
            continue

        # Match: "1. Away @ Home — time" or "Away @ Home"
        block_clean = re.sub(r'^\d+\.\s*', '', block.strip())
        m = re.match(r'(.+?)\s+@\s+(.+?)(?:\s*[—\-–].+?)?\s*\n', block_clean)
        if not m:
            continue

        away_team = normalize_team(m.group(1))
        home_team = normalize_team(m.group(2))

        pick_m = re.search(r'Pick:\s*(.+?)(?:\s*$)', block, re.M)
        picked_team = normalize_team(pick_m.group(1)) if pick_m else None

        conf_m = re.search(r'Confidence:\s*(STRONG|LEAN|COIN\s*FLIP|HIGH|MODERATE|LOW)', block, re.I)
        confidence = None
        if conf_m:
            conf_map = {"HIGH": "STRONG", "MODERATE": "LEAN", "LOW": "COIN FLIP"}
            confidence = conf_map.get(conf_m.group(1).upper(), conf_m.group(1).upper())

        pitch_m = re.search(r'Pitchers?:\s*(.+?)(?:\s+vs\.?\s+)(.+?)(?:\n|$)', block)
        away_pitcher = _extract_pitcher_name(pitch_m.group(1)) if pitch_m else None
        home_pitcher = _extract_pitcher_name(pitch_m.group(2)) if pitch_m else None

        reason_m = re.search(r'(?:[-•]\s+|Rationale:\s*)(.+)', block)
        reasoning = reason_m.group(1).strip()[:200] if reason_m else None

        if not picked_team:
            continue

        predictions.append({
            "away_team": away_team,
            "home_team": home_team,
            "picked_team": picked_team,
            "confidence": confidence,
            "away_pitcher": away_pitcher,
            "home_pitcher": home_pitcher,
            "reasoning_summary": reasoning,
        })

    return predictions


def _parse_pick_lines(text):
    """Fallback: scan for 'Pick: TEAM' lines and look backwards for matchups."""
    predictions = []
    lines = text.split("\n")

    for i, line in enumerate(lines):
        pick_m = re.search(r'Pick:\s*(.+?)(?:\s*$)', line)
        if not pick_m:
            continue

        picked_team = normalize_team(pick_m.group(1))
        if not picked_team:
            continue

        away_team = None
        home_team = None
        for j in range(max(0, i - 10), i):
            mm = re.search(r'(.+?)\s+@\s+(.+?)(?:\s*[—\-–]|\s*$)', lines[j])
            if mm:
                away_team = normalize_team(mm.group(1))
                home_team = normalize_team(mm.group(2))
                break

        predictions.append({
            "away_team": away_team,
            "home_team": home_team,
            "picked_team": picked_team,
            "confidence": None,
            "away_pitcher": None,
            "home_pitcher": None,
            "reasoning_summary": None,
        })

    return predictions


def _extract_pitcher_name(text):
    """Extract just the pitcher name from 'Name (stats...)' text."""
    if not text:
        return None
    cleaned = clean_markdown(text).strip()
    m = re.match(r'([A-Za-z\s.\'-]+?)(?:\(|$)', cleaned)
    return m.group(1).strip() if m else cleaned[:50]


sys.path.insert(0, os.path.dirname(__file__))
from prediction_loader import merge_predictions, print_accuracy


def read_chainlit_predictions(db_path):
    """Read prediction threads and steps from Chainlit SQLite."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    rows = conn.execute("""
        SELECT t.id AS thread_id, t.name AS thread_name, t."createdAt" AS thread_created,
               s.output, s."createdAt" AS step_created
        FROM threads t
        JOIN steps s ON t.id = s."threadId"
        WHERE s.output IS NOT NULL AND s.output != ''
          AND LENGTH(s.output) > 200
        ORDER BY t."createdAt"
    """).fetchall()

    conn.close()
    return rows


def is_prediction_thread(thread_name):
    """Check if a thread name indicates a prediction session (not a results review).

    Exclude threads must be checked first — thread names sometimes contain
    pasted picks tables alongside 'analyze' in the name.
    """
    if not thread_name:
        return False
    name_lower = thread_name.lower()
    if re.search(r'results|analy[sz]|review|how did|how well|accuracy', name_lower):
        return False
    if re.search(r'pick|winners|predict', name_lower):
        return True
    return False


def fetch_game_results(lakehouse_cur):
    """Fetch all completed game results from live_games.

    Keys by (game_date, away_team, home_team) AND by (away_team, home_team)
    with a date window to handle timezone mismatches (user in AU, games in US).
    """
    try:
        lakehouse_cur.execute("""
            SELECT game_pk, game_date, away_team_name, home_team_name,
                   away_score, home_score,
                   CASE WHEN CAST(home_score AS INTEGER) > CAST(away_score AS INTEGER)
                        THEN home_team_name ELSE away_team_name END AS winner
            FROM lakehouse.mlb.live_games
            WHERE game_status = 'Final'
        """)
        by_date = {}
        by_matchup = {}
        for row in lakehouse_cur.fetchall():
            game_date = str(row[1])[:10]
            result = {
                "game_pk": row[0], "game_date": game_date,
                "away_score": row[4], "home_score": row[5], "winner": row[6],
            }
            by_date[(game_date, row[2], row[3])] = result
            # Also index by matchup + date for fuzzy date matching
            matchup_key = (row[2], row[3])
            if matchup_key not in by_matchup:
                by_matchup[matchup_key] = []
            by_matchup[matchup_key].append(result)
        return by_date, by_matchup
    except Exception as e:
        print(f"  WARNING: Could not fetch game results: {e}", flush=True)
        return {}, {}


def find_game_result(game_date, away_team, home_team, by_date, by_matchup):
    """Find the matching game result, handling AU/US timezone date offsets."""
    from datetime import datetime, timedelta

    # Exact date match
    result = by_date.get((game_date, away_team, home_team))
    if result:
        return result

    # Try day before (AU date is 1 day ahead of US game date)
    try:
        dt = datetime.strptime(game_date, "%Y-%m-%d")
        prev_date = (dt - timedelta(days=1)).strftime("%Y-%m-%d")
        result = by_date.get((prev_date, away_team, home_team))
        if result:
            return result
    except ValueError:
        pass

    # Try matchup-based lookup (closest date within 2 days)
    matchup_results = by_matchup.get((away_team, home_team), [])
    if matchup_results and game_date:
        try:
            target = datetime.strptime(game_date, "%Y-%m-%d")
            closest = min(matchup_results,
                          key=lambda r: abs((datetime.strptime(r["game_date"], "%Y-%m-%d") - target).days))
            if abs((datetime.strptime(closest["game_date"], "%Y-%m-%d") - target).days) <= 2:
                return closest
        except ValueError:
            pass

    return {}


def main():
    t0 = time.time()

    if not os.path.exists(CHAINLIT_DB):
        print(f"ERROR: {CHAINLIT_DB} not found", file=sys.stderr)
        sys.exit(1)

    print(f"Reading predictions from {CHAINLIT_DB}", flush=True)
    print(f"Connecting to Trino at {TRINO_HOST}:{TRINO_PORT}", flush=True)

    minio_client = Minio(MINIO_ENDPOINT, access_key=MINIO_ACCESS_KEY,
                         secret_key=MINIO_SECRET_KEY, secure=False)
    if not minio_client.bucket_exists(BUCKET):
        minio_client.make_bucket(BUCKET)

    staging_conn = connect(host=TRINO_HOST, port=TRINO_PORT, user="admin",
                           catalog="staging", schema="mlb")
    lakehouse_conn = connect(host=TRINO_HOST, port=TRINO_PORT, user="admin",
                             catalog="lakehouse", schema="mlb")
    staging_cur = staging_conn.cursor()
    lakehouse_cur = lakehouse_conn.cursor()

    staging_cur.execute("CREATE SCHEMA IF NOT EXISTS staging.mlb")
    lakehouse_cur.execute("CREATE SCHEMA IF NOT EXISTS lakehouse.mlb")

    by_date, by_matchup = fetch_game_results(lakehouse_cur)
    print(f"  Loaded {len(by_date)} completed game results", flush=True)

    rows = read_chainlit_predictions(CHAINLIT_DB)
    print(f"  Found {len(rows)} candidate steps in Chainlit DB", flush=True)

    # Collect all predictions, then deduplicate per (thread, matchup)
    raw_records = {}
    used_game_pks = set()
    skipped = 0
    for row in rows:
        thread_name = row["thread_name"]
        if not is_prediction_thread(thread_name):
            continue

        game_date = extract_game_date(thread_name, row["thread_created"])
        preds = parse_predictions(row["output"])

        if not preds:
            skipped += 1
            continue

        for pred in preds:
            pred["away_team"], pred["home_team"] = resolve_ambiguous(
                pred["away_team"], pred["home_team"])
            pred["picked_team"] = resolve_picked_team(
                pred["picked_team"], pred["away_team"], pred["home_team"])
            dedup_key = (game_date, pred["away_team"], pred["home_team"])

            # Keep the first prediction with confidence; skip later duplicates
            existing = raw_records.get(dedup_key)
            if existing:
                if existing.get("confidence") or not pred.get("confidence"):
                    continue

            pred_id = hashlib.sha256(
                f"{row['thread_id']}:{pred['away_team']}:{pred['home_team']}".encode()
            ).hexdigest()[:16]

            result = find_game_result(game_date, pred["away_team"], pred["home_team"],
                                      by_date, by_matchup)

            # Skip if this game result was already claimed by another prediction
            gpk = result.get("game_pk")
            if gpk and gpk in used_game_pks:
                continue
            if gpk:
                used_game_pks.add(gpk)

            was_correct = None
            if result.get("winner") and pred["picked_team"]:
                was_correct = 1 if pred["picked_team"] == result["winner"] else 0

            raw_records[dedup_key] = {
                "prediction_id": pred_id,
                "thread_id": row["thread_id"],
                "thread_name": thread_name,
                "predicted_at": row["step_created"],
                "game_date": result.get("game_date", game_date),
                "away_team": pred["away_team"],
                "home_team": pred["home_team"],
                "picked_team": pred["picked_team"],
                "confidence": pred["confidence"],
                "away_pitcher": pred["away_pitcher"],
                "home_pitcher": pred["home_pitcher"],
                "reasoning_summary": pred["reasoning_summary"],
                "game_pk": result.get("game_pk"),
                "actual_winner": result.get("winner"),
                "was_correct": was_correct,
                "away_score": result.get("away_score"),
                "home_score": result.get("home_score"),
            }

    records = list(raw_records.values())

    if not records:
        print("  No predictions found to load", flush=True)
        staging_conn.close()
        lakehouse_conn.close()
        return

    df = pd.DataFrame(records)

    print(f"\n  Parsed {len(df)} predictions ({skipped} steps skipped)", flush=True)

    merge_predictions(df, staging_cur, lakehouse_cur, minio_client, "chainlit")
    print_accuracy(lakehouse_cur)

    staging_conn.close()
    lakehouse_conn.close()

    elapsed = time.time() - t0
    print(f"\nDone in {elapsed:.1f}s", flush=True)


if __name__ == "__main__":
    main()
