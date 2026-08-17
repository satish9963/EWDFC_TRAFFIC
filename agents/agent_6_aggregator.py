"""Agent 6 -- station-wise traffic totals.

Each corridor station gets entering, exiting and through traffic. The
distinction matters for infrastructure sizing: entering and exiting traffic
needs yard and handling capacity, through traffic needs only line capacity.

This module is where a column-name mismatch once zeroed every through-traffic
figure for weeks -- the orchestrator wrote "DFC stations touched", this read
"dfc_stations_touched", and `.get()` returned its default without complaint.
Both sides now use `schema.STATIONS_TOUCHED`, and the test in
tests/test_aggregator.py fails loudly if through traffic ever returns to zero
on data that contains some.
"""
import pandas as pd

from core import schema


class TrafficAggregator:
    def __init__(self, corridor_df):
        self.corridor_stations = (
            corridor_df[schema.CORRIDOR_CODE].astype(str).str.strip().str.upper().unique()
        )
        self.names = {}
        if schema.CORRIDOR_NAME in corridor_df.columns:
            for _, row in corridor_df.iterrows():
                code = str(row[schema.CORRIDOR_CODE]).strip().upper()
                self.names[code] = row.get(schema.CORRIDOR_NAME)
        self.chainage = {}
        if schema.CHAINAGE in corridor_df.columns:
            for _, row in corridor_df.iterrows():
                value = row.get(schema.CHAINAGE)
                if value is not None and value == value:
                    self.chainage[str(row[schema.CORRIDOR_CODE]).strip().upper()] = value

    @staticmethod
    def _as_list(value):
        """Touched stations arrive as a list in-process, a string from Excel."""
        if isinstance(value, list):
            return value
        if isinstance(value, str):
            return [part.strip().upper() for part in value.split(",") if part.strip()]
        return []

    def aggregate(self, df):
        if df.empty:
            return pd.DataFrame()

        stats = {
            station: {key: 0 for key in schema.STATION_STAT_KEYS}
            for station in self.corridor_stations
        }

        for _, row in df.iterrows():
            tonnage = row.get(schema.TONNAGE, 0) or 0
            units = row.get(schema.UNITS, 0) or 0
            entry = row.get(schema.ENTRY_STATION)
            exit_ = row.get(schema.EXIT_STATION)
            touched = self._as_list(row.get(schema.STATIONS_TOUCHED, []))

            if entry in stats:
                stats[entry][schema.ENTERING_TONNAGE] += tonnage
                stats[entry][schema.ENTERING_UNITS] += units
                stats[entry][schema.ENTERING_OD_COUNT] += 1

            # A flow entering and leaving at the same station is counted once.
            if exit_ in stats and entry != exit_:
                stats[exit_][schema.EXITING_TONNAGE] += tonnage
                stats[exit_][schema.EXITING_UNITS] += units
                stats[exit_][schema.EXITING_OD_COUNT] += 1

            for station in touched:
                if station in stats and station != entry and station != exit_:
                    stats[station][schema.THROUGH_TONNAGE] += tonnage
                    stats[station][schema.THROUGH_UNITS] += units
                    stats[station][schema.THROUGH_OD_COUNT] += 1

        records = []
        for station, values in stats.items():
            record = {schema.CORRIDOR_CODE: station}
            if self.names:
                record[schema.CORRIDOR_NAME] = self.names.get(station)
            if self.chainage:
                record[schema.CHAINAGE] = self.chainage.get(station)
            record.update(values)
            record[schema.TOTAL_TONNAGE] = (
                values[schema.ENTERING_TONNAGE]
                + values[schema.EXITING_TONNAGE]
                + values[schema.THROUGH_TONNAGE]
            )
            record[schema.TOTAL_UNITS] = (
                values[schema.ENTERING_UNITS]
                + values[schema.EXITING_UNITS]
                + values[schema.THROUGH_UNITS]
            )
            records.append(record)

        summary = pd.DataFrame(records)
        if schema.CHAINAGE in summary.columns and summary[schema.CHAINAGE].notna().any():
            # Corridor order is far more useful than alphabetical for reading a
            # station table or plotting a loading diagram.
            return summary.sort_values(schema.CHAINAGE).reset_index(drop=True)
        return summary.sort_values(schema.TOTAL_TONNAGE, ascending=False).reset_index(drop=True)
