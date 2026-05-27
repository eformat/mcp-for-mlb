#!/usr/bin/env python3
"""Load NOAA GHCN-D weather data into Trino Iceberg tables via Parquet.

Pipeline: CSV → Parquet (local) → MinIO S3 → Hive staging table → CTAS into Iceberg.

Usage:
    python scripts/load-weather-trino.py

Environment variables:
    TRINO_HOST      Trino coordinator host (default: localhost)
    TRINO_PORT      Trino coordinator port (default: 8080)
    MINIO_ENDPOINT  MinIO endpoint (default: localhost:9000)
    MINIO_ACCESS_KEY  Access key (default: minio)
    MINIO_SECRET_KEY  Secret key (default: minio1234)
    DATA_DIR        Path to weather CSV directory (default: data/weather)
"""

import os
import glob
import time
import tempfile
import shutil

import pandas as pd
from minio import Minio
from minio.deleteobjects import DeleteObject
from trino.dbapi import connect

TRINO_HOST = os.environ.get("TRINO_HOST", "localhost")
TRINO_PORT = int(os.environ.get("TRINO_PORT", "8080"))
MINIO_ENDPOINT = os.environ.get("MINIO_ENDPOINT", "localhost:9000")
MINIO_ACCESS_KEY = os.environ.get("MINIO_ACCESS_KEY", "minio")
MINIO_SECRET_KEY = os.environ.get("MINIO_SECRET_KEY", "minio1234")
DATA_DIR = os.environ.get("DATA_DIR", "data/weather")

BUCKET = "mlb-data"


def load_stations(minio_client, staging_cur, lakehouse_cur, tmpdir):
    """Load city_info.csv → Parquet → staging → Iceberg."""
    path = os.path.join(DATA_DIR, "city_info.csv")
    if not os.path.exists(path):
        print(f"  SKIP: {path} not found", flush=True)
        return 0

    print("  Loading weather_stations...", flush=True)
    df = pd.read_csv(path, encoding="utf-8-sig")
    df = df.rename(columns={
        "Name": "city_name", "ID": "station_id",
        "Lat": "latitude", "Lon": "longitude",
        "Stn.Name": "station_name", "Stn.stDate": "start_date", "Stn.edDate": "end_date",
    })
    # Keep only the columns we want
    df = df[["station_id", "city_name", "latitude", "longitude",
             "station_name", "start_date", "end_date"]]

    pq_path = os.path.join(tmpdir, "weather_stations.parquet")
    df.to_parquet(pq_path, index=False, engine="pyarrow")

    s3_dir = "parquet/weather_stations"
    old = list(minio_client.list_objects(BUCKET, prefix=f"{s3_dir}/", recursive=True))
    if old:
        list(minio_client.remove_objects(BUCKET, [DeleteObject(o.object_name) for o in old]))
    minio_client.fput_object(BUCKET, f"{s3_dir}/weather_stations.parquet", pq_path)

    staging_cur.execute("DROP TABLE IF EXISTS staging.mlb.weather_stations")
    staging_cur.execute("""
        CREATE TABLE staging.mlb.weather_stations (
            station_id VARCHAR, city_name VARCHAR,
            latitude DOUBLE, longitude DOUBLE,
            station_name VARCHAR, start_date VARCHAR, end_date VARCHAR
        )
        WITH (format = 'PARQUET', external_location = 's3://mlb-data/parquet/weather_stations/')
    """)

    lakehouse_cur.execute("DROP TABLE IF EXISTS lakehouse.mlb.weather_stations")
    lakehouse_cur.execute("""
        CREATE TABLE lakehouse.mlb.weather_stations AS
        SELECT * FROM staging.mlb.weather_stations
    """)

    lakehouse_cur.execute("SELECT COUNT(*) FROM lakehouse.mlb.weather_stations")
    count = lakehouse_cur.fetchone()[0]
    print(f"  weather_stations: {count} rows", flush=True)
    os.remove(pq_path)
    return count


def load_daily_weather(minio_client, staging_cur, lakehouse_cur, tmpdir):
    """Load all station CSVs → single Parquet → staging → Iceberg."""
    station_files = sorted(glob.glob(os.path.join(DATA_DIR, "US*.csv")))
    print(f"  Found {len(station_files)} weather station files", flush=True)

    # Read all CSVs into one dataframe
    t0 = time.time()
    print("  Reading all station CSVs...", flush=True)
    frames = []
    for fpath in station_files:
        station_id = os.path.splitext(os.path.basename(fpath))[0]
        df = pd.read_csv(fpath, encoding="utf-8-sig", low_memory=False,
                         na_values=["NA", ""], keep_default_na=True)
        df["station_id"] = station_id
        frames.append(df[["station_id", "Date", "tmax", "tmin", "prcp"]])
    all_data = pd.concat(frames, ignore_index=True)
    all_data = all_data.rename(columns={"Date": "observation_date"})
    # Ensure observation_date is string
    all_data["observation_date"] = all_data["observation_date"].astype(str)
    t_read = time.time() - t0
    print(f"  Read {len(all_data):,} rows in {t_read:.1f}s", flush=True)

    # Write as multiple Parquet files for parallel upload/read (split by ~1M rows)
    t0 = time.time()
    chunk_size = 1_000_000
    total = len(all_data)
    s3_dir = "parquet/weather_daily"

    # Clean old
    old = list(minio_client.list_objects(BUCKET, prefix=f"{s3_dir}/", recursive=True))
    if old:
        list(minio_client.remove_objects(BUCKET, [DeleteObject(o.object_name) for o in old]))

    n_chunks = (total + chunk_size - 1) // chunk_size
    print(f"  Writing {n_chunks} Parquet chunks and uploading to MinIO...", flush=True)
    for i in range(n_chunks):
        start = i * chunk_size
        end = min(start + chunk_size, total)
        chunk = all_data.iloc[start:end]
        pq_name = f"weather_daily_{i:03d}.parquet"
        pq_path = os.path.join(tmpdir, pq_name)
        chunk.to_parquet(pq_path, index=False, engine="pyarrow")
        minio_client.fput_object(BUCKET, f"{s3_dir}/{pq_name}", pq_path)
        os.remove(pq_path)
        print(f"    Chunk {i+1}/{n_chunks}: {end-start:,} rows uploaded", flush=True)

    t_upload = time.time() - t0
    print(f"  Parquet upload complete in {t_upload:.1f}s", flush=True)

    # Create staging table
    staging_cur.execute("DROP TABLE IF EXISTS staging.mlb.weather_daily")
    staging_cur.execute("""
        CREATE TABLE staging.mlb.weather_daily (
            station_id VARCHAR, observation_date VARCHAR,
            tmax DOUBLE, tmin DOUBLE, prcp DOUBLE
        )
        WITH (format = 'PARQUET', external_location = 's3://mlb-data/parquet/weather_daily/')
    """)

    # Verify staging count
    staging_cur.execute("SELECT COUNT(*) FROM staging.mlb.weather_daily")
    staging_count = staging_cur.fetchone()[0]
    print(f"  staging.mlb.weather_daily: {staging_count:,} rows", flush=True)

    # CTAS into Iceberg
    t0 = time.time()
    print("  Creating Iceberg table (CTAS)...", flush=True)
    lakehouse_cur.execute("DROP TABLE IF EXISTS lakehouse.mlb.weather_daily")
    lakehouse_cur.execute("""
        CREATE TABLE lakehouse.mlb.weather_daily AS
        SELECT * FROM staging.mlb.weather_daily
    """)

    lakehouse_cur.execute("SELECT COUNT(*) FROM lakehouse.mlb.weather_daily")
    count = lakehouse_cur.fetchone()[0]
    t_ctas = time.time() - t0
    print(f"  weather_daily: {count:,} rows (CTAS in {t_ctas:.1f}s)", flush=True)
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

    tmpdir = tempfile.mkdtemp(prefix="mlb-weather-")

    station_count = load_stations(minio_client, staging_cur, lakehouse_cur, tmpdir)
    daily_count = load_daily_weather(minio_client, staging_cur, lakehouse_cur, tmpdir)

    elapsed = time.time() - t0
    print(f"\nLoaded {station_count + daily_count:,} rows in {elapsed:.1f}s", flush=True)

    # Verification
    print("\nVerification:", flush=True)
    lakehouse_cur.execute("""
        SELECT station_id, COUNT(*) as days,
               MIN(observation_date) as earliest,
               MAX(observation_date) as latest
        FROM lakehouse.mlb.weather_daily
        GROUP BY station_id
        ORDER BY days DESC LIMIT 5
    """)
    print("  Top 5 stations by record count:")
    for row in lakehouse_cur.fetchall():
        print(f"    {row[0]}: {row[1]:,} days ({row[2]} to {row[3]})")

    shutil.rmtree(tmpdir, ignore_errors=True)
    staging_conn.close()
    lakehouse_conn.close()
    print("\nDone!", flush=True)


if __name__ == "__main__":
    main()
