"""Fetch the RBS routes a new OD dataset needs but the cache does not have.

The hosted app cannot do this. RBS refuses TCP connections from datacentre IP
ranges -- Streamlit Cloud, AWS, GCP -- so a run there fails with `Errno 111
Connection refused` before any HTTP request is made, and the circuit breaker
stops it after five consecutive refusals. That is a block on RBS's side, not a
bug here, and no change to the scraper removes it.

So the fetching happens where the portal answers: a machine on an ordinary
connection. This tool takes the new OD workbook, works out which pairs are
genuinely missing, and fetches only those.

    python tools/fetch_missing.py --od "Inputs/new-od.xlsx" --dry-run
    python tools/fetch_missing.py --od "Inputs/new-od.xlsx" --report fetched.csv

Then commit `cache.db`. The hosted app serves those pairs from cache and never
calls the portal at all.

It reads whatever the app reads -- the same header aliases, the same two-row
header promotion -- plus .csv and .parquet, which matter because a large .xlsx
costs several minutes just to parse.

The run is resumable: fetched pairs land in the cache, so re-running after an
interruption re-computes what is still missing and continues. Unlike the app's
own scraper this is not rate limited; it measured ~27 pairs/s against the
portal with no failures.
"""
import argparse
import csv
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agents.agent_2_rbs_scraper import norm_code  # noqa: E402
from core import schema  # noqa: E402
from tools.refresh_cache import fetch  # noqa: E402

# Stop rather than hammer a portal that is refusing us. This is the same
# reasoning as the scraper's circuit breaker: a refused handshake will not heal
# inside one run, and the honest outcome is a clear message about how many
# pairs were never attempted.
ABORT_AFTER_CONSECUTIVE_ERRORS = 15


def read_od_pairs(path):
    """Unique (source, destination) pairs from an OD file the app would accept."""
    import pandas as pd

    extension = os.path.splitext(path)[1].lower()
    if extension == ".parquet":
        frame = pd.read_parquet(path)
    elif extension == ".csv":
        frame = pd.read_csv(path)
    else:
        print("[read] parsing the workbook -- a large .xlsx can take several "
              "minutes, openpyxl reads the whole sheet regardless of columns",
              flush=True)
        frame = pd.read_excel(path)

    from agents.agent_1_validator import InputValidator
    clean, report = InputValidator().validate_od_data(frame)
    if getattr(report, "notes", None):
        for note in report.notes:
            print(f"[read] {note}", flush=True)

    pairs = ((norm_code(s), norm_code(d))
             for s, d in zip(clean[schema.FROM_CODE], clean[schema.TO_CODE]))
    unique = list(dict.fromkeys(p for p in pairs if p[0] and p[1]))
    print(f"[read] {len(clean):,} usable rows -> {len(unique):,} unique OD pairs",
          flush=True)
    return unique


def split_by_coverage(cache, pairs, retry_failed=False):
    """Which pairs the cache already answers, and which must be fetched.

    An ERROR row is always retried: it records that the portal could not be
    reached, which says nothing about the pair. A FAILED row is a real answer
    -- RBS has no route -- and is left alone unless asked for explicitly.
    """
    cached = cache.get_routes_bulk(pairs)
    have, missing = [], []
    failed_rows = 0
    for pair in pairs:
        row = cached.get(pair)
        if row is None or row["status"] == "ERROR":
            missing.append(pair)
        elif row["status"] == "FAILED":
            failed_rows += 1
            (missing if retry_failed else have).append(pair)
        else:
            have.append(pair)
    return have, missing, failed_rows


def main():
    ap = argparse.ArgumentParser(
        description="Fetch RBS routes missing from the cache for a new OD dataset.")
    ap.add_argument("--od", required=True, help="OD workbook (.xlsx/.csv/.parquet)")
    ap.add_argument("--db", default="cache.db")
    ap.add_argument("--workers", type=int, default=12)
    ap.add_argument("--limit", type=int, default=None,
                    help="fetch at most N missing pairs (for a trial run)")
    ap.add_argument("--batch", type=int, default=200, help="rows per commit")
    ap.add_argument("--report", default=None, help="write a per-pair CSV")
    ap.add_argument("--dry-run", action="store_true",
                    help="report coverage and stop, without fetching")
    ap.add_argument("--retry-failed", action="store_true",
                    help="also refetch pairs RBS previously said had no route")
    ap.add_argument("--progress-every", type=int, default=100)
    args = ap.parse_args()

    pairs = read_od_pairs(args.od)
    if not pairs:
        print("[done] no OD pairs in that file", flush=True)
        return

    os.environ["EWDFC_CACHE_DB"] = args.db
    from core.cache import RBSCache          # late, so the env var is picked up
    cache = RBSCache(args.db)

    have, missing, failed_rows = split_by_coverage(cache, pairs, args.retry_failed)
    share = 100.0 * len(have) / len(pairs)
    print(f"\n[coverage] {len(pairs):,} unique pairs in dataset", flush=True)
    print(f"           {len(have):,} already cached ({share:.1f}%)", flush=True)
    print(f"           {len(missing):,} missing -> to fetch", flush=True)
    if failed_rows and not args.retry_failed:
        print(f"           ({failed_rows:,} of the cached ones are known "
              f"no-route answers; --retry-failed to re-ask)", flush=True)

    if not missing:
        print("\n[done] nothing to fetch -- this dataset is fully covered", flush=True)
        return
    if args.dry_run:
        print("\n[dry-run] nothing fetched", flush=True)
        return
    if args.limit:
        missing = missing[:args.limit]
        print(f"[limit] fetching the first {len(missing):,}", flush=True)

    started = time.time()
    counts = {"SUCCESS": 0, "FAILED": 0, "ERROR": 0}
    consecutive_errors = 0
    pending, results, aborted = [], [], False

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(fetch, s, d): (s, d) for s, d in missing}
        for index, future in enumerate(as_completed(futures), 1):
            pair = futures[future]
            result = future.result()
            counts[result["status"]] += 1
            results.append((pair, result))

            if result["status"] == "ERROR":
                consecutive_errors += 1
                if consecutive_errors >= ABORT_AFTER_CONSECUTIVE_ERRORS:
                    aborted = True
                    for remaining in futures:
                        remaining.cancel()
                    break
            else:
                consecutive_errors = 0
                pending.append({"source": pair[0], "destination": pair[1], **result})

            if len(pending) >= args.batch:
                batch, pending[:] = list(pending), []
                cache.set_routes_batch(batch)

            if index % args.progress_every == 0:
                rate = index / (time.time() - started)
                eta = (len(missing) - index) / rate if rate else 0
                print(f"[{index}/{len(missing)}] ok={counts['SUCCESS']} "
                      f"no_route={counts['FAILED']} errors={counts['ERROR']} "
                      f"{rate:.1f}/s eta={eta/60:.0f}m", flush=True)

    if pending:
        cache.set_routes_batch(pending)

    elapsed = time.time() - started
    print(f"\n[done] {elapsed:.0f}s | fetched={counts['SUCCESS']} "
          f"no_route={counts['FAILED']} errors={counts['ERROR']}", flush=True)

    if aborted:
        attempted = len(results)
        print(f"\n[ABORTED] {ABORT_AFTER_CONSECUTIVE_ERRORS} connection failures in "
              f"a row -- stopped rather than hammer the portal.", flush=True)
        print(f"          {len(missing) - attempted:,} pairs were never attempted.",
              flush=True)
        print("          If this is `Connection refused`, the source IP is being "
              "blocked; run `python net_check` on THIS machine.", flush=True)
        print("          Everything fetched so far is saved. Re-run to continue.",
              flush=True)

    after = cache.stats()
    print(f"[cache] total {after['total']:,} rows", flush=True)

    if args.report:
        with open(args.report, "w", newline="", encoding="utf-8") as fh:
            writer = csv.writer(fh)
            writer.writerow(["source", "destination", "status", "distance",
                             "hops", "route"])
            for (source, destination), result in results:
                writer.writerow([
                    source, destination, result["status"], result["distance"],
                    len(result["route_sequence"]),
                    " -> ".join(result["route_sequence"]),
                ])
        print(f"[report] {len(results):,} rows -> {args.report}", flush=True)


if __name__ == "__main__":
    main()
