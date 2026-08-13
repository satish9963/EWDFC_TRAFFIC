import pandas as pd
import numpy as np

class CorridorMapper:
    def __init__(self, dfc_stations_df):
        """
        Initializes with DFC Stations DataFrame.
        Expected columns: DFC Station Code, Latitude, Longitude
        """
        self.dfc_stations = dfc_stations_df
        # Create Shapely points for distance calculation if needed
        # In a real scenario, we'd convert to GeoDataFrame and project to a metric CRS
        
    def map_route(self, route_sequence):
        """
        Maps an IR route sequence to the DFC corridor.
        Returns entry, exit, intersected stations, and interaction count.
        """
        if not route_sequence:
            return {
                "overlap": False,
                "entry_ir": None,
                "exit_ir": None,
                "entry_dfc": None,
                "exit_dfc": None,
                "dfc_stations_touched": [],
                "interaction_count": 0
            }

        # Simplistic mapping: Check if IR station is in DFC list.
        # In a full implementation, you'd calculate nearest DFC station for IR entry/exit points
        # if the station codes don't exactly match.
        
        dfc_station_codes = set(self.dfc_stations['DFC Station Code'].values)
        
        touched_dfc_stations = []
        for station in route_sequence:
            if station in dfc_station_codes:
                touched_dfc_stations.append(station)
                
        interaction_count = len(touched_dfc_stations)
        
        if interaction_count > 0:
            entry_ir = route_sequence[0]
            exit_ir = route_sequence[-1]
            entry_dfc = touched_dfc_stations[0]
            exit_dfc = touched_dfc_stations[-1]
            overlap = True
        else:
            entry_ir = route_sequence[0] if route_sequence else None
            exit_ir = route_sequence[-1] if route_sequence else None
            entry_dfc = None
            exit_dfc = None
            overlap = False
            
        return {
            "overlap": overlap,
            "entry_ir": entry_ir,
            "exit_ir": exit_ir,
            "entry_dfc": entry_dfc,
            "exit_dfc": exit_dfc,
            "dfc_stations_touched": touched_dfc_stations,
            "interaction_count": interaction_count
        }
