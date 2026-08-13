---
title: EWDFC Traffic Assessment
emoji: 🚂
colorFrom: blue
colorTo: indigo
sdk: docker
app_port: 8501
pinned: false
short_description: Freight OD traffic assessment and route mapping for the EWDFC
---

# EWDFC Traffic Assessment & Route Mapping

Processes Indian Railways freight OD traffic data, retrieves shortest-path routes from the
RBS portal, maps that traffic against the EWDFC alignment, and identifies diversion potential.

## Usage

Upload two Excel files in the sidebar, pick a threshold, and run the pipeline.

**OD Traffic** must contain:

| Column |
|---|
| From Station Code |
| From Station Name |
| To Station Code |
| To Station Name |
| Commodity |
| Annual Tonnage |
| No. of Rakes / Wagon Units |

**DFC Station List** must contain:

| Column |
|---|
| DFC Station Code |
| Station Name |
| Chainage |
| Latitude |
| Longitude |

Results appear as five tabs (Master OD, Station Summary, Route Combos, Eligible Traffic,
Exceptions) and export as a single multi-sheet Excel workbook from the sidebar.

## Pipeline

| Stage | Module | Role |
|---|---|---|
| 1 | `agents/agent_1_validator.py` | Validates and cleans both input sheets |
| 2 | `agents/agent_2_rbs_scraper.py` | Fetches routes from the RBS portal (cached) |
| 3 | `agents/agent_3_mapper.py` | Maps IR routes onto the DFC corridor |
| 4 | `agents/agent_4_diversion.py` | Applies diversion logic |
| 5 | `agents/agent_5_scenario.py` | Filters by interaction-count threshold |
| 6 | `agents/agent_6_aggregator.py` | Aggregates station and route summaries |
| 7 | `agents/agent_7_audit.py` | QA audit and exception log |

Orchestrated by `core/orchestrator.py`.

## Route cache

`cache.db` is a SQLite cache of previously fetched RBS routes, bundled so the app is usable
without hitting the portal for known OD pairs. Newly fetched routes are written to it during
a session.

Note that on a Space without persistent storage, writes to the cache are lost when the
container restarts and it resets to the bundled snapshot. The bundled routes always remain
available. To keep new lookups, either enable persistent storage and set `EWDFC_CACHE_DB` to
a path under it, or download the updated `cache.db` and commit it back.

## Configuration

| Env var | Effect |
|---|---|
| `EWDFC_CACHE_DB` | Path to the route cache database |
| `EWDFC_RUNTIME_DIR` | Scratch directory used when the app directory is read-only |

## Running locally

```bash
pip install -r requirements.txt
streamlit run app.py
```
