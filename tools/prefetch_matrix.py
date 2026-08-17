"""Pre-fetch the OD matrix for the busiest freight endpoints, overnight.

This is a bounded job, not a scrape of the network. The whole gazetteer is
145,359,192 ordered pairs -- 62 days of continuous requests and ~160 GB -- and
it is neither obtainable nor needed. What is worth having is the matrix over
the stations that actually carry freight in the datasets already seen.

Returns diminish quickly, so choose N deliberately:

    top 500   249,500 pairs   2.6 h   covers 38.9% of pairs already used
    top 1000  999,000 pairs  10.3 h   covers 67.6%
    top 2000  3,998,000      41 h     covers 92.3%

WHY A SEPARATE DATABASE

`cache.db` is committed to a public GitHub repo, which rejects files over
100 MB, and the deployed app seeds itself from that bundled copy. At ~541 bytes
a row, top 500 alone would take it to ~129 MB and break both. So this writes to
its own database (default `cache_full.db`, untracked), seeded from `cache.db`
so nothing is re-fetched. Point a local run at it with:

    RAIL_CACHE_DB=cache_full.db streamlit run app.py

and export only what a given dataset needs back into the shipped cache.

    python tools/prefetch_matrix.py --top 1000 --hours 10
    python tools/prefetch_matrix.py --top 1000 --hours 10   # again: resumes

The run is resumable and time-bounded, so an overnight window is simply
`--hours 10`; whatever it reaches is kept. It stops early if the portal starts
refusing, rather than hammering something that is turning us away.
"""
import argparse
import collections
import os
import shutil
import sqlite3
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools.refresh_cache import fetch  # noqa: E402

ABORT_AFTER_CONSECUTIVE_ERRORS = 15


def rank_endpoints(db_path):
    """Stations ordered by how often they appear as an OD endpoint."""
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute("SELECT source, destination FROM rbs_routes").fetchall()
    finally:
        conn.close()
    frequency = collections.Counter()
    for source, destination in rows:
        frequency[source] += 1
        frequency[destination] += 1
    return [station for station, _ in frequency.most_common()], len(rows)


def already_cached(db_path):
    conn = sqlite3.connect(db_path)
    try:
        return {(s, d) for s, d in conn.execute(
            "SELECT source, destination FROM rbs_routes")}
    finally:
        conn.close()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--top", type=int, default=500,
                    help="how many of the busiest endpoints to build a matrix over")
    ap.add_argument("--db", default="cache_full.db",
                    help="database to write to; seeded from --seed if absent")
    ap.add_argument("--seed", default="cache.db")
    ap.add_argument("--workers", type=int, default=12)
    ap.add_argument("--hours", type=float, default=None,
                    help="stop cleanly after this long (resume by re-running)")
    ap.add_argument("--batch", type=int, default=500)
    ap.add_argument("--progress-every", type=int, default=1000)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    if not os.path.exists(args.db):
        if not os.path.exists(args.seed):
            ap.error(f"neither {args.db} nor seed {args.seed} exists")
        print(f"[seed] copying {args.seed} -> {args.db}", flush=True)
        shutil.copy2(args.seed, args.db)

    ranked, seed_rows = rank_endpoints(args.db)
    if len(ranked) < args.top:
        print(f"[note] only {len(ranked)} endpoints known; using all of them",
              flush=True)
    top = ranked[:args.top]

    have = already_cached(args.db)
    wanted = [(s, d) for s in top for d in top if s != d and (s, d) not in have]

    print(f"[plan] top {len(top)} endpoints of {len(ranked)} known", flush=True)
    print(f"       {len(top) * (len(top) - 1):,} ordered pairs in that matrix", flush=True)
    print(f"       {len(have):,} already cached", flush=True)
    print(f"       {len(wanted):,} to fetch", flush=True)
    if args.hours:
        print(f"       stopping after {args.hours} h; re-run to continue", flush=True)
    if args.dry_run or not wanted:
        print("[dry-run] nothing fetched" if args.dry_run else "[done] nothing to fetch",
              flush=True)
        return

    os.environ["EWDFC_CACHE_DB"] = args.db
    from core.cache import RBSCache          # late, so the env var is picked up
    cache = RBSCache(args.db)

    started = time.time()
    deadline = started + args.hours * 3600 if args.hours else None
    counts = {"SUCCESS": 0, "FAILED": 0, "ERROR": 0}
    consecutive_errors = 0
    pending, done, stopped = [], 0, None

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(fetch, s, d): (s, d) for s, d in wanted}
        for future in as_completed(futures):
            pair = futures[future]
            result = future.result()
            counts[result["status"]] += 1
            done += 1

            if result["status"] == "ERROR":
                consecutive_errors += 1
                if consecutive_errors >= ABORT_AFTER_CONSECUTIVE_ERRORS:
                    stopped = "the portal is refusing connections"
            else:
                consecutive_errors = 0
                pending.append({"source": pair[0], "destination": pair[1], **result})

            if len(pending) >= args.batch:
                batch, pending[:] = list(pending), []
                cache.set_routes_batch(batch)

            if deadline and time.time() > deadline and not stopped:
                stopped = f"the {args.hours} h window closed"

            if stopped:
                for remaining in futures:
                    remaining.cancel()
                break

            if done % args.progress_every == 0:
                elapsed = time.time() - started
                rate = done / elapsed
                left = (len(wanted) - done) / rate if rate else 0
                print(f"[{done:,}/{len(wanted):,}] ok={counts['SUCCESS']:,} "
                      f"no_route={counts['FAILED']:,} err={counts['ERROR']} "
                      f"{rate:.1f}/s elapsed={elapsed/3600:.1f}h "
                      f"eta={left/3600:.1f}h", flush=True)

    if pending:
        cache.set_routes_batch(pending)

    elapsed = time.time() - started
    print(f"\n[done] {elapsed/3600:.2f} h | fetched={counts['SUCCESS']:,} "
          f"no_route={counts['FAILED']:,} errors={counts['ERROR']}", flush=True)
    if stopped:
        print(f"[stopped] {stopped}. {len(wanted) - done:,} pairs not attempted; "
              f"everything fetched is saved -- re-run to continue.", flush=True)
    size_mb = os.path.getsize(args.db) / 1e6
    print(f"[cache] {args.db}: {cache.stats()['total']:,} rows, {size_mb:,.0f} MB",
          flush=True)
    if size_mb > 100:
        print("[warn] over GitHub's 100 MB file limit -- keep this one local and "
              "do not add it to the repo.", flush=True)


if __name__ == "__main__":
    main()
