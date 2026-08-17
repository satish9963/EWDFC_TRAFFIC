"""Runtime paths and portal settings.

Column names and project behaviour do not live here -- see core/schema.py and
projects.yaml. What remains is genuinely global: where the app may write, where
the route cache is, and how to reach the RBS portal.
"""
import os
import shutil
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent


def _env(name, legacy_name=None, default=None):
    """Read a RAIL_* variable, falling back to the older EWDFC_* spelling.

    The deployed app and its documentation used EWDFC_CACHE_DB and
    EWDFC_RUNTIME_DIR. Renaming them outright would break a running deployment
    for no benefit, so both are accepted.
    """
    value = os.environ.get(name)
    if value is None and legacy_name:
        value = os.environ.get(legacy_name)
    return value if value is not None else default


def _first_writable(candidate):
    """Return candidate if files can actually be created in it, else None."""
    try:
        candidate.mkdir(parents=True, exist_ok=True)
        probe = candidate / ".write_probe"
        probe.touch()
        probe.unlink()
        return candidate
    except OSError:
        return None


# Locally the app directory is writable and RUNTIME_DIR is just BASE_DIR. On
# hosts that mount the app read-only we fall back to a scratch directory so
# imports do not crash at startup.
RUNTIME_DIR = _first_writable(BASE_DIR) or Path(
    _env("RAIL_RUNTIME_DIR", "EWDFC_RUNTIME_DIR", "/tmp/rail-corridor")
)
RUNTIME_DIR.mkdir(parents=True, exist_ok=True)

DATA_DIR = RUNTIME_DIR / "data"
OUTPUT_DIR = RUNTIME_DIR / "output"
DATA_DIR.mkdir(parents=True, exist_ok=True)
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

CACHE_DB = Path(_env("RAIL_CACHE_DB", "EWDFC_CACHE_DB", RUNTIME_DIR / "cache.db"))

# Seed the route cache from the bundled copy the first time we run on a fresh
# host. The cache is corridor-independent, so every project benefits from it.
_SEED_CACHE = BASE_DIR / "cache.db"
if not CACHE_DB.exists() and _SEED_CACHE.exists() and _SEED_CACHE != CACHE_DB:
    shutil.copy2(_SEED_CACHE, CACHE_DB)

# Station gazetteer used for proximity matching; absent is fine.
GAZETTEER_PATH = Path(_env("RAIL_GAZETTEER", None, BASE_DIR / "data" / "stations.csv"))

RBS_URL = "https://rbs.indianrail.gov.in/ShortPath/ShortPathServlet"

APP_TITLE = "Rail Corridor Traffic Assessment"
APP_ICON = "🚂"

MAX_UPLOAD_ROWS_WARNING = 100_000
