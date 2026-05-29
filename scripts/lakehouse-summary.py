#!/usr/bin/env python3
"""Show a summary of all tables and row counts in the Trino lakehouse.

Usage:
    python scripts/lakehouse-summary.py

Environment variables:
    TRINO_HOST  Trino coordinator host (default: localhost)
    TRINO_PORT  Trino coordinator port (default: 8080)
"""

import os
import sys

from trino.dbapi import connect

TRINO_HOST = os.environ.get("TRINO_HOST", "localhost")
TRINO_PORT = int(os.environ.get("TRINO_PORT", "8080"))

CATALOGS = ["lakehouse", "staging"]


def get_tables(cur, catalog, schema):
    cur.execute(f"SHOW TABLES FROM {catalog}.{schema}")
    return [row[0] for row in cur.fetchall()]


def get_row_count(cur, catalog, schema, table):
    try:
        cur.execute(f"SELECT COUNT(*) FROM {catalog}.{schema}.{table}")
        return cur.fetchone()[0]
    except Exception:
        return "error"


def format_count(n):
    if isinstance(n, str):
        return n
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}K"
    return str(n)


def main():
    conn = connect(host=TRINO_HOST, port=TRINO_PORT, user="admin")
    cur = conn.cursor()

    grand_total = 0
    table_count = 0

    for catalog in CATALOGS:
        try:
            cur.execute(f"SHOW SCHEMAS FROM {catalog}")
            schemas = [row[0] for row in cur.fetchall()
                       if row[0] not in ("information_schema",)]
        except Exception:
            continue

        for schema in schemas:
            tables = get_tables(cur, catalog, schema)
            if not tables:
                continue

            print(f"\n  {catalog}.{schema}")
            print(f"  {'─' * 50}")

            for table in sorted(tables):
                count = get_row_count(cur, catalog, schema, table)
                print(f"    {table:<40} {format_count(count):>10}")
                if isinstance(count, int):
                    grand_total += count
                table_count += 1

    print(f"\n  {'═' * 52}")
    print(f"  {table_count} tables, {format_count(grand_total)} total rows\n")

    cur.close()
    conn.close()


if __name__ == "__main__":
    main()
