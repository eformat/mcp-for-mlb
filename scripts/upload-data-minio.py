#!/usr/bin/env python3
"""Upload raw CSV data files to MinIO S3 bucket.

Uploads all baseball and weather CSVs plus README files
to the mlb-data bucket for reproducibility.

Usage:
    python scripts/upload-data-minio.py

Environment variables:
    MINIO_ENDPOINT  MinIO endpoint (default: localhost:9000)
    MINIO_ACCESS_KEY  Access key (default: minio)
    MINIO_SECRET_KEY  Secret key (default: minio1234)
    DATA_DIR          Base data directory (default: data)
"""

import os
import glob

from minio import Minio

MINIO_ENDPOINT = os.environ.get("MINIO_ENDPOINT", "localhost:9000")
MINIO_ACCESS_KEY = os.environ.get("MINIO_ACCESS_KEY", "minio")
MINIO_SECRET_KEY = os.environ.get("MINIO_SECRET_KEY", "minio1234")
DATA_DIR = os.environ.get("DATA_DIR", "data")
BUCKET = "mlb-data"


def main():
    client = Minio(MINIO_ENDPOINT, access_key=MINIO_ACCESS_KEY,
                   secret_key=MINIO_SECRET_KEY, secure=False)

    if not client.bucket_exists(BUCKET):
        client.make_bucket(BUCKET)
        print(f"Created bucket: {BUCKET}")

    count = 0
    for subdir in ["baseball", "weather"]:
        pattern = os.path.join(DATA_DIR, subdir, "*")
        for fpath in sorted(glob.glob(pattern)):
            if os.path.isfile(fpath):
                name = os.path.basename(fpath)
                obj_name = f"{subdir}/{name}"
                client.fput_object(BUCKET, obj_name, fpath)
                count += 1
                if count % 50 == 0:
                    print(f"  Uploaded {count} files...", flush=True)

    print(f"Uploaded {count} files to s3://{BUCKET}/", flush=True)


if __name__ == "__main__":
    main()
