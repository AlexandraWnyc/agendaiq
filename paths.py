"""
paths.py — central location for all filesystem paths.

Locally, everything lives next to the source (backwards compatible).
In the cloud, set DATA_DIR=/data (Render Disk / Railway Volume mount point) and all
state — SQLite DB, pdf_cache, exports, config — goes there so it survives redeploys.
"""
import os
from pathlib import Path

# Project source dir (where app_v6.py, scraper.py, etc. live)
PROJECT_DIR = Path(__file__).parent.resolve()

# Where persistent state lives. Cloud: /data. Local: project dir.
DATA_DIR = Path(os.environ.get("DATA_DIR", str(PROJECT_DIR))).resolve()

# Create subdirs on first import (idempotent)
PDF_CACHE_DIR = DATA_DIR / "pdf_cache"
OUTPUT_DIR    = DATA_DIR / "output"
EXPORTS_DIR   = DATA_DIR / "exports"
for _d in (DATA_DIR, PDF_CACHE_DIR, OUTPUT_DIR, EXPORTS_DIR):
    _d.mkdir(parents=True, exist_ok=True)

DB_PATH        = DATA_DIR / "oca_agenda.db"
CONFIG_PATH    = DATA_DIR / "oca_config.json"

# If a legacy oca_config.json exists in the project dir and none in DATA_DIR,
# copy it once so cloud deploys pick up local defaults.
_legacy_cfg = PROJECT_DIR / "oca_config.json"
if _legacy_cfg.exists() and not CONFIG_PATH.exists() and PROJECT_DIR != DATA_DIR:
    try:
        CONFIG_PATH.write_text(_legacy_cfg.read_text())
    except Exception:
        pass
