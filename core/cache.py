"""SQLite cache of RBS shortest-path lookups.

The cache is deliberately project-neutral. An Indian Railways shortest path
between two stations does not depend on which corridor is being assessed, so
every project shares one cache and benefits from the others' lookups.

Schema history
--------------
v1 (unversioned)  source, destination, distance, route_sequence, junctions,
                  status. No time dimension at all, so a row fetched once was
                  served forever and there was no expiry to tune.
v2                adds fetched_at / gauge / basis / http_status. Rows carried
                  over from v1 keep fetched_at = NULL, meaning "legacy, age
                  unknown" -- treated as stale by any max_age policy.
"""
import json
import sqlite3
import threading
from datetime import datetime, timedelta, timezone

from config import CACHE_DB
from core.schema import normalise_station_code

SCHEMA_VERSION = 2

# Columns added after v1. Applied with ALTER TABLE so an existing cache is
# upgraded in place rather than rebuilt.
_V2_COLUMNS = {
    "fetched_at": "TEXT",    # ISO-8601 UTC. NULL means legacy/unknown age.
    "gauge": "TEXT",         # gaugeType sent to the portal, e.g. 'S'
    "basis": "TEXT",         # distance basis, e.g. 'goods'
    "http_status": "INTEGER",
}


def utc_now():
    """Timestamp used for every write. UTC, so caches merge across machines."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class RBSCache:
    _lock = threading.Lock()

    def __init__(self, db_path=CACHE_DB):
        self.db_path = db_path
        self._init_db()

    def _connect(self):
        conn = sqlite3.connect(self.db_path, timeout=30)
        # Deliberately NOT journal_mode=WAL. WAL needs a shared-memory file
        # beside the database, which Windows drives mounted into WSL (DrvFs)
        # and most network shares cannot create -- and this app is normally run
        # from exactly such a drive. Worse, the mode is recorded in the file
        # header, so a failed attempt leaves the database unopenable until the
        # header is reset. Rollback journalling works everywhere, and the only
        # thing given up is reading during a concurrent write, which the
        # refresh tool avoids anyway by working on its own copy.
        conn.execute("PRAGMA synchronous=NORMAL")
        return conn

    def _init_db(self):
        with self._lock:
            with self._connect() as conn:
                conn.execute('''
                    CREATE TABLE IF NOT EXISTS rbs_routes (
                        source TEXT,
                        destination TEXT,
                        distance REAL,
                        route_sequence TEXT,
                        junctions TEXT,
                        status TEXT,
                        PRIMARY KEY (source, destination)
                    )
                ''')
                existing = {r[1] for r in conn.execute("PRAGMA table_info(rbs_routes)")}
                for column, decl in _V2_COLUMNS.items():
                    if column not in existing:
                        conn.execute(f"ALTER TABLE rbs_routes ADD COLUMN {column} {decl}")
                conn.execute('''
                    CREATE TABLE IF NOT EXISTS cache_meta (
                        key TEXT PRIMARY KEY,
                        value TEXT
                    )
                ''')
                conn.execute(
                    "INSERT OR REPLACE INTO cache_meta (key, value) VALUES ('schema_version', ?)",
                    (str(SCHEMA_VERSION),),
                )
                # Cheap lookup for "what is stale" without scanning route blobs.
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_routes_fetched_at ON rbs_routes(fetched_at)"
                )
                conn.commit()

    # -- reads ------------------------------------------------------------

    def get_route(self, source, destination, max_age_days=None):
        """Return a cached route, or None.

        With max_age_days set, a row older than that -- and any legacy row,
        whose age is unknown -- is reported as a miss so the caller refetches.
        """
        with self._lock:
            with self._connect() as conn:
                row = conn.execute('''
                    SELECT distance, route_sequence, junctions, status, fetched_at
                    FROM rbs_routes
                    WHERE source = ? AND destination = ?
                ''', (source, destination)).fetchone()

        if not row:
            return None
        if max_age_days is not None and self._is_stale(row[4], max_age_days):
            return None
        return {
            "distance": row[0],
            "route_sequence": json.loads(row[1]) if row[1] else [],
            "junctions": json.loads(row[2]) if row[2] else [],
            "status": row[3],
            "fetched_at": row[4],
        }

    @staticmethod
    def _is_stale(fetched_at, max_age_days):
        if not fetched_at:
            return True  # legacy row: age unknown, so assume stale
        try:
            stamped = datetime.fromisoformat(fetched_at)
        except ValueError:
            return True
        if stamped.tzinfo is None:
            stamped = stamped.replace(tzinfo=timezone.utc)
        return datetime.now(timezone.utc) - stamped > timedelta(days=max_age_days)

    def get_routes_bulk(self, pairs, max_age_days=None):
        """Look up many routes in one connection.

        get_route opens a connection per call, which is fine for a handful and
        ruinous for a full-year run: tens of thousands of connections, each
        serialised behind the class lock, on a database that often lives on a
        slow Windows mount. This does the whole set in one pass and is the
        reason a fully-cached run is bounded by pandas rather than by SQLite.

        Returns {(source, destination): row} for hits only; misses are absent.
        """
        pairs = list(dict.fromkeys(pairs))
        if not pairs:
            return {}

        found = {}
        with self._lock:
            with self._connect() as conn:
                # Row-value IN needs SQLite 3.15+; chunked to stay well under
                # the 999-variable default limit.
                for start in range(0, len(pairs), 400):
                    chunk = pairs[start:start + 400]
                    values = ",".join("(?,?)" for _ in chunk)
                    flat = [value for pair in chunk for value in pair]
                    rows = conn.execute(
                        f"SELECT source, destination, distance, route_sequence, "
                        f"junctions, status, fetched_at FROM rbs_routes "
                        f"WHERE (source, destination) IN (VALUES {values})", flat
                    ).fetchall()
                    for row in rows:
                        found[(row[0], row[1])] = {
                            "distance": row[2],
                            "route_sequence": json.loads(row[3]) if row[3] else [],
                            "junctions": json.loads(row[4]) if row[4] else [],
                            "status": row[5],
                            "fetched_at": row[6],
                        }

        if max_age_days is not None:
            found = {key: row for key, row in found.items()
                     if not self._is_stale(row["fetched_at"], max_age_days)}
        return found

    def stats(self):
        """Counts the UI needs to describe cache freshness honestly."""
        with self._lock:
            with self._connect() as conn:
                total, legacy, oldest, newest = conn.execute('''
                    SELECT COUNT(*),
                           SUM(CASE WHEN fetched_at IS NULL THEN 1 ELSE 0 END),
                           MIN(fetched_at), MAX(fetched_at)
                    FROM rbs_routes
                ''').fetchone()
                by_status = dict(conn.execute(
                    "SELECT status, COUNT(*) FROM rbs_routes GROUP BY status"
                ).fetchall())
        return {
            "total": total or 0,
            "legacy": legacy or 0,
            "dated": (total or 0) - (legacy or 0),
            "oldest": oldest,
            "newest": newest,
            "by_status": by_status,
        }

    def stale_pairs(self, max_age_days=None):
        """Every (source, destination) that a refresh would need to refetch."""
        with self._lock:
            with self._connect() as conn:
                if max_age_days is None:
                    rows = conn.execute(
                        "SELECT source, destination FROM rbs_routes WHERE fetched_at IS NULL"
                    ).fetchall()
                else:
                    # Every timestamp is written by utc_now(), so all rows share
                    # one format and a string comparison orders them correctly.
                    # The bound is inclusive so that max_age_days=0 means
                    # "refetch everything", matching get_route's behaviour on a
                    # row written within the current second.
                    cutoff = (datetime.now(timezone.utc)
                              - timedelta(days=max_age_days)).isoformat(timespec="seconds")
                    rows = conn.execute(
                        "SELECT source, destination FROM rbs_routes "
                        "WHERE fetched_at IS NULL OR fetched_at <= ?", (cutoff,)
                    ).fetchall()
        return [(r[0], r[1]) for r in rows]

    # -- writes -----------------------------------------------------------

    def set_route(self, source, destination, distance, route_sequence, junctions,
                  status="SUCCESS", gauge="S", basis="goods", http_status=None):
        self.set_routes_batch([{
            "source": source, "destination": destination, "distance": distance,
            "route_sequence": route_sequence, "junctions": junctions,
            "status": status, "gauge": gauge, "basis": basis,
            "http_status": http_status,
        }])

    def set_routes_batch(self, records):
        """One transaction for many routes.

        A per-route commit costs a full fsync each; batching is what makes a
        38k-pair refresh finish in minutes of disk time rather than hours.
        """
        if not records:
            return
        stamp = utc_now()
        rows = [(
            r["source"], r["destination"], r.get("distance"),
            json.dumps(r.get("route_sequence") or []),
            json.dumps(r.get("junctions") or []),
            r.get("status", "SUCCESS"), stamp,
            r.get("gauge", "S"), r.get("basis", "goods"), r.get("http_status"),
        ) for r in records]
        with self._lock:
            with self._connect() as conn:
                conn.executemany('''
                    INSERT OR REPLACE INTO rbs_routes
                    (source, destination, distance, route_sequence, junctions,
                     status, fetched_at, gauge, basis, http_status)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ''', rows)
                conn.commit()

    def status_counts(self):
        """{'SUCCESS': n, 'FAILED': n, 'ERROR': n} -- lets the UI report what the
        cache actually holds instead of guessing."""
        with self._lock:
            with self._connect() as conn:
                return dict(conn.execute(
                    "SELECT status, COUNT(*) FROM rbs_routes GROUP BY status"
                ).fetchall())

    def purge_status(self, status="ERROR"):
        """Drop rows written by a failed run so the next run retries them cleanly.

        ERROR rows are re-attempted anyway (a lookup only short-circuits on
        SUCCESS), but leaving them in makes status_counts() misleading.
        """
        with self._lock:
            with self._connect() as conn:
                cursor = conn.execute("DELETE FROM rbs_routes WHERE status = ?", (status,))
                conn.commit()
                return cursor.rowcount

    def invalidate(self, pairs):
        """Force specific (source, destination) pairs to be refetched live.

        Use this to prove the scraper is really reaching RBS: invalidate a few
        pairs, re-run, and check that stats['fetched'] > 0.
        """
        with self._lock:
            with self._connect() as conn:
                cursor = conn.executemany(
                    "DELETE FROM rbs_routes WHERE source = ? AND destination = ?",
                    [(normalise_station_code(s), normalise_station_code(d)) for s, d in pairs])
                conn.commit()
                return cursor.rowcount
