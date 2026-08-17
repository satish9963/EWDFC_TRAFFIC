"""Build a station gazetteer: code and/or name -> coordinates.

Proximity matching needs to know where stations are. Two independent sources are
supported, because neither is sufficient alone:

  esri  ESRI Living Atlas India's railway station layer. ~10,000 curated
        stations with names and good coverage -- but no IR station codes.
  osm   OpenStreetMap, which carries IR codes in the `ref` tag. Authoritative
        for codes, patchier on coverage, and slow to query nationally.

Codes matter because the RBS portal returns routes as station codes with no
positions. Names matter because corridor station lists are frequently written
with names and no codes. Merging both gives a table that can answer either.

    python tools/build_gazetteer.py --out data/stations.csv --source both
"""
import argparse
import csv
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request

# --- ESRI Living Atlas India ---------------------------------------------
ESRI_LAYER = ("https://livingatlas.esri.in/server1/rest/services/Railway/"
              "IN_Railway_Line/MapServer/0/query")
ESRI_PAGE = 1000

# --- Overpass -------------------------------------------------------------
OVERPASS_ENDPOINTS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://overpass.osm.ch/api/interpreter",
]
OVERPASS_QUERY = """
[out:json][timeout:{timeout}];
(
  node["railway"~"^(station|halt)$"]["ref"]({bbox});
  way["railway"~"^(station|halt)$"]["ref"]({bbox});
);
out center tags;
"""
INDIA_BBOX = "6.0,67.0,37.5,98.0"


def _get(url, timeout=120, retries=3):
    for attempt in range(retries):
        try:
            request = urllib.request.Request(
                url, headers={"User-Agent": "corridor-assessment/1.0 (gazetteer)"})
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return json.loads(response.read().decode())
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError) as exc:
            if attempt == retries - 1:
                print(f"[warn] {url[:80]}... failed: {exc}", flush=True)
                return None
            time.sleep(3 * (attempt + 1))
    return None


def fetch_esri():
    """Page through the ESRI station layer.

    outSR=4326 is essential: the layer's own latitude/longitude *attributes* are
    stored in a different, unlabelled unit, and reading them directly puts
    Maharashtra stations in West Bengal. The geometry, reprojected on request,
    is the trustworthy figure.
    """
    rows = {}
    offset = 0
    while True:
        params = urllib.parse.urlencode({
            "where": "1=1",
            "outFields": "name,city,junction",
            "returnGeometry": "true",
            "outSR": "4326",
            "f": "json",
            "resultOffset": offset,
            "resultRecordCount": ESRI_PAGE,
        })
        payload = _get(f"{ESRI_LAYER}?{params}")
        if not payload:
            break
        features = payload.get("features", [])
        if not features:
            break
        for feature in features:
            geometry = feature.get("geometry") or {}
            attributes = feature.get("attributes") or {}
            name = (attributes.get("name") or "").strip()
            x, y = geometry.get("x"), geometry.get("y")
            if not name or x is None or y is None:
                continue
            if not (6 <= y <= 38 and 67 <= x <= 98):
                continue
            key = name.upper()
            rows.setdefault(key, {
                "code": "", "name": name,
                "lat": round(float(y), 6), "lon": round(float(x), 6),
                "junction": attributes.get("junction") or "",
                "source": "esri",
            })
        print(f"[esri] {offset + len(features)} records", flush=True)
        offset += len(features)
        if not payload.get("exceededTransferLimit") and len(features) < ESRI_PAGE:
            break
        time.sleep(0.5)
    return rows


def _tiles(bbox, rows, cols):
    south, west, north, east = [float(v) for v in bbox.split(",")]
    lat_step, lon_step = (north - south) / rows, (east - west) / cols
    for r in range(rows):
        for c in range(cols):
            yield (f"{south + r * lat_step:.4f},{west + c * lon_step:.4f},"
                   f"{south + (r + 1) * lat_step:.4f},{west + (c + 1) * lon_step:.4f}")


def fetch_osm(bbox=INDIA_BBOX, rows=4, cols=4, timeout=180, checkpoint=None):
    """Tiled Overpass query. One national query for this reliably times out.

    A tile over a dense region can spend many minutes failing over between
    endpoints, so progress is written after every tile. Losing a twenty-minute
    crawl because the last tile hung is not an acceptable failure mode.
    """
    found = {}
    grid = list(_tiles(bbox, rows, cols))
    for index, tile in enumerate(grid, 1):
        body = OVERPASS_QUERY.format(bbox=tile, timeout=timeout)
        payload = None
        for endpoint in OVERPASS_ENDPOINTS:
            try:
                request = urllib.request.Request(
                    endpoint,
                    data=urllib.parse.urlencode({"data": body}).encode(),
                    headers={"User-Agent": "corridor-assessment/1.0 (gazetteer)"})
                with urllib.request.urlopen(request, timeout=timeout + 60) as response:
                    payload = json.loads(response.read().decode())
                break
            except Exception:
                continue
        if not payload:
            print(f"[osm] tile {index}/{len(grid)} failed", flush=True)
            continue
        for element in payload.get("elements", []):
            tags = element.get("tags") or {}
            raw = tags.get("ref") or tags.get("railway:ref") or ""
            if element["type"] == "node":
                lat, lon = element.get("lat"), element.get("lon")
            else:
                center = element.get("center") or {}
                lat, lon = center.get("lat"), center.get("lon")
            if lat is None or lon is None:
                continue
            for part in [c.strip().upper() for c in str(raw).replace("/", ";").split(";")]:
                if not part or not part.isalnum() or len(part) > 8:
                    continue
                # An IR station code always contains letters. A purely numeric
                # ref is a platform or a local sequence number -- Lahore's
                # suburban stations are tagged ref=1, ref=2 and would otherwise
                # be indexed as if they were station codes.
                if part.isdigit():
                    continue
                found[part] = {
                    "code": part,
                    "name": tags.get("name") or tags.get("name:en") or "",
                    "lat": round(float(lat), 6), "lon": round(float(lon), 6),
                    "junction": "", "source": "osm",
                }
        print(f"[osm] tile {index}/{len(grid)}: {len(found)} coded stations so far", flush=True)
        if checkpoint:
            checkpoint(found)
        time.sleep(1)
    return found


def merge(osm_rows, esri_rows):
    """Coded OSM entries first; ESRI fills names OSM does not cover.

    An ESRI station whose name already appears against a coded OSM entry is
    dropped, so one physical station does not appear twice.
    """
    merged = dict(osm_rows)
    known_names = {r["name"].upper() for r in osm_rows.values() if r["name"]}
    for key, row in esri_rows.items():
        if key in known_names:
            continue
        # Keyed by name, since these carry no code.
        merged[f"~{key}"] = row
    return merged


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--source", choices=["esri", "osm", "both"], default="both")
    ap.add_argument("--bbox", default=INDIA_BBOX)
    ap.add_argument("--rows", type=int, default=4)
    ap.add_argument("--cols", type=int, default=4)
    args = ap.parse_args()

    esri_rows = fetch_esri() if args.source in ("esri", "both") else {}

    def write(osm_rows, final=False):
        combined = merge(osm_rows, esri_rows)
        if not combined:
            return 0, 0
        ordered = sorted(combined.values(), key=lambda r: (r["code"] or "~", r["name"]))
        # Write to a temporary file and move it into place, so an interrupted
        # write never leaves a half-written gazetteer that loads without error.
        temporary = f"{args.out}.partial"
        with open(temporary, "w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(
                fh, fieldnames=["code", "name", "lat", "lon", "junction", "source"])
            writer.writeheader()
            writer.writerows(ordered)
        os.replace(temporary, args.out)
        coded = sum(1 for r in ordered if r["code"])
        if final:
            print(f"[write] {args.out}: {len(ordered)} stations, "
                  f"{coded} with an IR code", flush=True)
        return len(ordered), coded

    osm_rows = {}
    if args.source in ("osm", "both"):
        osm_rows = fetch_osm(args.bbox, args.rows, args.cols,
                             checkpoint=lambda rows: write(rows))
    print(f"[got] esri={len(esri_rows)} osm={len(osm_rows)}", flush=True)

    total, _ = write(osm_rows, final=True)
    if not total:
        raise SystemExit("No stations found; refusing to write an empty gazetteer")


if __name__ == "__main__":
    main()
