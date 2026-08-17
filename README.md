---
title: Rail Corridor Traffic Assessment
emoji: 🚂
colorFrom: blue
colorTo: indigo
sdk: docker
app_port: 8501
pinned: false
short_description: Freight OD traffic assessment and route mapping for any rail corridor
---

# Rail Corridor Traffic Assessment

Processes Indian Railways freight OD traffic data, retrieves shortest-path routes from the
RBS portal, maps that traffic against a rail corridor, and identifies diversion potential.

The corridor is an input, not a constant. The same pipeline serves a dedicated freight
corridor, a proposed new line, or a doubling scheme — what changes is the corridor
definition and what counts as divertible, both set per project.

## Usage

Pick a project preset, upload the OD traffic and corridor station workbooks, optionally
add an alignment, and run.

### OD traffic

Only the origin and destination codes are required. Everything else is filled with neutral
defaults if absent, so a two-column OD list is a valid first-pass input.

| Column | Required |
|---|---|
| From Station Code | yes |
| To Station Code | yes |
| From Station Name | no |
| To Station Name | no |
| Commodity | no — defaults to UNKNOWN |
| Annual Tonnage | no — defaults to 0 |
| No. of Rakes / Wagon Units | no — defaults to 0 |

Alternative headers are recognised automatically: `FROMSTTN`, `TOSTTN`, `Origin Code`,
`Destination Station Code`, `MTPA`, `No.of Rakes/No. of Units` and many others. A header
row sitting below a merged title row is detected and promoted.

**Tonnage is never converted.** Figures are carried through in whatever unit the workbook
uses. A workbook in MTPA and one in tonnes are indistinguishable to a parser, and guessing
wrong changes an answer by a factor of a million.

### Corridor station list

| Column | Required | Unlocks |
|---|---|---|
| Corridor Station Code | yes | code matching |
| Station Name | no | readable output |
| Chainage | no | corridor length used, corridor-ordered tables |
| Latitude / Longitude | no | proximity matching |

`DFC Station Code` and `Station Code` are accepted as the code column.

### Corridor alignment (optional)

KML, KMZ or GeoJSON. Shapefiles need `geopandas`, which is not installed by default.

An alignment supplies chainage and enables proximity matching. CAD exports are handled:
the EWDFC drawing in this repo is 21 MB with 44,164 line segments across 298 layers, and
is reduced to a single 1,956 km centreline. You will be asked which layer holds the
alignment when auto-detection is ambiguous.

## Projects

Presets live in `projects.yaml`. Each sets the corridor's name, how a route station is
judged to be on the corridor, and what counts as divertible:

| Preset | Matching | Eligibility |
|---|---|---|
| `ewdfc`, `wdfc`, `edfc` | station code | at least N corridor stations touched |
| `new_line` | code or within 15 km of alignment | at least N km of corridor used |
| `doubling_upgrade` | code or within 3 km | at least N% of the route on corridor |
| `custom` | code or within 5 km | at least N stations touched |

Every field is overridable in the sidebar. Add a project by copying a block in
`projects.yaml`.

### Eligibility rules

| Rule | Means | Needs |
|---|---|---|
| `min_stations` | touches at least N corridor stations | nothing extra |
| `min_corridor_km` | uses at least N km of corridor | chainage |
| `min_corridor_share` | at least N% of the journey on corridor | chainage and route distance |

A rule that cannot be evaluated returns `UNKNOWN`, never `NO`, so a missing input does not
read as a negative finding.

## Pipeline

| Stage | Module | Role |
|---|---|---|
| 1 | `agents/agent_1_validator.py` | Resolve headers, clean both input sheets |
| 2 | `agents/agent_2_rbs_scraper.py` | Fetch routes from the RBS portal (cached, with expiry) |
| 3 | `agents/agent_3_mapper.py` | Match route stations to the corridor; measure corridor km |
| 4 | `agents/agent_4_diversion.py` | Apply the eligibility rule |
| 5 | `agents/agent_5_scenario.py` | Threshold scenarios and route combinations |
| 6 | `agents/agent_6_aggregator.py` | Station-wise entering / exiting / through traffic |
| 7 | `agents/agent_7_audit.py` | QA audit and exception log |

Orchestrated by `core/orchestrator.py`. Column names come from `core/schema.py` — every
module refers to a column through a constant there rather than a string literal, because a
mismatch between two literals once zeroed every through-traffic figure silently.

## Route cache

`cache.db` is a SQLite cache of RBS shortest paths, bundled so the app is usable without
hitting the portal for known pairs. It is corridor-independent: a route between two
stations does not depend on which corridor is being assessed, so every project benefits
from every other project's lookups.

Rows carry a `fetched_at` timestamp. Rows cached before that column existed have `NULL`,
meaning "age unknown", and are refetched whenever a maximum age applies. Cache age and
coverage are shown in the sidebar.

To refresh:

```bash
python tools/refresh_cache.py --db cache.db --workers 12 --drift-report drift.csv
```

Run it against a cache on local disk rather than a Windows-mounted path; SQLite locking
over DrvFs is slow and unreliable. The run is resumable and reports which routes changed.

## Looking up routes by hand

The RBS web form answers one OD pair at a time and expects you to already know the station
code. `tools/rbs_lookup.py` does both halves from the command line, cache-first, so a pair
already in `cache.db` costs nothing and only genuinely new pairs reach the portal.

```bash
python tools/rbs_lookup.py --station bhusaval     # find a code, no network needed
python tools/rbs_lookup.py NGP BPL                # one pair, with station names and chainage
python tools/rbs_lookup.py --pairs "NGP-BPL, HWH-NDLS"
python tools/rbs_lookup.py --from-excel od.xlsx --out routes.xlsx
```

`--from-excel` accepts any workbook the app accepts, header aliases included, and looks up
each distinct pair once. `--refresh` ignores cached rows and refetches.

Two limits worth knowing:

- **Not every code is in the gazetteer.** `--station` searches `data/stations.csv`, which
  carries codes for 7,021 of its 12,057 stations. `NGP` is a valid IR code but has no row,
  so the search says so rather than showing name matches as if they were the answer.
- **A single-station answer is not a route.** RBS replies to an unroutable pair — usually a
  destination code that does not exist — with a page listing the origin alone. That is
  reported as unavailable, and cached as `FAILED` rather than as a 0 km success.

## Station gazetteer

`data/stations.csv` maps station codes **and names** to coordinates, which is what makes
proximity matching possible — RBS returns codes with no positions, and corridor station
lists are frequently written with names and no codes.

```bash
python tools/build_gazetteer.py --out data/stations.csv --source both
```

Two sources, because neither is sufficient alone:

| Source | Gives | Does not give |
|---|---|---|
| ESRI Living Atlas India | ~10,200 stations with names and coordinates, in about a minute | **no IR station codes** |
| OpenStreetMap (`ref` tag) | IR station codes | patchier coverage; a national query times out, so it is fetched in tiles and takes ~20 minutes |

Names are normalised before comparison, so `Bhusaval Junction`, `Bhusaval Jn` and
`New Bhusaval` all resolve to the same station.

Two traps worth knowing if you extend this:

- The ESRI layer's own `latitude`/`longitude` **attributes are in an unlabelled unit and
  decode wrongly** — a Maharashtra station lands in West Bengal. Always request
  `returnGeometry=true&outSR=4326` and use the geometry.
- Its `Railway Network` layer's `fromjunction`/`tojunction` are *section* endpoints, not
  segment endpoints, so consecutive features repeat the same pair.

The gazetteer is a convenience, not an authority. Coordinates on the uploaded corridor
station list always take precedence, and its absence disables proximity matching rather
than failing a run. The tiled OSM crawl checkpoints after every tile, so an interrupted
run keeps what it has already found.

### Why routes are not simply scraped in bulk

RBS returns a route per **OD pair**, not per station. India has ~10,200 stations, so the
full matrix is ~105 million ordered pairs — roughly 61 days of continuous requests at the
~20/s this tool achieves, and tens of GB. The bundled cache holds the pairs the data
actually needs. Computing paths from a network graph instead would not reproduce IR's
official *goods* routing, which follows permitted-route and gauge rules.

## Configuration

| Env var | Effect |
|---|---|
| `RAIL_CACHE_DB` (or `EWDFC_CACHE_DB`) | Path to the route cache database |
| `RAIL_RUNTIME_DIR` (or `EWDFC_RUNTIME_DIR`) | Scratch directory when the app dir is read-only |
| `RAIL_GAZETTEER` | Path to the station gazetteer CSV |

## Running locally

```bash
pip install -r requirements.txt
streamlit run app.py
pytest tests/
```
