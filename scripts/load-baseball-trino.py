#!/usr/bin/env python3
"""Load Lahman Baseball Database CSV files into Trino Iceberg tables via Parquet.

Pipeline: CSV → Parquet (local) → MinIO S3 → Hive staging table → CTAS into Iceberg.
This is orders of magnitude faster than row-by-row INSERT.

Usage:
    python scripts/load-baseball-trino.py

Environment variables:
    TRINO_HOST      Trino coordinator host (default: localhost)
    TRINO_PORT      Trino coordinator port (default: 8080)
    MINIO_ENDPOINT  MinIO endpoint (default: localhost:9000)
    MINIO_ACCESS_KEY  Access key (default: minio)
    MINIO_SECRET_KEY  Secret key (default: minio1234)
    DATA_DIR        Path to baseball CSV directory (default: data/baseball)
"""

import os
import sys
import time
import tempfile
import shutil

import pandas as pd
import pyarrow.parquet as pq
from minio import Minio
from minio.deleteobjects import DeleteObject
from trino.dbapi import connect

TRINO_HOST = os.environ.get("TRINO_HOST", "localhost")
TRINO_PORT = int(os.environ.get("TRINO_PORT", "8080"))
MINIO_ENDPOINT = os.environ.get("MINIO_ENDPOINT", "localhost:9000")
MINIO_ACCESS_KEY = os.environ.get("MINIO_ACCESS_KEY", "minio")
MINIO_SECRET_KEY = os.environ.get("MINIO_SECRET_KEY", "minio1234")
DATA_DIR = os.environ.get("DATA_DIR", "data/baseball")

BUCKET = "mlb-data"
PARQUET_PREFIX = "parquet"

COLUMN_RENAMES = {"2B": "doubles", "3B": "triples"}

BOM_STRIP = {
    "﻿ID": "ID",
    "﻿yearID": "yearID",
    "﻿schoolID": "schoolID",
}

TABLES = {
    "AllstarFull":        {"table": "allstar_full",        "integers": ["yearID","gameNum","GP","startingPos"]},
    "Appearances":        {"table": "appearances",         "integers": ["yearID","G_all","GS","G_batting","G_defense","G_p","G_c","G_1b","G_2b","G_3b","G_ss","G_lf","G_cf","G_rf","G_of","G_dh","G_ph","G_pr"]},
    "AwardsManagers":     {"table": "awards_managers",      "integers": ["yearID"]},
    "AwardsPlayers":      {"table": "awards_players",       "integers": ["yearID"]},
    "AwardsShareManagers":{"table": "awards_share_managers", "integers": ["yearID","pointsMax"], "doubles": ["pointsWon","votesFirst"]},
    "AwardsSharePlayers": {"table": "awards_share_players", "integers": ["yearID","pointsMax"], "doubles": ["pointsWon","votesFirst"]},
    "Batting":            {"table": "batting",             "integers": ["yearID","stint","G","AB","R","H","doubles","triples","HR","RBI","SB","CS","BB","SO","IBB","HBP","SH","SF","GIDP"]},
    "BattingPost":        {"table": "batting_post",        "integers": ["yearID","G","AB","R","H","doubles","triples","HR","RBI","SB","CS","BB","SO","IBB","HBP","SH","SF","GIDP"]},
    "CollegePlaying":     {"table": "college_playing",     "integers": ["yearID"]},
    "Fielding":           {"table": "fielding",            "integers": ["yearID","stint","G","GS","InnOuts","PO","A","E","DP","PB","WP","SB","CS"], "doubles": ["ZR"]},
    "FieldingOF":         {"table": "fielding_of",         "integers": ["yearID","stint","Glf","Gcf","Grf"]},
    "FieldingOFsplit":    {"table": "fielding_of_split",   "integers": ["yearID","stint","G","GS","InnOuts","PO","A","E","DP","PB","WP","SB","CS"], "doubles": ["ZR"]},
    "FieldingPost":       {"table": "fielding_post",       "integers": ["yearID","G","GS","InnOuts","PO","A","E","DP","TP","PB","SB","CS"]},
    "HallOfFame":         {"table": "hall_of_fame",        "integers": ["yearid","ballots","needed","votes"]},
    "HomeGames":          {"table": "home_games",          "integers": ["yearkey","games","openings","attendance"]},
    "Managers":           {"table": "managers",            "integers": ["yearID","inseason","G","W","L","rank"]},
    "ManagersHalf":       {"table": "managers_half",       "integers": ["yearID","inseason","half","G","W","L","rank"]},
    "Parks":              {"table": "parks",               "integers": ["ID"]},
    "People":             {"table": "people",              "integers": ["ID","birthYear","birthMonth","birthDay","deathYear","deathMonth","deathDay","weight","height"]},
    "Pitching":           {"table": "pitching",            "integers": ["yearID","stint","W","L","G","GS","CG","SHO","SV","IPouts","H","ER","HR","BB","SO","IBB","WP","HBP","BK","BFP","GF","R","SH","SF","GIDP"], "doubles": ["BAOpp","ERA"]},
    "PitchingPost":       {"table": "pitching_post",       "integers": ["yearID","W","L","G","GS","CG","SHO","SV","IPouts","H","ER","HR","BB","SO","IBB","WP","HBP","BK","BFP","GF","R","SH","SF","GIDP"], "doubles": ["BAOpp","ERA"]},
    "Salaries":           {"table": "salaries",            "integers": ["yearID"], "doubles": ["salary"]},
    "Schools":            {"table": "schools",             "integers": []},
    "SeriesPost":         {"table": "series_post",         "integers": ["yearID","wins","losses","ties"]},
    "Teams":              {"table": "teams",               "integers": ["yearID","Rank","G","Ghome","W","L","R","AB","H","doubles","triples","HR","BB","SO","SB","CS","HBP","SF","RA","ER","CG","SHO","SV","IPouts","HA","HRA","BBA","SOA","E","DP","attendance","BPF","PPF"], "doubles": ["ERA","FP"]},
    "TeamsFranchises":    {"table": "teams_franchises",    "integers": []},
    "TeamsHalf":          {"table": "teams_half",          "integers": ["yearID","Half","Rank","G","W","L"]},
}


def _build_ctas_columns(df, int_cols, dbl_cols):
    """Build SELECT column list with CAST for CTAS into Iceberg.

    Uses TRY_CAST for numeric columns to handle dirty data gracefully (returns NULL).
    """
    parts = []
    for col in df.columns:
        if col in int_cols:
            parts.append(f'TRY_CAST("{col}" AS INTEGER) AS "{col}"')
        elif col in dbl_cols:
            parts.append(f'TRY_CAST("{col}" AS DOUBLE) AS "{col}"')
        else:
            parts.append(f'CAST("{col}" AS VARCHAR) AS "{col}"')
    return ", ".join(parts)


def _build_staging_columns(df, int_cols, dbl_cols):
    """Build column definitions for Hive staging table matching actual Parquet types.

    Pandas writes nullable int columns as float64, so we must declare them
    as DOUBLE in the staging table regardless of our target type.
    """
    parts = []
    for col in df.columns:
        pd_dtype = df[col].dtype
        if pd_dtype == "float64":
            parts.append(f'"{col}" DOUBLE')
        elif pd_dtype == "int64":
            parts.append(f'"{col}" BIGINT')
        elif pd_dtype == "object":
            parts.append(f'"{col}" VARCHAR')
        else:
            parts.append(f'"{col}" VARCHAR')
    return ", ".join(parts)


def load_table(csv_name, spec, minio_client, staging_cur, lakehouse_cur, tmpdir):
    table = spec["table"]
    int_cols = set(spec.get("integers", []))
    dbl_cols = set(spec.get("doubles", []))

    csv_path = os.path.join(DATA_DIR, f"{csv_name}.csv")
    if not os.path.exists(csv_path):
        print(f"  SKIP: {csv_path} not found", flush=True)
        return 0

    # 1. Read CSV
    df = pd.read_csv(csv_path, encoding="utf-8-sig", low_memory=False)
    df.columns = [BOM_STRIP.get(c, c) for c in df.columns]
    df.rename(columns=COLUMN_RENAMES, inplace=True)

    # Pandas reads int columns with NaN as float — that's fine, Parquet handles it.
    # We just need to make sure string columns don't have NaN objects.
    str_cols = [c for c in df.columns if c not in int_cols and c not in dbl_cols]
    for c in str_cols:
        df[c] = df[c].astype(object).where(df[c].notna(), None)

    # 2. Write Parquet
    pq_path = os.path.join(tmpdir, f"{table}.parquet")
    df.to_parquet(pq_path, index=False, engine="pyarrow")

    # 3. Upload to MinIO
    s3_dir = f"{PARQUET_PREFIX}/{table}"
    s3_key = f"{s3_dir}/{table}.parquet"
    # Clean old files in this prefix
    old = list(minio_client.list_objects(BUCKET, prefix=f"{s3_dir}/", recursive=True))
    if old:
        list(minio_client.remove_objects(BUCKET, [DeleteObject(o.object_name) for o in old]))
    minio_client.fput_object(BUCKET, s3_key, pq_path)

    # 4. Create Hive staging table
    staging_cur.execute(f'DROP TABLE IF EXISTS staging.mlb."{table}"')
    col_defs = _build_staging_columns(df, int_cols, dbl_cols)
    staging_cur.execute(f"""
        CREATE TABLE staging.mlb."{table}" ({col_defs})
        WITH (format = 'PARQUET', external_location = 's3://{BUCKET}/{s3_dir}/')
    """)

    # 5. CTAS into Iceberg
    lakehouse_cur.execute(f'DROP TABLE IF EXISTS lakehouse.mlb."{table}"')
    select_cols = _build_ctas_columns(df, int_cols, dbl_cols)
    lakehouse_cur.execute(f"""
        CREATE TABLE lakehouse.mlb."{table}" AS
        SELECT {select_cols} FROM staging.mlb."{table}"
    """)

    # Verify
    lakehouse_cur.execute(f'SELECT COUNT(*) FROM lakehouse.mlb."{table}"')
    count = lakehouse_cur.fetchone()[0]
    print(f"  {table}: {count:,} rows", flush=True)

    # Cleanup local parquet
    os.remove(pq_path)
    return count


def main():
    t0 = time.time()
    print(f"Connecting to Trino at {TRINO_HOST}:{TRINO_PORT}", flush=True)
    print(f"MinIO at {MINIO_ENDPOINT}", flush=True)

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

    tmpdir = tempfile.mkdtemp(prefix="mlb-parquet-")
    grand_total = 0

    for csv_name, spec in TABLES.items():
        count = load_table(csv_name, spec, minio_client, staging_cur, lakehouse_cur, tmpdir)
        grand_total += count

    elapsed = time.time() - t0
    print(f"\nLoaded {grand_total:,} rows across {len(TABLES)} tables in {elapsed:.1f}s", flush=True)

    # Verification
    print("\nSample queries:", flush=True)
    try:
        lakehouse_cur.execute("""
            SELECT p.nameFirst, p.nameLast, SUM(b.HR) as career_hr
            FROM lakehouse.mlb.batting b
            JOIN lakehouse.mlb.people p ON b.playerID = p.playerID
            GROUP BY p.nameFirst, p.nameLast
            ORDER BY career_hr DESC LIMIT 5
        """)
        print("  Top 5 career HR leaders:")
        for row in lakehouse_cur.fetchall():
            print(f"    {row[0]} {row[1]}: {row[2]}")
    except Exception as e:
        print(f"  HR query failed: {e}")

    try:
        lakehouse_cur.execute("""
            SELECT yearID, teamIDwinner, teamIDloser, wins, losses
            FROM lakehouse.mlb.series_post WHERE round = 'WS'
            ORDER BY yearID DESC LIMIT 5
        """)
        print("  Last 5 World Series:")
        for row in lakehouse_cur.fetchall():
            print(f"    {row[0]}: {row[1]} beat {row[2]} ({row[3]}-{row[4]})")
    except Exception as e:
        print(f"  WS query failed: {e}")

    shutil.rmtree(tmpdir, ignore_errors=True)
    staging_conn.close()
    lakehouse_conn.close()
    print("\nDone!", flush=True)


if __name__ == "__main__":
    main()
