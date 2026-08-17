"""Cross-check proximity matching against station-code matching.

Code matching is turned OFF so that every match reported here had to come from
geometry: a route station whose gazetteer coordinates fall within the buffer of
the alignment read from the CAD drawing. Compared against code matching, this
shows whether the spatial route finds the same corridor and what it adds.
"""
import os
import sys

import pandas as pd

PROJ = "/mnt/d/Claude New Projects/EWDFC"
os.chdir(PROJ)
sys.path.insert(0, PROJ)

from agents.agent_1_validator import InputValidator  # noqa: E402
from core import schema  # noqa: E402
from core.gazetteer import load_gazetteer  # noqa: E402
from core.geometry import build_from_placemarks, read_kml_placemarks  # noqa: E402
from core.orchestrator import WorkflowOrchestrator  # noqa: E402
from core.projects import load_projects  # noqa: E402

N = int(sys.argv[1]) if len(sys.argv) > 1 else 600

gazetteer = load_gazetteer()
print(f"gazetteer: {len(gazetteer)} entries from {gazetteer.source}", flush=True)

placemarks = read_kml_placemarks(f"{PROJ}/EWDFCC Alignment with Proposed Stations.kmz")
geometry = build_from_placemarks(
    placemarks, alignment_layer="EWDFCC / Alignment without Ch / Alignment_")
print(f"alignment: {geometry.length_km:,.0f} km, chainage over "
      f"{geometry.primary_length_km:,.0f} km", flush=True)

od = pd.read_excel(f"{PROJ}/Inputs/OD Sheet Sample.xlsx").head(N)
stations = pd.read_excel(f"{PROJ}/Inputs/DFC Junction station chainages R1.xlsx")

corridor, _ = InputValidator().validate_corridor_stations(stations)
codes = list(corridor[schema.CORRIDOR_CODE])
print(f"corridor stations: {len(codes)}, "
      f"gazetteer coverage of them: {gazetteer.coverage(codes):.0%}", flush=True)

projects, _ = load_projects()
base = projects["ewdfc"]

runs = {
    "code only": base.with_overrides(by_code=True, by_proximity=False),
    "proximity only": base.with_overrides(by_code=False, by_proximity=True, buffer_km=5.0),
    "code + proximity": base.with_overrides(by_code=True, by_proximity=True, buffer_km=5.0),
}

print(f"\n{'mode':<20}{'overlap':>10}{'interactions':>14}{'T3 eligible':>13}{'spatial':>9}")
for label, project in runs.items():
    results = WorkflowOrchestrator(od, stations, project, geometry=geometry).run()
    master = results["master_od"]
    counts = master[schema.INTERACTION_COUNT]
    print(f"{label:<20}"
          f"{(master[schema.CORRIDOR_OVERLAP] == 'YES').sum():>10,}"
          f"{counts.sum():>14,}"
          f"{(counts >= 3).sum():>13,}"
          f"{len(results['spatial_matches']):>9}", flush=True)
    if results["spatial_matches"]:
        print(f"    matched by distance: "
              f"{', '.join(results['spatial_matches'][:15])}", flush=True)
