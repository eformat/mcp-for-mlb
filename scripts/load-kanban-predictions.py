#!/usr/bin/env python3
"""Load prediction history from Hermes Kanban into Trino Iceberg.

Pipeline: Kanban SQLite → extract structured JSON metadata → match results → Parquet → MinIO → Iceberg.

Usage:
    python scripts/load-kanban-predictions.py

Environment variables:
    TRINO_HOST        Trino coordinator host (default: localhost)
    TRINO_PORT        Trino coordinator port (default: 8080)
    MINIO_ENDPOINT    MinIO endpoint (default: localhost:9000)
    MINIO_ACCESS_KEY  Access key (default: minio)
    MINIO_SECRET_KEY  Secret key (default: minio1234)
    KANBAN_DB         Path to kanban.db (default: data/predictions/kanban.db)
"""

import hashlib
import json
import os
import sqlite3
import sys
import time

import pandas as pd
from minio import Minio
from trino.dbapi import connect

TRINO_HOST = os.environ.get("TRINO_HOST", "localhost")
TRINO_PORT = int(os.environ.get("TRINO_PORT", "8090"))
MINIO_ENDPOINT = os.environ.get("MINIO_ENDPOINT", "localhost:9000")
MINIO_ACCESS_KEY = os.environ.get("MINIO_ACCESS_KEY", "minio")
MINIO_SECRET_KEY = os.environ.get("MINIO_SECRET_KEY", "minio1234")
KANBAN_DB = os.environ.get("KANBAN_DB", "data/predictions/kanban.db")

sys.path.insert(0, os.path.dirname(__file__))
from prediction_loader import merge_predictions, print_accuracy

BUCKET = "mlb-data"
TABLE_NAME = "prediction_history"


def fetch_game_results(lakehouse_cur):
    """Fetch all completed game results from live_games."""
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
            matchup_key = (row[2], row[3])
            if matchup_key not in by_matchup:
                by_matchup[matchup_key] = []
            by_matchup[matchup_key].append(result)
        return by_date, by_matchup
    except Exception as e:
        print(f"  WARNING: Could not fetch game results: {e}", flush=True)
        return {}, {}


def find_game_result(game_date, away_team, home_team, by_date, by_matchup):
    """Find the matching game result, handling date offsets."""
    from datetime import datetime, timedelta

    result = by_date.get((game_date, away_team, home_team))
    if result:
        return result

    try:
        dt = datetime.strptime(game_date, "%Y-%m-%d")
        prev_date = (dt - timedelta(days=1)).strftime("%Y-%m-%d")
        result = by_date.get((prev_date, away_team, home_team))
        if result:
            return result
    except ValueError:
        pass

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


def read_kanban_predictions(db_path):
    """Read completed mlb-picker task runs from Kanban SQLite."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    tables = [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()]

    if "task_runs" not in tables or "tasks" not in tables:
        print(f"  WARNING: kanban.db missing expected tables. Found: {tables}", flush=True)
        conn.close()
        return []

    cols = [r[1] for r in conn.execute("PRAGMA table_info(task_runs)").fetchall()]
    print(f"  task_runs columns: {cols}", flush=True)

    cols_tasks = [r[1] for r in conn.execute("PRAGMA table_info(tasks)").fetchall()]
    print(f"  tasks columns: {cols_tasks}", flush=True)

    assignee_col = "assignee" if "assignee" in cols_tasks else "profile"
    metadata_col = "metadata" if "metadata" in cols else "result_metadata"
    summary_col = "summary" if "summary" in cols else "result"

    query = f"""
        SELECT t.id AS task_id, t.title, r.id AS run_id,
               r.{summary_col} AS summary,
               r.{metadata_col} AS metadata,
               r.ended_at AS completed_at
        FROM tasks t
        JOIN task_runs r ON t.id = r.task_id
        WHERE t.{assignee_col} = 'mlb-picker'
          AND r.outcome = 'completed'
          AND r.{metadata_col} IS NOT NULL
          AND r.{metadata_col} != ''
        ORDER BY r.ended_at DESC
    """
    try:
        rows = conn.execute(query).fetchall()
    except Exception as e:
        print(f"  WARNING: Query failed: {e}", flush=True)
        print("  Trying fallback query...", flush=True)
        rows = conn.execute("""
            SELECT t.id AS task_id, t.title, r.id AS run_id,
                   r.summary, r.metadata, r.ended_at AS completed_at
            FROM tasks t
            JOIN task_runs r ON t.id = r.task_id
            WHERE r.outcome = 'completed'
              AND r.metadata IS NOT NULL AND r.metadata != ''
            ORDER BY r.ended_at DESC
        """).fetchall()

    conn.close()
    return rows


def main():
    t0 = time.time()

    if not os.path.exists(KANBAN_DB):
        print(f"ERROR: {KANBAN_DB} not found", file=sys.stderr)
        sys.exit(1)

    print(f"Reading predictions from {KANBAN_DB}", flush=True)
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

    rows = read_kanban_predictions(KANBAN_DB)
    print(f"  Found {len(rows)} completed kanban task runs", flush=True)

    records = {}
    used_game_pks = set()
    for row in rows:
        metadata_raw = row["metadata"]
        try:
            metadata = json.loads(metadata_raw)
        except (json.JSONDecodeError, TypeError):
            print(f"  Skipping run {row['run_id']}: invalid JSON metadata", flush=True)
            continue

        predictions = metadata.get("predictions", [])
        if not predictions:
            print(f"  Skipping run {row['run_id']}: no predictions in metadata", flush=True)
            continue

        prediction_date = metadata.get("prediction_date")

        for pred in predictions:
            away_team = pred.get("away_team")
            home_team = pred.get("home_team")
            picked_team = pred.get("picked_team")
            if not all([away_team, home_team, picked_team]):
                continue

            game_date = prediction_date or pred.get("game_date")
            if not game_date:
                continue

            sorted_teams = tuple(sorted([away_team, home_team]))
            dedup_key = (game_date, sorted_teams)
            if dedup_key in records:
                continue

            pred_id = hashlib.sha256(
                f"kanban:{row['task_id']}:{away_team}:{home_team}".encode()
            ).hexdigest()[:16]

            result = find_game_result(game_date, away_team, home_team,
                                       by_date, by_matchup)

            gpk = result.get("game_pk")
            if gpk and gpk in used_game_pks:
                continue
            if gpk:
                used_game_pks.add(gpk)

            was_correct = None
            if result.get("winner") and picked_team:
                was_correct = 1 if picked_team == result["winner"] else 0

            records[dedup_key] = {
                "prediction_id": pred_id,
                "thread_id": f"kanban:{row['task_id']}",
                "thread_name": row["title"],
                "predicted_at": row["completed_at"],
                "game_date": result.get("game_date", game_date),
                "away_team": away_team,
                "home_team": home_team,
                "picked_team": picked_team,
                "confidence": pred.get("confidence"),
                "away_pitcher": pred.get("away_pitcher"),
                "home_pitcher": pred.get("home_pitcher"),
                "reasoning_summary": pred.get("reasoning_summary"),
                "game_pk": result.get("game_pk"),
                "actual_winner": result.get("winner"),
                "was_correct": was_correct,
                "away_score": result.get("away_score"),
                "home_score": result.get("home_score"),
            }

    all_records = list(records.values())

    if not all_records:
        print("  No predictions found to load", flush=True)
        staging_conn.close()
        lakehouse_conn.close()
        return

    df = pd.DataFrame(all_records)

    print(f"\n  Parsed {len(df)} predictions from Kanban", flush=True)

    merge_predictions(df, staging_cur, lakehouse_cur, minio_client, "kanban")
    print_accuracy(lakehouse_cur)

    staging_conn.close()
    lakehouse_conn.close()

    elapsed = time.time() - t0
    print(f"\nDone in {elapsed:.1f}s", flush=True)


if __name__ == "__main__":
    main()
