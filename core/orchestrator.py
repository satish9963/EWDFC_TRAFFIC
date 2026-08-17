"""Runs the seven agents in order and assembles the result set.

The orchestrator holds the project settings, so every agent is configured from
one place and none of them reads global state. That is what allows two projects
with different corridors, matching rules and thresholds to run in the same
process without interfering.
"""
import pandas as pd

from agents.agent_1_validator import InputValidator
from agents.agent_2_rbs_scraper import RBSScraper
from agents.agent_3_mapper import CorridorMapper
from agents.agent_4_diversion import DiversionEngine
from agents.agent_5_scenario import ScenarioFilter
from agents.agent_6_aggregator import TrafficAggregator
from agents.agent_7_audit import QAAudit
from core import gazetteer as gazetteer_module
from core import schema


class WorkflowOrchestrator:
    def __init__(self, od_df, corridor_df, project, geometry=None, gazetteer=None):
        self.od_df = od_df
        self.corridor_df = corridor_df
        self.project = project
        self.geometry = geometry
        self.gazetteer = gazetteer

    def run(self, progress_callback=None):
        def report(pct, message):
            if progress_callback:
                progress_callback(pct, message)

        report(5, "Validating input data (agent 1)...")
        validator = InputValidator()
        clean_od, od_report = validator.validate_od_data(self.od_df)
        clean_corridor, corridor_report = validator.validate_corridor_stations(self.corridor_df)

        # The client's own station coordinates outrank the bundled gazetteer.
        gazetteer = self.gazetteer or gazetteer_module.load_gazetteer()
        by_code, by_name = gazetteer_module.from_station_frame(
            clean_corridor, schema.CORRIDOR_CODE, schema.LATITUDE, schema.LONGITUDE,
            name_column=schema.CORRIDOR_NAME,
        )
        if by_code or by_name:
            gazetteer = gazetteer.overlay(by_code, by_name)

        # Orient chainage the way the client's station list measures it, so a
        # drawing that happens to start at the far end does not invert every
        # chainage in the report. Falling back to the name lets this work for a
        # station list that gives names without codes.
        if self.geometry is not None and schema.CHAINAGE in clean_corridor.columns:
            references = []
            for _, row in clean_corridor.iterrows():
                position = gazetteer.locate(
                    code=row[schema.CORRIDOR_CODE],
                    name=row.get(schema.CORRIDOR_NAME),
                )
                value = row.get(schema.CHAINAGE)
                if position and value is not None and value == value:
                    references.append((position[0], position[1], float(value)))
            if references:
                self.geometry.orient_by_reference(references)

        report(15, "Resolving routes (agent 2)...")
        scraper = RBSScraper(max_age_days=self.project.cache_max_age_days)
        # Normalised on both sides -- here and at the lookup below. If the two
        # disagree the lookup misses, IR Distance renders blank, and nothing
        # reports an error.
        od_pairs = [
            (schema.normalise_station_code(source), schema.normalise_station_code(dest))
            for source, dest in zip(clean_od[schema.FROM_CODE], clean_od[schema.TO_CODE])
        ]

        def route_progress(done, total):
            report(15 + int(35 * done / max(total, 1)), f"Routes resolved {done}/{total}...")

        routes = scraper.get_routes_batch(od_pairs, progress_callback=route_progress)

        report(55, "Mapping the corridor and deciding diversion (agents 3, 4)...")
        mapper = CorridorMapper(
            clean_corridor,
            geometry=self.geometry,
            gazetteer=gazetteer,
            buffer_km=self.project.matching.buffer_km,
            by_code=self.project.matching.by_code,
            by_proximity=self.project.matching.by_proximity,
        )
        engine = DiversionEngine(self.project.eligibility)

        rows = []
        total_rows = len(clean_od)
        for position, (_, row) in enumerate(clean_od.iterrows()):
            # Same normalisation the scraper used to build its keys.
            source = schema.normalise_station_code(row[schema.FROM_CODE])
            destination = schema.normalise_station_code(row[schema.TO_CODE])
            distance, sequence, _junctions, fetched_at = routes.get(
                (source, destination), (None, [], [], None)
            )

            mapping = mapper.map_route(sequence, ir_distance=distance)
            decision = engine.decide(mapping)

            record = row.to_dict()
            record[schema.IR_DISTANCE] = distance
            record[schema.ROUTE] = " -> ".join(sequence) if sequence else ""
            record[schema.ROUTE_FETCHED_AT] = fetched_at
            record[schema.ROUTE_SOURCE] = scraper.pair_status.get(
                (source, destination), "UNKNOWN")
            record[schema.CORRIDOR_OVERLAP] = "YES" if mapping["overlap"] else "NO"
            record[schema.ROUTE_ORIGIN] = mapping["route_origin"]
            record[schema.ROUTE_DESTINATION] = mapping["route_destination"]
            record[schema.ENTRY_STATION] = mapping["entry_station"]
            record[schema.EXIT_STATION] = mapping["exit_station"]
            record[schema.STATIONS_TOUCHED] = ", ".join(mapping["stations_touched"])
            record[schema.INTERACTION_COUNT] = mapping["interaction_count"]
            record[schema.CORRIDOR_KM] = mapping["corridor_km"]
            record[schema.CORRIDOR_SHARE] = mapping["corridor_share"]
            record[schema.MATCH_MODE] = mapping["match_mode"]
            record[schema.THRESHOLD_APPLIED] = decision["Threshold Applied"]
            record[schema.ELIGIBLE] = decision["Eligible"]
            rows.append(record)

            if position % 500 == 0:
                report(55 + int(20 * position / max(total_rows, 1)),
                       f"Mapped {position}/{total_rows} routes...")

        master_df = pd.DataFrame(rows)

        report(80, "Building threshold scenarios (agent 5)...")
        scenario_filter = ScenarioFilter(self.project.eligibility.thresholds_offered)
        scenarios = scenario_filter.filter_and_split(master_df)
        route_combos = scenario_filter.generate_route_combination_summary(master_df)

        report(88, "Aggregating station traffic (agent 6)...")
        station_summary = TrafficAggregator(clean_corridor).aggregate(master_df)

        report(95, "Running QA audit (agent 7)...")
        exception_log = QAAudit().run_audit(
            clean_od, master_df, clean_corridor, mapper=mapper,
            validation=od_report.as_dict(),
        )

        report(100, "Complete")

        # Surface where the routes actually came from. Without this a run looks
        # identical whether RBS answered, was unreachable, or was never
        # contacted because every pair was already cached.
        route_stats = dict(scraper.stats)
        route_stats["unique_pairs"] = len(set(od_pairs))
        route_stats["last_error"] = scraper.last_error
        route_stats["circuit_open"] = scraper.circuit_open

        results = {
            "master_od": master_df,
            "station_summary": station_summary,
            "route_combos": route_combos,
            "exception_log": exception_log,
            "project": self.project,
            "criterion": engine.criterion_text(),
            "route_stats": route_stats,
            "validation": {"od": od_report.as_dict(), "corridor": corridor_report.as_dict()},
            "geometry": self.geometry,
            "spatial_matches": sorted(mapper.spatial_matches),
            "gazetteer": {
                "source": gazetteer.source,
                "codes": len(gazetteer),
                "coverage": mapper.gazetteer_coverage,
                "unlocatable": len(mapper.unlocatable_stations),
            },
            "chainage_source": mapper.chainage_source,
            "thresholds": scenario_filter.thresholds,
        }
        results.update({k: v for k, v in scenarios.items() if k != "master"})
        return results
