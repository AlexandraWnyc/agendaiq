"""
backup_db.py — nightly backup of the SQLite DB to off-site storage (S3 or Backblaze B2).

Run as a Render Cron Job (or any cron). Needs boto3 in requirements.txt and the
following env vars set on the cron service:

  AWS_ACCESS_KEY_ID       - your S3 / B2 access key
  AWS_SECRET_ACCESS_KEY   - your S3 / B2 secret
  AWS_DEFAULT_REGION      - e.g. us-east-1
  BACKUP_BUCKET           - bucket name
  BACKUP_ENDPOINT_URL     - (optional) e.g. https://s3.us-east-005.backblazeb2.com
                            for Backblaze or any S3-compatible provider
  DATA_DIR                - /data (same as the web service)

Retention: keeps the last 30 daily + 12 monthly. Adjust RETAIN_* below.
"""
import os, sys, gzip, shutil, sqlite3, logging
from datetime import datetime
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("backup")

RETAIN_DAILY = 30
RETAIN_MONTHLY = 12


def snapshot_db(src: Path, dst: Path):
    """Consistent snapshot using SQLite backup API (safe while app is writing)."""
    src_conn = sqlite3.connect(str(src))
    dst_conn = sqlite3.connect(str(dst))
    with dst_conn:
        src_conn.backup(dst_conn)
    src_conn.close()
    dst_conn.close()


def gzip_file(path: Path) -> Path:
    gz = path.with_suffix(path.suffix + ".gz")
    with open(path, "rb") as f_in, gzip.open(gz, "wb", compresslevel=6) as f_out:
        shutil.copyfileobj(f_in, f_out)
    path.unlink(missing_ok=True)
    return gz


def upload(local: Path, bucket: str, key: str, endpoint: str | None):
    import boto3
    kwargs = {}
    if endpoint:
        kwargs["endpoint_url"] = endpoint
    s3 = boto3.client("s3", **kwargs)
    s3.upload_file(str(local), bucket, key)
    log.info(f"Uploaded {local.name} → s3://{bucket}/{key}")


def prune(bucket: str, prefix: str, keep: int, endpoint: str | None):
    import boto3
    kwargs = {}
    if endpoint:
        kwargs["endpoint_url"] = endpoint
    s3 = boto3.client("s3", **kwargs)
    resp = s3.list_objects_v2(Bucket=bucket, Prefix=prefix)
    items = sorted(resp.get("Contents", []), key=lambda o: o["LastModified"], reverse=True)
    for old in items[keep:]:
        s3.delete_object(Bucket=bucket, Key=old["Key"])
        log.info(f"Pruned old backup {old['Key']}")


def main():
    data_dir = Path(os.environ.get("DATA_DIR", ".")).resolve()
    db_path = data_dir / "oca_agenda.db"
    bucket = os.environ.get("BACKUP_BUCKET")
    endpoint = os.environ.get("BACKUP_ENDPOINT_URL") or None

    if not db_path.exists():
        log.error(f"DB not found at {db_path}"); sys.exit(1)
    if not bucket:
        log.error("BACKUP_BUCKET not set"); sys.exit(1)

    stamp = datetime.utcnow().strftime("%Y-%m-%d_%H%M%SZ")
    tmp = data_dir / f"oca_agenda_{stamp}.db"
    snapshot_db(db_path, tmp)
    gz = gzip_file(tmp)

    # Daily snapshot
    daily_key = f"daily/oca_agenda_{stamp}.db.gz"
    upload(gz, bucket, daily_key, endpoint)

    # Monthly snapshot on the 1st
    if datetime.utcnow().day == 1:
        monthly_key = f"monthly/oca_agenda_{stamp}.db.gz"
        upload(gz, bucket, monthly_key, endpoint)

    gz.unlink(missing_ok=True)

    prune(bucket, "daily/",   RETAIN_DAILY,   endpoint)
    prune(bucket, "monthly/", RETAIN_MONTHLY, endpoint)
    log.info("Backup complete.")


if __name__ == "__main__":
    main()
