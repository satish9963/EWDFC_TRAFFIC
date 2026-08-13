import pandas as pd

class TrafficAggregator:
    def __init__(self, dfc_stations_df):
        self.dfc_stations = dfc_stations_df['DFC Station Code'].unique()

    def aggregate(self, df):
        """
        Aggregates station-wise traffic.
        Returns a dataframe with each DFC station and its entering, exiting, through, and total traffic.
        """
        if df.empty:
             return pd.DataFrame()
             
        stats = {station: {
            'entering_tonnage': 0, 'entering_rakes': 0, 'entering_od_count': 0,
            'exiting_tonnage': 0, 'exiting_rakes': 0, 'exiting_od_count': 0,
            'through_tonnage': 0, 'through_rakes': 0, 'through_od_count': 0,
        } for station in self.dfc_stations}

        for _, row in df.iterrows():
            tonnage = row.get('Annual Tonnage', 0)
            rakes = row.get('No. of Rakes / Wagon Units', 0)
            
            entry = row.get('Entry DFC')
            exit = row.get('Exit DFC')
            # Must match the column the orchestrator writes, or through traffic
            # silently falls back to empty and every through_* total stays zero.
            touched = row.get('DFC stations touched', [])
            
            if isinstance(touched, str):
                if touched.strip() == "":
                    touched = []
                else:
                    touched = [s.strip() for s in touched.split(',')]
            elif not isinstance(touched, list):
                continue

            if entry in stats:
                stats[entry]['entering_tonnage'] += tonnage
                stats[entry]['entering_rakes'] += rakes
                stats[entry]['entering_od_count'] += 1
                
            if exit in stats and entry != exit: # Prevent double counting if entry==exit (which shouldn't happen for valid flow)
                stats[exit]['exiting_tonnage'] += tonnage
                stats[exit]['exiting_rakes'] += rakes
                stats[exit]['exiting_od_count'] += 1
                
            # Through stations are those touched that are neither entry nor exit
            through_stations = [s for s in touched if s != entry and s != exit]
            for s in through_stations:
                if s in stats:
                    stats[s]['through_tonnage'] += tonnage
                    stats[s]['through_rakes'] += rakes
                    stats[s]['through_od_count'] += 1

        # Build DataFrame
        records = []
        for station, s_data in stats.items():
            total_tonnage = s_data['entering_tonnage'] + s_data['exiting_tonnage'] + s_data['through_tonnage']
            total_rakes = s_data['entering_rakes'] + s_data['exiting_rakes'] + s_data['through_rakes']
            
            records.append({
                'DFC Station Code': station,
                **s_data,
                'total_tonnage': total_tonnage,
                'total_rakes': total_rakes
            })

        return pd.DataFrame(records)
