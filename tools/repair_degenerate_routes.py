"""Re-fetch cached routes that are stored as a success but are not routes.

RBS has two ways of saying "no". One is an empty page. The other is a page
listing **the origin alone**, which parses to a single station and 0 km, and was
stored as `status='SUCCESS'` -- a row that is never retried, never reaches the
unresolved audit, and downstream is indistinguishable from a real flow that
happens to touch no corridor station.

All three fetch paths now reject that shape, so no new ones appear. This repairs
the rows already in a cache:

    python tools/repair_degenerate_routes.py --db cache.db --dry-run
    python tools/repair_degenerate_routes.py --db cache.db --report repair.csv

Each candidate is refetched and re-classified on the evidence rather than
assumed bad -- some may have been genuine portal glitches at the time, and those
come back as real routes. A pair the portal errors on is left exactly as it is;
an unreachable portal must not be recorded as a finding about the data.

The database is backed up before anything is written unless --no-backup is
given. Run it against a cache on local disk, not a Windows-mounted path.
"""
import argparse
import collections
import csv
import json
import os
import shutil
import sqlite3
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agents.agent_2_rbs_scraper import _CODE_RE as CODE_RE  # noqa: E402
from tools.refresh_cache import fetch  # noqa: E402


def find_suspect(db_path):
    """Rows claiming success that the current parser would never have written.

    Two shapes, from two different broken parsers:

    * fewer than two stations between two different places -- the portal saying
      "no" by listing the origin alone, stored as a 0 km success;
    * a token in the sequence that is not a station code at all -- `Source`,
      `G`, `R` -- from the refresh tool's since-deleted second parser, which
      accepted any cell of eight characters or fewer.

    Both are decided by re-fetching, not by editing the stored row: a sequence
    with one bad token was also parsed by the tool that dropped every station
    with sidings, so stripping the token would leave a route still missing
    stations. The whole row has to be re-read from the portal.
    """
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(
            "SELECT source, destination, distance, route_sequence, fetched_at "
            "FROM rbs_routes WHERE status = 'SUCCESS'").fetchall()
    finally:
        conn.close()

    candidates = []
    for source, destination, distance, sequence_json, fetched_at in rows:
        sequence = json.loads(sequence_json) if sequence_json else []
        too_short = len(sequence) < 2 and source != destination
        has_non_code = any(not CODE_RE.match(token) for token in sequence)
        if not (too_short or has_non_code):
            continue
        candidates.append({
            "source": source, "destination": destination,
            "old_distance": distance, "old_sequence": sequence,
            "old_fetched_at": fetched_at,
            "reason": "too_short" if too_short else "non_code_token",
        })
    return candidates


def backup(db_path):
    target = f"{db_path}.bak-{time.strftime('%Y%m%dT%H%M%S')}"
    shutil.copy2(db_path, target)
    print(f"[backup] {target}", flush=True)
    return target


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True, help="cache database to repair in place")
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--report", default=None, help="write a per-pair CSV")
    ap.add_argument("--dry-run", action="store_true",
                    help="list what would be refetched and stop")
    ap.add_argument("--no-backup", action="store_true")
    args = ap.parse_args()

    candidates = find_suspect(args.db)
    by_reason = collections.Counter(row["reason"] for row in candidates)
    print(f"[plan] {len(candidates)} suspect SUCCESS rows: "
          f"{dict(by_reason)}", flush=True)
    if not candidates:
        return
    for row in candidates[:5]:
        print(f"        {row['source']}->{row['destination']} "
              f"{row['old_sequence'][:8]} {row['old_distance']} km", flush=True)
    if args.dry_run:
        print("[dry-run] nothing written", flush=True)
        return

    if not args.no_backup:
        backup(args.db)

    os.environ["EWDFC_CACHE_DB"] = args.db
    from core.cache import RBSCache          # late, so the env var is picked up
    cache = RBSCache(args.db)

    started = time.time()
    results, counts = [], {"FAILED": 0, "SUCCESS": 0, "ERROR": 0}

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(fetch, row["source"], row["destination"]): row
                   for row in candidates}
        for index, future in enumerate(as_completed(futures), 1):
            row = futures[future]
            result = future.result()
            counts[result["status"]] += 1
            results.append((row, result))
            if index % 50 == 0:
                rate = index / (time.time() - started)
                print(f"[{index}/{len(candidates)}] corrected={counts['FAILED']} "
                      f"now_routable={counts['SUCCESS']} errors={counts['ERROR']} "
                      f"{rate:.1f}/s", flush=True)

    # A portal error says nothing about the pair, so those rows are left alone.
    writes = [{"source": row["source"], "destination": row["destination"], **result}
              for row, result in results if result["status"] != "ERROR"]
    if writes:
        cache.set_routes_batch(writes)

    hops_moved = sum(1 for row, result in results
                     if result["status"] == "SUCCESS"
                     and len(result["route_sequence"]) != len(row["old_sequence"]))
    gained = sum(len(result["route_sequence"]) - len(row["old_sequence"])
                 for row, result in results if result["status"] == "SUCCESS")

    print(f"[done] {time.time() - started:.0f}s", flush=True)
    print(f"  re-read as a real route                 : {counts['SUCCESS']}", flush=True)
    print(f"    ...of which the hop count changed     : {hops_moved} "
          f"(net {gained:+d} stations)", flush=True)
    print(f"  corrected to FAILED (not a route)       : {counts['FAILED']}", flush=True)
    print(f"  portal error, row left unchanged        : {counts['ERROR']}", flush=True)

    if args.report:
        with open(args.report, "w", newline="", encoding="utf-8") as fh:
            writer = csv.writer(fh)
            writer.writerow(["source", "destination", "reason", "old_distance",
                             "old_hops", "old_sequence", "old_fetched_at",
                             "new_status", "new_distance", "new_hops", "new_sequence"])
            for row, result in results:
                writer.writerow([
                    row["source"], row["destination"], row["reason"],
                    row["old_distance"], len(row["old_sequence"]),
                    " -> ".join(row["old_sequence"]), row["old_fetched_at"],
                    result["status"], result["distance"],
                    len(result["route_sequence"]),
                    " -> ".join(result["route_sequence"]),
                ])
        print(f"[report] {len(results)} rows -> {args.report}", flush=True)


if __name__ == "__main__":
    main()
