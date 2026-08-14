import pandas as pd
from agents.agent_1_validator import InputValidator
from agents.agent_2_rbs_scraper import RBSScraper, norm_code
from agents.agent_3_mapper import CorridorMapper
from agents.agent_4_diversion import DiversionEngine
from agents.agent_5_scenario import ScenarioFilter
from agents.agent_6_aggregator import TrafficAggregator
from agents.agent_7_audit import QAAudit

class WorkflowOrchestrator:
    def __init__(self, od_df, dfc_df, threshold):
        self.od_df = od_df
        self.dfc_df = dfc_df
        self.threshold = threshold

    def run(self, progress_callback=None):
        """
        Runs the full pipeline.
        progress_callback is a function to report progress to the UI.
        """
        if progress_callback: progress_callback(10, "Validating Input Data (Agent 1)...")
        validator = InputValidator()
        clean_od = validator.validate_od_data(self.od_df)
        clean_dfc = validator.validate_dfc_stations(self.dfc_df)
        
        if progress_callback: progress_callback(20, "Initializing Agents...")
        scraper = RBSScraper()
        mapper = CorridorMapper(clean_dfc)
        engine = DiversionEngine(self.threshold)
        audit = QAAudit()
        
        results = []
        total_rows = len(clean_od)
        
        if progress_callback: progress_callback(30, "Batch Fetching Routes (Agent 2)...")
        
        # Extract unique OD pairs
        od_pairs = []
        for _, row in clean_od.iterrows():
            od_pairs.append((norm_code(row['From Station Code']),
                             norm_code(row['To Station Code'])))
            
        # Agent 2: Fetch Routes in Batch
        route_cache = scraper.get_routes_batch(od_pairs)
        
        if progress_callback: progress_callback(50, "Mapping Corridors and Decision Engine (Agents 3, 4)...")
        
        for i, (idx, row) in enumerate(clean_od.iterrows()):
            # Same normalisation the scraper used to build its keys - otherwise
            # the lookup below misses and IR Distance renders blank.
            src = norm_code(row['From Station Code'])
            dest = norm_code(row['To Station Code'])
            
            # Retrieve from our fetched batch
            route_data = route_cache.get((src, dest))
            if route_data:
                distance, route_sequence, junctions = route_data
            else:
                distance, route_sequence, junctions = None, [], []
            
            # Agent 3: Map Corridor
            mapping = mapper.map_route(route_sequence)
            
            # Agent 4: Diversion Decision
            decision = engine.decide(mapping['interaction_count'])
            
            # Combine results
            res_row = row.copy()
            res_row['IR Distance'] = distance
            res_row['Route Source'] = scraper.pair_status.get((src, dest), 'UNKNOWN')
            res_row['Route'] = " -> ".join(route_sequence) if route_sequence else ""
            res_row['EWDFC overlap'] = "YES" if mapping['overlap'] else "NO"
            res_row['Entry IR'] = mapping['entry_ir']
            res_row['Exit IR'] = mapping['exit_ir']
            res_row['Entry DFC'] = mapping['entry_dfc']
            res_row['Exit DFC'] = mapping['exit_dfc']
            res_row['DFC stations touched'] = ", ".join(mapping['dfc_stations_touched'])
            res_row['Interaction Count'] = mapping['interaction_count']
            res_row['Threshold Applied'] = decision['Threshold Applied']
            res_row['Eligible'] = decision['Eligible']
            
            results.append(res_row)
            
            # Update progress every 10 rows
            if progress_callback and i % 10 == 0:
                progress_callback(50 + int(25 * (i / total_rows)), f"Processed {i}/{total_rows} routes locally...")

        master_df = pd.DataFrame(results)
        
        if progress_callback: progress_callback(75, "Filtering Scenarios (Agent 5)...")
        scenario_filter = ScenarioFilter()
        scenarios = scenario_filter.filter_and_split(master_df)
        
        if progress_callback: progress_callback(85, "Aggregating Traffic (Agent 6)...")
        aggregator = TrafficAggregator(clean_dfc)
        station_traffic = aggregator.aggregate(master_df)
        route_combos = scenario_filter.generate_route_combination_summary(master_df)
        
        if progress_callback: progress_callback(95, "Running QA/Audit (Agent 7)...")
        exception_log = audit.run_audit(clean_od, master_df, clean_dfc)

        # Surface where the routes actually came from. Without this the run
        # looks identical whether RBS answered, was unreachable, or was never
        # contacted because every pair was already cached.
        route_stats = dict(scraper.stats)
        route_stats['last_error'] = scraper.last_error
        route_stats['unique_pairs'] = len(set(od_pairs))

        if progress_callback: progress_callback(100, "Processing Complete!")

        return {
            "master_od": master_df,
            "threshold_2_eligible": scenarios["threshold_2_eligible"],
            "threshold_3_eligible": scenarios["threshold_3_eligible"],
            "non_eligible": scenarios["threshold_2_non_eligible"] if self.threshold == 2 else scenarios["threshold_3_non_eligible"],
            "station_summary": station_traffic,
            "route_combos": route_combos,
            "exception_log": exception_log,
            "route_stats": route_stats
        }
