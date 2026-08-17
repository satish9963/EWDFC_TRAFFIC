"""Compare two route caches against one corridor.

Answers the question a refresh always raises: did the new route data actually
change the finding, or only the timestamps?

    python tools/cache_impact.py --old before.db --new after.db \
        --stations "Inputs/DFC Junction station chainages R1.xlsx"

Route sequences are read straight out of both databases rather than through the
scraper. That is deliberate: going through the scraper would refetch the old
cache's undated rows, because undated means stale under any max-age policy --
making both arms fresh and measuring nothing.
"""
import argparse
import json
import os
import sqlite3
import sys

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agents.agent_1_validator import InputValidator  # noqa: E402
from core import schema  # noqa: E402


def score(db_path, codes, chainage):
    """Corridor interactions and corridor-km for every route in a cache."""
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    rows = conn.execute(
        "SELECT source, destination, route_sequence FROM rbs_routes WHERE status='SUCCESS'"
    ).fetchall()
    conn.close()

    counts, corridor_km, hops = {}, {}, 0
    for source, destination, blob in rows:
        sequence = json.loads(blob) if blob else []
        hops += len(sequence)
        touched = [s for s in sequence if s in codes]
        counts[(source, destination)] = len(touched)
        chain = [chainage[s] for s in touched if chainage.get(s) == chainage.get(s)]
        corridor_km[(source, destination)] = (max(chain) - min(chain)) if len(chain) >= 2 else 0.0
    return counts, corridor_km, len(rows), hops


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--old", required=True, help="cache before the refresh")
    ap.add_argument("--new", required=True, help="cache after the refresh")
    ap.add_argument("--stations", required=True, help="corridor station workbook")
    ap.add_argument("--thresholds", default="1,2,3,4,5")
    args = ap.parse_args()

    corridor, _ = InputValidator().validate_corridor_stations(pd.read_excel(args.stations))
    codes = set(corridor[schema.CORRIDOR_CODE])
    chainage = dict(zip(corridor[schema.CORRIDOR_CODE], corridor.get(schema.CHAINAGE, [])))
    print(f"corridor stations: {len(codes)}", flush=True)

    old_counts, old_km, old_rows, old_hops = score(args.old, codes, chainage)
    new_counts, new_km, new_rows, new_hops = score(args.new, codes, chainage)
    print(f"old: {old_rows:,} routes, {old_hops:,} station hops")
    print(f"new: {new_rows:,} routes, {new_hops:,} station hops")

    shared = sorted(set(old_counts) & set(new_counts))
    if not shared:
        raise SystemExit("The two caches share no OD pairs; nothing to compare.")
    print(f"\ncomparable OD pairs: {len(shared):,}\n")
    print(f"{'metric':<34}{'old':>14}{'new':>14}{'change':>12}")

    def line(label, o, n, fmt="{:,.0f}"):
        pct = f"{100 * (n - o) / o:+.1f}%" if o else "n/a"
        print(f"{label:<34}{fmt.format(o):>14}{fmt.format(n):>14}{pct:>12}")

    line("total corridor interactions",
         sum(old_counts[k] for k in shared), sum(new_counts[k] for k in shared))
    line("mean interactions per route",
         sum(old_counts[k] for k in shared) / len(shared),
         sum(new_counts[k] for k in shared) / len(shared), "{:.3f}")
    for threshold in [int(t) for t in args.thresholds.split(",")]:
        line(f"routes eligible at T{threshold}",
             sum(1 for k in shared if old_counts[k] >= threshold),
             sum(1 for k in shared if new_counts[k] >= threshold))
    line("total corridor-km used",
         sum(old_km[k] for k in shared), sum(new_km[k] for k in shared))

    gained = sum(1 for k in shared if new_counts[k] > old_counts[k])
    lost = sum(1 for k in shared if new_counts[k] < old_counts[k])
    print(f"\nOD pairs gaining corridor interactions: {gained:,}")
    print(f"OD pairs losing  corridor interactions: {lost:,}")

    # Aggregates can hide large per-route moves, and route-level findings are
    # what end up quoted in a report.
    for threshold in [int(t) for t in args.thresholds.split(",")]:
        newly = [k for k in shared
                 if old_counts[k] < threshold <= new_counts[k]]
        if newly:
            print(f"\nnewly eligible at T{threshold}: {len(newly):,}")
            for key in newly[:5]:
                print(f"    {key[0]}->{key[1]}: "
                      f"{old_counts[key]} -> {new_counts[key]} stations")


if __name__ == "__main__":
    main()
