"""Shared Trino/MinIO merge logic for prediction loading scripts.

Both Chainlit and Kanban loaders use this to merge predictions into the
lakehouse.mlb.prediction_history table without clobbering each other.
"""

import tempfile
import shutil

import pandas as pd
from minio import Minio
from minio.deleteobjects import DeleteObject

BUCKET = "mlb-data"
PARQUET_PREFIX = "parquet"
TABLE_NAME = "prediction_history"
INT_COLS = {"game_pk", "away_score", "home_score", "was_correct"}

EXPECTED_COLUMNS = [
    "prediction_id", "thread_id", "thread_name", "predicted_at",
    "game_date", "away_team", "home_team", "picked_team", "confidence",
    "away_pitcher", "home_pitcher", "reasoning_summary",
    "game_pk", "actual_winner", "was_correct", "away_score", "home_score",
]


def _build_staging_columns(df):
    parts = []
    for col in df.columns:
        pd_dtype = df[col].dtype
        if pd_dtype == "float64":
            parts.append(f'"{col}" DOUBLE')
        elif pd_dtype == "int64":
            parts.append(f'"{col}" BIGINT')
        else:
            parts.append(f'"{col}" VARCHAR')
    return ", ".join(parts)


def _build_ctas_columns(df):
    parts = []
    for col in df.columns:
        if col in INT_COLS:
            parts.append(f'TRY_CAST("{col}" AS INTEGER) AS "{col}"')
        else:
            parts.append(f'CAST("{col}" AS VARCHAR) AS "{col}"')
    return ", ".join(parts)


def merge_predictions(df, staging_cur, lakehouse_cur, minio_client, source_tag):
    """Merge a DataFrame of predictions into lakehouse.mlb.prediction_history.

    Creates the lakehouse table if it doesn't exist, then inserts rows
    whose prediction_id is not already present (upsert by prediction_id).

    Args:
        df: DataFrame with prediction records (must have EXPECTED_COLUMNS).
        staging_cur: Trino cursor for staging catalog.
        lakehouse_cur: Trino cursor for lakehouse catalog.
        minio_client: MinIO client instance.
        source_tag: Short label for staging table name, e.g. "chainlit" or "kanban".
    """
    for col in EXPECTED_COLUMNS:
        if col not in df.columns:
            df[col] = None

    str_cols = [c for c in df.columns if c not in INT_COLS]
    for c in str_cols:
        df[c] = df[c].astype(object).where(df[c].notna(), None)

    staging_table = f"{TABLE_NAME}_{source_tag}"
    s3_dir = f"{PARQUET_PREFIX}/{staging_table}"
    s3_key = f"{s3_dir}/{staging_table}.parquet"

    tmpdir = tempfile.mkdtemp(prefix=f"mlb-{source_tag}-predictions-")
    pq_path = f"{tmpdir}/{staging_table}.parquet"
    df.to_parquet(pq_path, index=False, engine="pyarrow")

    old = list(minio_client.list_objects(BUCKET, prefix=f"{s3_dir}/", recursive=True))
    if old:
        list(minio_client.remove_objects(BUCKET, [DeleteObject(o.object_name) for o in old]))
    minio_client.fput_object(BUCKET, s3_key, pq_path)

    staging_cur.execute("CREATE SCHEMA IF NOT EXISTS staging.mlb")
    lakehouse_cur.execute("CREATE SCHEMA IF NOT EXISTS lakehouse.mlb")

    staging_cur.execute(f'DROP TABLE IF EXISTS staging.mlb."{staging_table}"')
    col_defs = _build_staging_columns(df)
    staging_cur.execute(f"""
        CREATE TABLE staging.mlb."{staging_table}" ({col_defs})
        WITH (format = 'PARQUET', external_location = 's3://{BUCKET}/{s3_dir}/')
    """)

    select_cols = _build_ctas_columns(df)

    try:
        lakehouse_cur.execute(f'SELECT 1 FROM lakehouse.mlb."{TABLE_NAME}" LIMIT 1')
        table_exists = True
    except Exception:
        table_exists = False

    if not table_exists:
        print(f"  Creating lakehouse.mlb.{TABLE_NAME}...", flush=True)
        lakehouse_cur.execute(f"""
            CREATE TABLE lakehouse.mlb."{TABLE_NAME}" AS
            SELECT {select_cols} FROM staging.mlb."{staging_table}"
        """)
    else:
        lakehouse_cur.execute(f"""
            INSERT INTO lakehouse.mlb."{TABLE_NAME}"
            SELECT {select_cols} FROM staging.mlb."{staging_table}" s
            WHERE NOT EXISTS (
                SELECT 1 FROM lakehouse.mlb."{TABLE_NAME}" t
                WHERE t."prediction_id" = CAST(s."prediction_id" AS VARCHAR)
            )
            AND NOT EXISTS (
                SELECT 1 FROM lakehouse.mlb."{TABLE_NAME}" t
                WHERE t."game_date" = CAST(s."game_date" AS VARCHAR)
                  AND t."away_team" = CAST(s."away_team" AS VARCHAR)
                  AND t."home_team" = CAST(s."home_team" AS VARCHAR)
            )
        """)

    lakehouse_cur.execute(f'SELECT COUNT(*) FROM lakehouse.mlb."{TABLE_NAME}"')
    count = lakehouse_cur.fetchone()[0]
    print(f"  {TABLE_NAME}: {count:,} total rows", flush=True)

    shutil.rmtree(tmpdir, ignore_errors=True)


def print_accuracy(lakehouse_cur):
    """Print accuracy summary from prediction_history."""
    print("\nPrediction Accuracy:", flush=True)
    try:
        lakehouse_cur.execute(f"""
            SELECT confidence,
                   COUNT(*) AS picks,
                   SUM(was_correct) AS correct,
                   ROUND(CAST(SUM(was_correct) AS DOUBLE) / NULLIF(COUNT(was_correct), 0), 3) AS accuracy
            FROM lakehouse.mlb."{TABLE_NAME}"
            WHERE was_correct IS NOT NULL
            GROUP BY confidence
            ORDER BY accuracy DESC
        """)
        for row in lakehouse_cur.fetchall():
            print(f"  {row[0] or 'UNKNOWN':12s}  {row[2]}/{row[1]} ({row[3]:.1%})", flush=True)
    except Exception as e:
        print(f"  Accuracy query failed: {e}", flush=True)

    try:
        lakehouse_cur.execute(f"""
            SELECT COUNT(*) AS total,
                   SUM(CASE WHEN was_correct IS NOT NULL THEN 1 ELSE 0 END) AS resolved,
                   SUM(CASE WHEN was_correct IS NULL THEN 1 ELSE 0 END) AS pending
            FROM lakehouse.mlb."{TABLE_NAME}"
        """)
        row = lakehouse_cur.fetchone()
        print(f"\n  Total: {row[0]}  Resolved: {row[1]}  Pending: {row[2]}", flush=True)
    except Exception as e:
        print(f"  Summary query failed: {e}", flush=True)
