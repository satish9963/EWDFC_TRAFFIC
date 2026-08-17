"""Look up OD routes and station codes without touching the RBS web form.

Searching the portal by hand is slow: one pair at a time, no bulk, and you have
to know the station code before you can start. This does both halves.

FIND A STATION CODE (no network -- uses the bundled gazetteer)

    python tools/rbs_lookup.py --station bhusaval
    python tools/rbs_lookup.py --station "new delhi"

ONE OD PAIR

    python tools/rbs_lookup.py NGP BPL

MANY PAIRS

    python tools/rbs_lookup.py --pairs "NGP-BPL, BSL-JL, DURG-RJN"
    python tools/rbs_lookup.py --from-excel pairs.xlsx --out routes.xlsx

Everything is cache-first, so repeating a query costs nothing and only genuinely
new pairs hit the portal. Add --refresh to force a live fetch.
"""
import argparse
import csv
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agents.agent_2_rbs_scraper import RBSScraper, norm_code  # noqa: E402
from core.gazetteer import DEFAULT_PATH, Gazetteer  # noqa: E402


def find_stations(query, limit=25):
    """Search the bundled gazetteer by name or code, best match first.

    Returns (rows, total, top_rank). The whole file is scanned -- 12k rows
    costs about a tenth of a second, and stopping early once enough substring
    hits had accumulated used to hide the answer: a search for `GAR` filled up
    on "...nagar" long before reaching the station whose code is literally GAR.
    """
    path = Path(os.environ.get("RAIL_GAZETTEER") or DEFAULT_PATH)
    if not path.exists():
        print(f"No gazetteer at {path}. Build one with tools/build_gazetteer.py.")
        return [], 0, None

    needle = Gazetteer.normalise_name(query)
    raw = str(query).strip().upper()
    scored = []

    # Read the CSV directly: the Gazetteer indexes are keyed for exact lookup,
    # not for the partial search a human doing the typing needs.
    with open(path, newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            name_key = Gazetteer.normalise_name(row.get("name", ""))
            code = (row.get("code") or "").strip().upper()
            if code and code == raw:
                rank = 0                                    # the station asked for
            elif needle and name_key == needle:
                rank = 1                                    # named exactly
            elif needle and any(word.startswith(needle) for word in name_key.split()):
                rank = 2                                    # a word starts with it
            elif needle and needle in name_key:
                rank = 3                                    # buried in the middle
            else:
                continue
            scored.append((rank, not code, len(name_key), name_key, row))

    scored.sort(key=lambda item: item[:4])
    top_rank = scored[0][0] if scored else None
    return [item[4] for item in scored[:limit]], len(scored), top_rank


def print_stations(rows, total, top_rank, query):
    if not rows:
        print(f"No station matching {query!r}. Try fewer letters.")
        return
    print(f"\n{'code':<8}{'station':<38}{'lat':>10}{'lon':>10}  source")
    print("-" * 78)
    for row in rows:
        code = row.get("code") or "—"
        try:
            position = f"{float(row['lat']):>10.4f}{float(row['lon']):>10.4f}"
        except (TypeError, ValueError, KeyError):
            position = f"{'—':>10}{'—':>10}"
        print(f"{code:<8}{row.get('name', '')[:37]:<38}{position}  {row.get('source', '')}")

    if total > len(rows):
        print(f"\n  {total} matches, showing the closest {len(rows)}.")
    # A query shaped like a code that matched nothing by code is worth saying
    # out loud -- otherwise a page of name matches reads as "no such station".
    # An exact name hit means the search already answered; stay quiet then.
    if _looks_like_code(query) and top_rank not in (0, 1):
        print(f"\n  No station carries the code {query.strip().upper()}; "
              "the rows above matched on name only.")
    if any(not r.get("code") for r in rows):
        print("  Rows with no code come from ESRI, which carries names but not IR codes.")


def _looks_like_code(query):
    text = str(query).strip()
    return 2 <= len(text) <= 8 and text.isalnum() and " " not in text


def parse_pairs(args):
    """Collect OD pairs from whichever input was given."""
    pairs = []
    if args.pair:
        pairs.append((args.pair[0], args.pair[1]))
    if args.pairs:
        for chunk in args.pairs.replace(",", "\n").splitlines():
            chunk = chunk.strip()
            if not chunk:
                continue
            for sep in ("-", ">", ":", " to ", " "):
                if sep in chunk:
                    left, _, right = chunk.partition(sep)
                    if left.strip() and right.strip():
                        pairs.append((left.strip(), right.strip()))
                        break
            else:
                # Say so rather than dropping it: a typo in one entry of a long
                # --pairs string would otherwise just shorten the results.
                print(f"  Skipped {chunk!r}: no separator, expected FROM-TO.")
    if args.from_excel:
        import pandas as pd
        from agents.agent_1_validator import InputValidator
        frame = pd.read_excel(args.from_excel)
        clean, _ = InputValidator().validate_od_data(frame)
        from core import schema
        pairs.extend(zip(clean[schema.FROM_CODE], clean[schema.TO_CODE]))
    # An OD workbook repeats a pair once per commodity; the route is the same
    # every time, so look it up once.
    return list(dict.fromkeys((norm_code(s), norm_code(d)) for s, d in pairs))


def print_route(result, show_stations=True):
    src, dest = result["source"], result["destination"]
    print(f"\n{'=' * 74}")
    print(f"  {src} -> {dest}")
    print(f"{'=' * 74}")

    if result["error"]:
        print(f"  NOT AVAILABLE: {result['error']}")
        return

    print(f"  Distance        : {result['distance_km']:,.2f} km")
    print(f"  Stations en route: {len(result['route'])}")
    if result["junctions"]:
        print(f"  Junctions       : {', '.join(result['junctions'])}")
    age = result.get("fetched_at")
    print(f"  Source          : {result['origin']}"
          + (f" ({age[:10]})" if age and age != "just now" else ""))

    if show_stations and result.get("stations"):
        print(f"\n  {'#':>3}  {'code':<8}{'station':<34}{'cumulative km':>14}")
        print("  " + "-" * 60)
        for index, station in enumerate(result["stations"], 1):
            km = station["cumulative_km"]
            print(f"  {index:>3}  {station['code']:<8}{station['name'][:33]:<34}"
                  f"{km if km is not None else '—':>14}")
    elif result["route"]:
        # Cached rows keep codes only; names were never stored.
        print(f"\n  Route: {' -> '.join(result['route'])}")


def main():
    ap = argparse.ArgumentParser(
        description="Look up RBS routes and station codes.",
        formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__)
    ap.add_argument("pair", nargs="*", help="FROM TO (two station codes)")
    ap.add_argument("--station", help="search for a station code by name")
    ap.add_argument("--pairs", help='e.g. "NGP-BPL, BSL-JL"')
    ap.add_argument("--from-excel", help="workbook with origin/destination columns")
    ap.add_argument("--out", help="write results to .xlsx or .csv")
    ap.add_argument("--refresh", action="store_true", help="ignore the cache and refetch")
    ap.add_argument("--quiet", action="store_true", help="summary only, no station lists")
    args = ap.parse_args()

    if args.station:
        rows, total, top_rank = find_stations(args.station)
        print_stations(rows, total, top_rank, args.station)
        return

    if args.pair and len(args.pair) != 2:
        ap.error("give exactly two codes, or use --pairs / --from-excel")

    pairs = parse_pairs(args)
    if not pairs:
        ap.error("nothing to look up. Try: rbs_lookup.py NGP BPL")

    # max_age_days=0 makes every cached row look stale, forcing a refetch.
    scraper = RBSScraper(max_age_days=0 if args.refresh else None)

    results = []
    for source, destination in pairs:
        result = scraper.lookup(source, destination)
        results.append(result)
        print_route(result, show_stations=not args.quiet and len(pairs) <= 10)

    if len(pairs) > 1:
        ok = [r for r in results if r["distance_km"] is not None]
        print(f"\n{'=' * 74}")
        print(f"  {len(ok)} of {len(results)} pairs resolved.  "
              f"cache={scraper.stats['cache_hits']} fetched={scraper.stats['fetched']} "
              f"no_route={scraper.stats['no_route']} errors={scraper.stats['errors']}")
        if ok:
            total = sum(r["distance_km"] for r in ok)
            print(f"  Distance: total {total:,.1f} km, mean {total / len(ok):,.1f} km")
        if scraper.last_error:
            print(f"  Last error: {scraper.last_error}")

    if args.out:
        import pandas as pd
        frame = pd.DataFrame([{
            "From": r["source"], "To": r["destination"],
            "Distance (km)": r["distance_km"],
            "Stations": len(r["route"]) if r["route"] else 0,
            "Route": " -> ".join(r["route"]),
            "Junctions": ", ".join(r["junctions"]),
            "Source": r["origin"],
            "Error": r["error"] or "",
        } for r in results])
        if args.out.lower().endswith(".csv"):
            frame.to_csv(args.out, index=False)
        else:
            frame.to_excel(args.out, index=False)
        print(f"\n  Written to {args.out}")


if __name__ == "__main__":
    main()
