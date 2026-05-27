#!/usr/bin/env python3
"""Load NOSIBLE/financial-sentiment dataset from Parquet into Trino Iceberg table on MinIO S3."""

import pandas as pd
from trino.dbapi import connect

SERVER = "localhost"
PORT = 8080
CATALOG = "lakehouse"
SCHEMA = "finance"
TABLE = "financial_news"
PARQUET_PATH = "/tmp/financial-sentiment/data.parquet"
BATCH_SIZE = 500

conn = connect(host=SERVER, port=PORT, user="admin", catalog=CATALOG, schema=SCHEMA)
cur = conn.cursor()

print(f"Creating schema {CATALOG}.{SCHEMA}...")
cur.execute(f"CREATE SCHEMA IF NOT EXISTS {CATALOG}.{SCHEMA}")

print(f"Creating table {TABLE}...")
cur.execute(f"DROP TABLE IF EXISTS {CATALOG}.{SCHEMA}.{TABLE}")
cur.execute(f"""
    CREATE TABLE {CATALOG}.{SCHEMA}.{TABLE} (
        text VARCHAR,
        label VARCHAR,
        source VARCHAR,
        url VARCHAR
    )
""")

print(f"Loading data from {PARQUET_PATH}...")
df = pd.read_parquet(PARQUET_PATH)
df = df.rename(columns={"netloc": "source"})

total = 0
for start in range(0, len(df), BATCH_SIZE):
    batch = df.iloc[start:start + BATCH_SIZE]
    values = ", ".join(
        f"({v})"
        for _, row in batch.iterrows()
        for v in [", ".join(
            f"'{str(val).replace(chr(39), chr(39)+chr(39))}'" for val in row
        )]
    )
    cur.execute(f"INSERT INTO {CATALOG}.{SCHEMA}.{TABLE} VALUES {values}")
    total += len(batch)
    if total % 5000 == 0 or total == len(df):
        print(f"  Inserted {total}/{len(df)} rows...")

print(f"Done. {total} rows loaded into {CATALOG}.{SCHEMA}.{TABLE}")

cur.execute(f"SELECT count(*) FROM {CATALOG}.{SCHEMA}.{TABLE}")
count = cur.fetchone()[0]
print(f"Verified: {count} rows in table")

cur.close()
conn.close()
