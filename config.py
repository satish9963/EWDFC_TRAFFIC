import os
import shutil
from pathlib import Path

# Paths
BASE_DIR = Path(__file__).resolve().parent


def _first_writable(candidate):
    """Return candidate if we can actually create files in it, else None."""
    try:
        candidate.mkdir(parents=True, exist_ok=True)
        probe = candidate / ".ewdfc_write_probe"
        probe.touch()
        probe.unlink()
        return candidate
    except OSError:
        return None


# Locally the app directory is writable, so RUNTIME_DIR is just BASE_DIR and
# every path below is unchanged. On hosts that mount the app read-only we fall
# back to a scratch directory so imports don't crash at startup.
RUNTIME_DIR = _first_writable(BASE_DIR) or Path(os.environ.get("EWDFC_RUNTIME_DIR", "/tmp/ewdfc"))
RUNTIME_DIR.mkdir(parents=True, exist_ok=True)

DATA_DIR = RUNTIME_DIR / "data"
OUTPUT_DIR = RUNTIME_DIR / "output"
CACHE_DB = Path(os.environ.get("EWDFC_CACHE_DB", RUNTIME_DIR / "cache.db"))

# Create directories if they don't exist
DATA_DIR.mkdir(parents=True, exist_ok=True)
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

# Seed the route cache from the bundled copy the first time we run on a fresh host
_SEED_CACHE = BASE_DIR / "cache.db"
if not CACHE_DB.exists() and _SEED_CACHE.exists() and _SEED_CACHE != CACHE_DB:
    shutil.copy2(_SEED_CACHE, CACHE_DB)

# Default Input Paths (can be overridden via UI)
DEFAULT_OD_FILE = DATA_DIR / "OD_Traffic.xlsx"
DEFAULT_EWDFC_KML = DATA_DIR / "EWDFC_Alignment.kml"
DEFAULT_DFC_STATIONS = DATA_DIR / "DFC_Stations.xlsx"

# RBS Portal URL
RBS_URL = "https://rbs.indianrail.gov.in/ShortPath/ShortPathServlet"

# Application Settings
DEFAULT_THRESHOLD = 3
SUPPORTED_THRESHOLDS = [2, 3]

# Data columns expected
OD_COLUMNS = [
    "From Station Code",
    "From Station Name",
    "To Station Code",
    "To Station Name",
    "Commodity",
    "Annual Tonnage",
    "No. of Rakes / Wagon Units"
]

DFC_STATION_COLUMNS = [
    "DFC Station Code",
    "Station Name",
    "Chainage",
    "Latitude",
    "Longitude"
]
