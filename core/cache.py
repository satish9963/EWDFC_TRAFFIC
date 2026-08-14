import sqlite3
import json
import threading
from config import CACHE_DB

class RBSCache:
    _lock = threading.Lock()
    
    def __init__(self, db_path=CACHE_DB):
        self.db_path = db_path
        self._init_db()

    def _init_db(self):
        with self._lock:
            with sqlite3.connect(self.db_path, timeout=15) as conn:
                cursor = conn.cursor()
                cursor.execute('PRAGMA journal_mode=WAL')
                cursor.execute('''
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
                conn.commit()

    def get_route(self, source, destination):
        with self._lock:
            with sqlite3.connect(self.db_path, timeout=15) as conn:
                cursor = conn.cursor()
                cursor.execute('''
                    SELECT distance, route_sequence, junctions, status 
                    FROM rbs_routes 
                    WHERE source = ? AND destination = ?
                ''', (source, destination))
                row = cursor.fetchone()
                
                if row:
                    return {
                        "distance": row[0],
                        "route_sequence": json.loads(row[1]) if row[1] else [],
                        "junctions": json.loads(row[2]) if row[2] else [],
                        "status": row[3]
                    }
                return None

    def set_route(self, source, destination, distance, route_sequence, junctions, status="SUCCESS"):
        with self._lock:
            with sqlite3.connect(self.db_path, timeout=15) as conn:
                cursor = conn.cursor()
                cursor.execute('''
                    INSERT OR REPLACE INTO rbs_routes 
                    (source, destination, distance, route_sequence, junctions, status)
                    VALUES (?, ?, ?, ?, ?, ?)
                ''', (
                    source, 
                    destination, 
                    distance, 
                    json.dumps(route_sequence), 
                    json.dumps(junctions), 
                    status
                ))
                conn.commit()

    def status_counts(self):
        """{'SUCCESS': n, 'FAILED': n, 'ERROR': n} - lets the UI report what the
        cache actually holds instead of guessing."""
        with self._lock:
            with sqlite3.connect(self.db_path, timeout=15) as conn:
                rows = conn.execute(
                    'SELECT status, COUNT(*) FROM rbs_routes GROUP BY status'
                ).fetchall()
                return {r[0]: r[1] for r in rows}

    def purge_status(self, status="ERROR"):
        """Drop rows written by a failed run so the next run retries them cleanly.

        ERROR rows are re-attempted anyway (get_route only short-circuits on
        SUCCESS), but leaving them in makes status_counts() misleading.
        """
        with self._lock:
            with sqlite3.connect(self.db_path, timeout=15) as conn:
                cur = conn.execute(
                    'DELETE FROM rbs_routes WHERE status = ?', (status,))
                conn.commit()
                return cur.rowcount

    def invalidate(self, pairs):
        """Force specific (source, destination) pairs to be refetched live.

        Use this to prove the scraper is really hitting RBS: invalidate a few
        pairs, re-run, and check stats['fetched'] > 0.
        """
        with self._lock:
            with sqlite3.connect(self.db_path, timeout=15) as conn:
                conn.executemany(
                    'DELETE FROM rbs_routes WHERE source = ? AND destination = ?',
                    [(str(s).strip().upper(), str(d).strip().upper()) for s, d in pairs])
                conn.commit()
