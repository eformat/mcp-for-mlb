#!/usr/bin/env python3
"""Load hotel reviews dataset from CSV into Trino Iceberg table on MinIO S3."""

import csv
import sys
from trino.dbapi import connect

SERVER = "localhost"
PORT = 8080
CATALOG = "lakehouse"
SCHEMA = "reviews"
TABLE = "hotel_reviews"
CSV_PATH = "/tmp/hotel-reviews/balanced_dataset.csv"
BATCH_SIZE = 500

conn = connect(host=SERVER, port=PORT, user="admin", catalog=CATALOG, schema=SCHEMA)
cur = conn.cursor()

print(f"Creating schema {CATALOG}.{SCHEMA}...")
cur.execute(f"CREATE SCHEMA IF NOT EXISTS {CATALOG}.{SCHEMA}")

print(f"Creating table {TABLE}...")
cur.execute(f"DROP TABLE IF EXISTS {CATALOG}.{SCHEMA}.{TABLE}")
cur.execute(f"""
    CREATE TABLE {CATALOG}.{SCHEMA}.{TABLE} (
        review VARCHAR,
        label VARCHAR
    )
""")

print(f"Loading data from {CSV_PATH}...")
with open(CSV_PATH, newline="", encoding="utf-8") as f:
    reader = csv.DictReader(f)
    batch = []
    total = 0
    for row in reader:
        review = row["review"].replace("'", "''")
        label = row["label"].replace("'", "''")
        batch.append(f"('{review}', '{label}')")
        if len(batch) >= BATCH_SIZE:
            values = ", ".join(batch)
            cur.execute(f"INSERT INTO {CATALOG}.{SCHEMA}.{TABLE} VALUES {values}")
            total += len(batch)
            print(f"  Inserted {total} rows...")
            batch = []
    if batch:
        values = ", ".join(batch)
        cur.execute(f"INSERT INTO {CATALOG}.{SCHEMA}.{TABLE} VALUES {values}")
        total += len(batch)

print(f"Done. {total} rows loaded into {CATALOG}.{SCHEMA}.{TABLE}")

cur.execute(f"SELECT count(*) FROM {CATALOG}.{SCHEMA}.{TABLE}")
count = cur.fetchone()[0]
print(f"Verified: {count} rows in table")

cur.close()
conn.close()
