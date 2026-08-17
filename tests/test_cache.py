"""Route cache: the time dimension added in schema v2."""
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from core.cache import RBSCache


@pytest.fixture
def v1_db(tmp_path):
    """A cache in the original schema, with no fetched_at column at all."""
    path = tmp_path / "cache.db"
    conn = sqlite3.connect(path)
    conn.execute('''CREATE TABLE rbs_routes (
        source TEXT, destination TEXT, distance REAL, route_sequence TEXT,
        junctions TEXT, status TEXT, PRIMARY KEY (source, destination))''')
    conn.execute("INSERT INTO rbs_routes VALUES ('A','B',100.0,'[\"A\",\"B\"]','[]','SUCCESS')")
    conn.commit()
    conn.close()
    return path


def test_v1_cache_is_migrated_in_place(v1_db):
    cache = RBSCache(v1_db)
    row = cache.get_route("A", "B")

    assert row["distance"] == 100.0
    assert row["route_sequence"] == ["A", "B"]
    assert row["fetched_at"] is None      # legacy: age unknown


def test_legacy_rows_are_stale_under_any_max_age(v1_db):
    """A row with no fetch date cannot be shown to be fresh, so it is not."""
    cache = RBSCache(v1_db)
    assert cache.get_route("A", "B") is not None            # served with no policy
    assert cache.get_route("A", "B", max_age_days=3650) is None


def test_freshly_written_rows_are_dated_and_served(v1_db):
    cache = RBSCache(v1_db)
    cache.set_route("C", "D", 50.0, ["C", "D"], [])

    row = cache.get_route("C", "D", max_age_days=30)
    assert row is not None and row["distance"] == 50.0
    stamped = datetime.fromisoformat(row["fetched_at"])
    assert datetime.now(timezone.utc) - stamped < timedelta(minutes=5)


def test_stats_separate_dated_from_legacy(v1_db):
    cache = RBSCache(v1_db)
    cache.set_route("C", "D", 50.0, ["C", "D"], [])
    stats = cache.stats()

    assert stats["total"] == 2
    assert stats["legacy"] == 1
    assert stats["dated"] == 1


def test_stale_pairs_lists_what_a_refresh_would_refetch(v1_db):
    cache = RBSCache(v1_db)
    cache.set_route("C", "D", 50.0, ["C", "D"], [])

    assert cache.stale_pairs() == [("A", "B")]              # legacy only
    assert set(cache.stale_pairs(max_age_days=0)) == {("A", "B"), ("C", "D")}


def test_batch_write_round_trips(v1_db):
    cache = RBSCache(v1_db)
    cache.set_routes_batch([
        {"source": "E", "destination": "F", "distance": 10.0,
         "route_sequence": ["E", "F"], "junctions": [], "status": "SUCCESS"},
        {"source": "G", "destination": "H", "distance": 20.0,
         "route_sequence": ["G", "H"], "junctions": [], "status": "SUCCESS"},
    ])
    assert cache.get_route("E", "F")["distance"] == 10.0
    assert cache.get_route("G", "H")["distance"] == 20.0


def test_bulk_read_matches_single_reads(v1_db):
    cache = RBSCache(v1_db)
    cache.set_route("C", "D", 50.0, ["C", "D"], [])

    bulk = cache.get_routes_bulk([("A", "B"), ("C", "D"), ("X", "Y")])
    assert set(bulk) == {("A", "B"), ("C", "D")}          # misses simply absent
    assert bulk[("A", "B")]["distance"] == 100.0
    assert bulk[("C", "D")]["route_sequence"] == ["C", "D"]


def test_bulk_read_applies_max_age(v1_db):
    cache = RBSCache(v1_db)
    cache.set_route("C", "D", 50.0, ["C", "D"], [])

    fresh = cache.get_routes_bulk([("A", "B"), ("C", "D")], max_age_days=30)
    assert set(fresh) == {("C", "D")}                     # the legacy row is stale


def test_bulk_read_handles_more_pairs_than_the_variable_limit(v1_db):
    """Chunking: SQLite caps bound variables, two per pair."""
    cache = RBSCache(v1_db)
    pairs = [(f"S{i}", f"T{i}") for i in range(900)]
    cache.set_routes_batch([
        {"source": s, "destination": t, "distance": float(i),
         "route_sequence": [s, t], "junctions": []}
        for i, (s, t) in enumerate(pairs)
    ])
    found = cache.get_routes_bulk(pairs)
    assert len(found) == 900
    assert found[("S899", "T899")]["distance"] == 899.0


def test_migration_is_idempotent(v1_db):
    RBSCache(v1_db)
    RBSCache(v1_db)                                          # must not raise
    assert RBSCache(v1_db).stats()["total"] == 1
