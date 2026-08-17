"""Re-fetch cached RBS routes so every row carries a known fetch date.

Run this against a cache copied onto local disk, not a Windows-mounted path --
SQLite locking over DrvFs is both slow and unreliable, and this writes tens of
thousands of rows.

    python tools/refresh_cache.py --db /path/cache.db --workers 12

The run is resumable: rows already stamped during this refresh are skipped, so
an interrupted run can simply be restarted. Every refetched value is compared
against what was cached before, and disagreements are written to a drift report
-- the point of the exercise is to learn whether the old data was still right,
not just to change its timestamp.
"""
import argparse
import csv
import json
import os
import random
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
from bs4 import BeautifulSoup

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

RBS_URL = "https://rbs.indianrail.gov.in/ShortPath/ShortPathServlet"

# The portal presents a valid chain as of 2026-08-17, so certificates are
# verified. Only set this False via --insecure, and only if the portal's chain
# regresses; an unverified fetch can be silently rewritten in transit, and this
# data ends up in client deliverables.
VERIFY_TLS = True

_local = threading.local()
_counter = threading.Lock()
_progress = {"done": 0, "ok": 0, "failed": 0, "changed": 0, "consecutive_errors": 0}


def _session():
    """One requests.Session per worker thread; they are not thread-safe."""
    if not hasattr(_local, "session"):
        s = requests.Session()
        s.verify = VERIFY_TLS
        s.headers.update({"User-Agent": "Mozilla/5.0 (compatible; corridor-assessment/1.0)"})
        _local.session = s
    return _local.session


def parse_route(html):
    """Pull the station sequence and total distance out of the RBS response.

    Kept identical to the scraper in agents/ so a refreshed row is exactly what
    a live lookup would have produced.
    """
    soup = BeautifulSoup(html, "html.parser")
    sequence, distance = [], 0.0
    for row in soup.find_all("tr"):
        cols = row.find_all("td")
        if len(cols) > 3:
            code = cols[1].text.strip().split("\n")[0].strip()
            if code and len(code) <= 8 and " " not in code:
                sequence.append(code)
                text = cols[3].text.strip()
                if text.replace(".", "", 1).isdigit():
                    distance = float(text)
    return sequence, distance


def fetch(source, destination, retries=3):
    payload = {
        "srcCode": source, "destCode": destination,
        "gaugeType": "S", "distance": "goods", "PageName": "ShortPath",
    }
    last_status = None
    for attempt in range(retries):
        try:
            r = _session().post(RBS_URL, data=payload, timeout=45)
            last_status = r.status_code
            if r.status_code != 200:
                raise requests.HTTPError(f"HTTP {r.status_code}")
            sequence, distance = parse_route(r.text)
            if not sequence:
                # A real "no path" answer, not a transport failure.
                return {"status": "FAILED", "distance": 0.0, "route_sequence": [],
                        "junctions": [], "http_status": r.status_code}
            return {
                "status": "SUCCESS", "distance": distance, "route_sequence": sequence,
                "junctions": [c for c in sequence if "JN" in c],
                "http_status": r.status_code,
            }
        except Exception:
            if attempt == retries - 1:
                return {"status": "ERROR", "distance": None, "route_sequence": [],
                        "junctions": [], "http_status": last_status}
            # Jittered backoff, so a portal hiccup doesn't turn into a stampede.
            time.sleep((2 ** attempt) + random.random())
    return {"status": "ERROR", "distance": None, "route_sequence": [],
            "junctions": [], "http_status": last_status}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True, help="cache database to refresh in place")
    ap.add_argument("--workers", type=int, default=12)
    ap.add_argument("--limit", type=int, default=None, help="stop after N pairs (for a trial run)")
    ap.add_argument("--batch", type=int, default=200, help="rows per commit")
    ap.add_argument("--drift-report", default=None)
    ap.add_argument("--progress-every", type=int, default=250)
    ap.add_argument("--insecure", action="store_true",
                    help="skip TLS verification (only if the portal's chain regresses)")
    args = ap.parse_args()

    if args.insecure:
        global VERIFY_TLS
        VERIFY_TLS = False
        import urllib3
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        print("[warn] TLS verification disabled", flush=True)

    os.environ["EWDFC_CACHE_DB"] = args.db
    from core.cache import RBSCache  # imported late so the env var is picked up

    cache = RBSCache(args.db)
    before = cache.stats()
    print(f"[start] {before['total']} rows | legacy {before['legacy']} | dated {before['dated']}",
          flush=True)

    pairs = cache.stale_pairs()
    if args.limit:
        pairs = pairs[:args.limit]
    if not pairs:
        print("[done] nothing stale to refresh", flush=True)
        return
    print(f"[plan] refreshing {len(pairs)} pairs with {args.workers} workers", flush=True)

    # Snapshot the old values so drift can be measured after each refetch.
    import sqlite3
    conn = sqlite3.connect(args.db)
    previous = {
        (s, d): (dist, seq)
        for s, d, dist, seq in conn.execute(
            "SELECT source, destination, distance, route_sequence FROM rbs_routes")
    }
    conn.close()

    drift_rows, pending = [], []
    started = time.time()

    def work(pair):
        src, dest = pair
        return pair, fetch(src, dest)

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = [pool.submit(work, p) for p in pairs]
        for future in as_completed(futures):
            (src, dest), result = future.result()

            old_distance, old_sequence_json = previous.get((src, dest), (None, "[]"))
            old_sequence = json.loads(old_sequence_json) if old_sequence_json else []

            if result["status"] == "ERROR":
                # Leave the row's data alone; only note the failed attempt.
                with _counter:
                    _progress["failed"] += 1
                    _progress["consecutive_errors"] += 1
                    errors = _progress["consecutive_errors"]
                if errors and errors % 25 == 0:
                    print(f"[warn] {errors} consecutive errors -- pausing 30s", flush=True)
                    time.sleep(30)
            else:
                distance_moved = (
                    old_distance is not None and result["distance"] is not None
                    and abs(float(old_distance) - float(result["distance"])) > 0.01
                )
                route_moved = old_sequence != result["route_sequence"]
                if distance_moved or route_moved:
                    drift_rows.append({
                        "source": src, "destination": dest,
                        "old_distance": old_distance, "new_distance": result["distance"],
                        "old_hops": len(old_sequence), "new_hops": len(result["route_sequence"]),
                        "distance_changed": distance_moved, "route_changed": route_moved,
                    })
                    with _counter:
                        _progress["changed"] += 1
                pending.append({"source": src, "destination": dest, **result})
                with _counter:
                    _progress["ok"] += 1
                    _progress["consecutive_errors"] = 0

            with _counter:
                _progress["done"] += 1
                done = _progress["done"]
                flush_now = len(pending) >= args.batch

            if flush_now:
                batch, pending[:] = list(pending), []
                cache.set_routes_batch(batch)

            if done % args.progress_every == 0:
                elapsed = time.time() - started
                rate = done / elapsed if elapsed else 0
                remaining = (len(pairs) - done) / rate if rate else 0
                print(f"[{done}/{len(pairs)}] ok={_progress['ok']} "
                      f"failed={_progress['failed']} changed={_progress['changed']} "
                      f"{rate:.1f}/s eta={remaining/60:.0f}m", flush=True)

    if pending:
        cache.set_routes_batch(pending)

    after = cache.stats()
    print(f"[done] {time.time() - started:.0f}s | ok={_progress['ok']} "
          f"failed={_progress['failed']} changed={_progress['changed']}", flush=True)
    print(f"[cache] total {after['total']} | legacy {after['legacy']} | dated {after['dated']} "
          f"| newest {after['newest']}", flush=True)

    if args.drift_report and drift_rows:
        with open(args.drift_report, "w", newline="") as fh:
            writer = csv.DictWriter(fh, fieldnames=list(drift_rows[0]))
            writer.writeheader()
            writer.writerows(drift_rows)
        print(f"[drift] {len(drift_rows)} rows -> {args.drift_report}", flush=True)


if __name__ == "__main__":
    main()
