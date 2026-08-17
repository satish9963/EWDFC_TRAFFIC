"""Agent 5 -- threshold scenarios and route-combination summaries.

Thresholds used to be hard-wired to 2 and 3, which is the DFC screening
convention rather than anything intrinsic. A project comparing 1 through 6
could not express that. Scenarios are now built for whatever set the project
offers, keyed by threshold so the UI can look any of them up.
"""
import pandas as pd

from core import schema


class ScenarioFilter:
    def __init__(self, thresholds=(2, 3)):
        self.thresholds = tuple(sorted(set(int(t) for t in thresholds)))

    def filter_and_split(self, master_df):
        """Eligible / non-eligible splits at every offered threshold.

        These are always computed on interaction count, whatever rule the
        project uses for its headline verdict, because the threshold sweep is a
        sensitivity check on that one dimension.
        """
        if schema.INTERACTION_COUNT not in master_df.columns:
            raise ValueError(
                f"{schema.INTERACTION_COUNT!r} missing from the master table; "
                f"agent 3 did not run."
            )

        counts = master_df[schema.INTERACTION_COUNT]
        scenarios = {"master": master_df}
        for threshold in self.thresholds:
            eligible = master_df[counts >= threshold].copy()
            eligible[schema.THRESHOLD_APPLIED] = threshold
            eligible[schema.ELIGIBLE] = "YES"
            scenarios[f"threshold_{threshold}_eligible"] = eligible
            scenarios[f"threshold_{threshold}_non_eligible"] = master_df[counts < threshold].copy()
        return scenarios

    def generate_route_combination_summary(self, df):
        """Tonnage and units grouped by entry-exit station pair."""
        if df.empty or schema.ENTRY_STATION not in df.columns:
            return pd.DataFrame()

        working = df.copy()
        working[schema.ROUTE_COMBINATION] = (
            working[schema.ENTRY_STATION].fillna("NONE").astype(str)
            + " - "
            + working[schema.EXIT_STATION].fillna("NONE").astype(str)
        )

        aggregations = {"od_count": (schema.FROM_CODE, "count")}
        if schema.TONNAGE in working.columns:
            aggregations["total_tonnage"] = (schema.TONNAGE, "sum")
        if schema.UNITS in working.columns:
            aggregations["total_units"] = (schema.UNITS, "sum")
        if schema.CORRIDOR_KM in working.columns:
            aggregations["mean_corridor_km"] = (schema.CORRIDOR_KM, "mean")

        summary = (working.groupby(schema.ROUTE_COMBINATION, as_index=False)
                   .agg(**aggregations))
        summary = summary[summary[schema.ROUTE_COMBINATION] != "NONE - NONE"]

        sort_column = "total_tonnage" if "total_tonnage" in summary.columns else "od_count"
        return summary.sort_values(sort_column, ascending=False).reset_index(drop=True)
