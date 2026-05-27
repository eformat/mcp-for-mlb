#!/usr/bin/env python3
"""Load Statcast pitch-by-pitch data into Trino Iceberg tables via Parquet.

Pipeline: CSV → Parquet → MinIO S3 → Hive staging → CTAS into Iceberg.

Data sources:
  - 2015-2018 + 2019: pitches, atbats, games, player_names (custom Statcast format)
  - 2024-2025: Modern MLB Statcast 94-column postseason files

Usage:
    python scripts/load-pitch-trino.py

Environment variables:
    TRINO_HOST, TRINO_PORT, MINIO_ENDPOINT, MINIO_ACCESS_KEY, MINIO_SECRET_KEY
    DATA_DIR  Path to pitch data directory (default: data/pitch)
"""

import os
import time
import tempfile
import shutil
import glob

import pandas as pd
from minio import Minio
from minio.deleteobjects import DeleteObject
from trino.dbapi import connect

TRINO_HOST = os.environ.get("TRINO_HOST", "localhost")
TRINO_PORT = int(os.environ.get("TRINO_PORT", "8080"))
MINIO_ENDPOINT = os.environ.get("MINIO_ENDPOINT", "localhost:9000")
MINIO_ACCESS_KEY = os.environ.get("MINIO_ACCESS_KEY", "minio")
MINIO_SECRET_KEY = os.environ.get("MINIO_SECRET_KEY", "minio1234")
DATA_DIR = os.environ.get("DATA_DIR", "data/pitch")

BUCKET = "mlb-data"
PARQUET_PREFIX = "parquet"

INT_COLS_PITCHES = {
    "zone", "event_num", "b_score", "b_count", "s_count",
    "outs", "pitch_num", "on_1b", "on_2b", "on_3b",
}
DOUBLE_COLS_PITCHES = {
    "px", "pz", "start_speed", "end_speed", "spin_rate", "spin_dir",
    "break_angle", "break_length", "break_y", "ax", "ay", "az",
    "sz_bot", "sz_top", "type_confidence", "vx0", "vy0", "vz0",
    "x", "x0", "y", "y0", "z0", "pfx_x", "pfx_z", "nasty",
}

INT_COLS_ATBATS = {"inning", "top", "o", "p_score", "batter_id", "pitcher_id"}

INT_COLS_GAMES = {"attendance", "away_final_score", "home_final_score", "delay"}


def _staging_type(col, dtype, int_cols, double_cols):
    """Map actual pandas dtype to Hive staging column type.

    Must match what Parquet actually wrote — pandas stores nullable ints as float64.
    """
    if dtype == "float64":
        return "DOUBLE"
    if dtype == "int64":
        return "BIGINT"
    return "VARCHAR"


def _ctas_cast(col, int_cols, double_cols):
    """Generate CTAS SELECT expression with TRY_CAST."""
    if col in int_cols:
        return f'TRY_CAST("{col}" AS INTEGER) AS "{col}"'
    if col in double_cols:
        return f'TRY_CAST("{col}" AS DOUBLE) AS "{col}"'
    return f'CAST("{col}" AS VARCHAR) AS "{col}"'


def _load_table(name, df, int_cols, double_cols, minio_client, staging_cur, lakehouse_cur, tmpdir):
    """Write Parquet, upload to MinIO, create staging table, CTAS into Iceberg."""
    print(f"  {name}: {len(df):,} rows, {len(df.columns)} cols", flush=True)

    # Write Parquet (split large tables into chunks)
    s3_dir = f"{PARQUET_PREFIX}/{name}"
    old = list(minio_client.list_objects(BUCKET, prefix=f"{s3_dir}/", recursive=True))
    if old:
        list(minio_client.remove_objects(BUCKET, [DeleteObject(o.object_name) for o in old]))

    chunk_size = 1_000_000
    n_chunks = max(1, (len(df) + chunk_size - 1) // chunk_size)
    for i in range(n_chunks):
        chunk = df.iloc[i * chunk_size : (i + 1) * chunk_size]
        pq_name = f"{name}_{i:03d}.parquet"
        pq_path = os.path.join(tmpdir, pq_name)
        chunk.to_parquet(pq_path, index=False, engine="pyarrow")
        minio_client.fput_object(BUCKET, f"{s3_dir}/{pq_name}", pq_path)
        os.remove(pq_path)

    # Create staging table matching actual Parquet types
    staging_cur.execute(f'DROP TABLE IF EXISTS staging.mlb."{name}"')
    col_defs = ", ".join(
        f'"{c}" {_staging_type(c, str(df[c].dtype), int_cols, double_cols)}'
        for c in df.columns
    )
    staging_cur.execute(f"""
        CREATE TABLE staging.mlb."{name}" ({col_defs})
        WITH (format = 'PARQUET', external_location = 's3://{BUCKET}/{s3_dir}/')
    """)

    # CTAS into Iceberg
    lakehouse_cur.execute(f'DROP TABLE IF EXISTS lakehouse.mlb."{name}"')
    select_cols = ", ".join(_ctas_cast(c, int_cols, double_cols) for c in df.columns)
    lakehouse_cur.execute(f"""
        CREATE TABLE lakehouse.mlb."{name}" AS
        SELECT {select_cols} FROM staging.mlb."{name}"
    """)

    lakehouse_cur.execute(f'SELECT COUNT(*) FROM lakehouse.mlb."{name}"')
    count = lakehouse_cur.fetchone()[0]
    print(f"    -> lakehouse.mlb.{name}: {count:,} rows", flush=True)
    return count


def load_historical(minio_client, staging_cur, lakehouse_cur, tmpdir):
    """Load 2015-2018 + 2019 pitch data."""
    base = os.path.join(DATA_DIR, "2015-2018")
    total = 0

    # Games
    print("Loading pitch_games...", flush=True)
    g1 = pd.read_csv(os.path.join(base, "games.csv"), encoding="utf-8-sig", low_memory=False)
    g2 = pd.read_csv(os.path.join(base, "2019_games.csv"), encoding="utf-8-sig", low_memory=False)
    # 2019 has different column order but same columns (minus 'delay')
    if "delay" not in g2.columns:
        g2["delay"] = None
    games = pd.concat([g1, g2], ignore_index=True)
    total += _load_table("pitch_games", games, INT_COLS_GAMES, set(), minio_client, staging_cur, lakehouse_cur, tmpdir)

    # At-bats
    print("Loading pitch_atbats...", flush=True)
    a1 = pd.read_csv(os.path.join(base, "atbats.csv"), encoding="utf-8-sig", low_memory=False)
    a2 = pd.read_csv(os.path.join(base, "2019_atbats.csv"), encoding="utf-8-sig", low_memory=False)
    atbats = pd.concat([a1, a2], ignore_index=True)
    total += _load_table("pitch_atbats", atbats, INT_COLS_ATBATS, set(), minio_client, staging_cur, lakehouse_cur, tmpdir)

    # Pitches
    print("Loading pitch_pitches...", flush=True)
    p1 = pd.read_csv(os.path.join(base, "pitches.csv"), encoding="utf-8-sig", low_memory=False)
    p2 = pd.read_csv(os.path.join(base, "2019_pitches.csv"), encoding="utf-8-sig", low_memory=False)
    pitches = pd.concat([p1, p2], ignore_index=True)
    # 2019 data has 'placeholder' strings in numeric columns — coerce to NaN
    for col in DOUBLE_COLS_PITCHES | INT_COLS_PITCHES:
        if col in pitches.columns:
            pitches[col] = pd.to_numeric(pitches[col], errors="coerce")
    total += _load_table("pitch_pitches", pitches, INT_COLS_PITCHES, DOUBLE_COLS_PITCHES, minio_client, staging_cur, lakehouse_cur, tmpdir)

    # Player names
    print("Loading pitch_player_names...", flush=True)
    names = pd.read_csv(os.path.join(base, "player_names.csv"), encoding="utf-8-sig")
    total += _load_table("pitch_player_names", names, {"id"}, set(), minio_client, staging_cur, lakehouse_cur, tmpdir)

    return total


def load_modern_statcast(minio_client, staging_cur, lakehouse_cur, tmpdir):
    """Load 2024-2025 modern Statcast postseason data."""
    print("Loading statcast_pitches (2024-2025 postseason)...", flush=True)

    frames = []
    for subdir in ["2024", "2025"]:
        dirpath = os.path.join(DATA_DIR, subdir)
        if not os.path.isdir(dirpath):
            continue
        for f in sorted(glob.glob(os.path.join(dirpath, "*.csv"))):
            df = pd.read_csv(f, encoding="utf-8-sig", low_memory=False)
            # Normalize duplicate column names (pitcher.1, fielder_2.1)
            cols = []
            seen = {}
            for c in df.columns:
                if c in seen:
                    seen[c] += 1
                    cols.append(f"{c}_{seen[c]}")
                else:
                    seen[c] = 0
                    cols.append(c)
            df.columns = cols
            frames.append(df)

    if not frames:
        print("  No modern statcast files found", flush=True)
        return 0

    statcast = pd.concat(frames, ignore_index=True)

    # Deduplicate (the "ToDate" file is a subset of the full postseason file)
    dedup_cols = ["game_pk", "at_bat_number", "pitch_number"]
    if all(c in statcast.columns for c in dedup_cols):
        before = len(statcast)
        statcast = statcast.drop_duplicates(subset=dedup_cols, keep="last")
        print(f"  Deduplicated: {before:,} -> {len(statcast):,} rows", flush=True)

    # Identify numeric columns from the Statcast schema
    statcast_int_cols = {
        "game_year", "balls", "strikes", "inning", "at_bat_number", "pitch_number",
        "hit_location", "outs_when_up", "zone", "hit_distance_sc",
        "game_pk", "pitcher", "batter", "pitcher_1",
        "fielder_2", "fielder_2_1", "fielder_3", "fielder_4", "fielder_5",
        "fielder_6", "fielder_7", "fielder_8", "fielder_9",
        "on_1b", "on_2b", "on_3b",
        "home_score", "away_score", "bat_score", "fld_score",
        "post_away_score", "post_home_score", "post_bat_score", "post_fld_score",
        "launch_speed_angle",
    }
    statcast_double_cols = {
        "release_speed", "release_pos_x", "release_pos_z", "release_pos_y",
        "release_extension", "effective_speed", "release_spin_rate",
        "spin_dir", "spin_rate_deprecated", "break_angle_deprecated",
        "break_length_deprecated", "spin_axis",
        "pfx_x", "pfx_z", "plate_x", "plate_z",
        "vx0", "vy0", "vz0", "ax", "ay", "az",
        "sz_top", "sz_bot",
        "hc_x", "hc_y",
        "launch_speed", "launch_angle",
        "estimated_ba_using_speedangle", "estimated_woba_using_speedangle",
        "woba_value", "woba_denom", "babip_value", "iso_value",
        "delta_home_win_exp", "delta_run_exp",
        "bat_speed", "swing_length",
    }

    total = _load_table(
        "statcast_pitches", statcast,
        statcast_int_cols, statcast_double_cols,
        minio_client, staging_cur, lakehouse_cur, tmpdir,
    )
    return total


def upload_raw_csvs(minio_client):
    """Upload raw pitch CSV files to MinIO for reproducibility."""
    print("Uploading raw pitch CSVs to MinIO...", flush=True)
    count = 0
    for root, dirs, files in os.walk(DATA_DIR):
        for f in files:
            fpath = os.path.join(root, f)
            rel = os.path.relpath(fpath, os.path.dirname(DATA_DIR))
            minio_client.fput_object(BUCKET, rel, fpath)
            count += 1
    print(f"  Uploaded {count} pitch files to s3://{BUCKET}/pitch/", flush=True)


def main():
    t0 = time.time()
    print(f"Connecting to Trino at {TRINO_HOST}:{TRINO_PORT}", flush=True)
    print(f"MinIO at {MINIO_ENDPOINT}", flush=True)

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

    tmpdir = tempfile.mkdtemp(prefix="mlb-pitch-")

    total = 0
    total += load_historical(minio_client, staging_cur, lakehouse_cur, tmpdir)
    total += load_modern_statcast(minio_client, staging_cur, lakehouse_cur, tmpdir)

    upload_raw_csvs(minio_client)

    elapsed = time.time() - t0
    print(f"\nLoaded {total:,} pitch rows across 5 tables in {elapsed:.1f}s", flush=True)

    # Verification
    print("\nVerification:", flush=True)
    for table in ["pitch_games", "pitch_atbats", "pitch_pitches", "pitch_player_names", "statcast_pitches"]:
        try:
            lakehouse_cur.execute(f'SELECT COUNT(*) FROM lakehouse.mlb."{table}"')
            print(f"  {table}: {lakehouse_cur.fetchone()[0]:,} rows")
        except Exception as e:
            print(f"  {table}: ERROR - {e}")

    print("\nSample queries:", flush=True)
    try:
        lakehouse_cur.execute("""
            SELECT pitch_type, COUNT(*) as cnt,
                   ROUND(AVG(TRY_CAST(start_speed AS DOUBLE)), 1) as avg_speed
            FROM lakehouse.mlb.pitch_pitches
            WHERE pitch_type IS NOT NULL AND pitch_type != ''
            GROUP BY pitch_type
            ORDER BY cnt DESC LIMIT 5
        """)
        print("  Top 5 pitch types (2015-2019):")
        for r in lakehouse_cur.fetchall():
            print(f"    {r[0]}: {r[1]:,} pitches, avg {r[2]} mph")
    except Exception as e:
        print(f"  Pitch type query failed: {e}")

    shutil.rmtree(tmpdir, ignore_errors=True)
    staging_conn.close()
    lakehouse_conn.close()
    print("\nDone!", flush=True)


if __name__ == "__main__":
    main()
