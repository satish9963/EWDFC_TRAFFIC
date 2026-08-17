"""Agent 7 -- QA audit and exception log.

The audit answers "what should not be trusted in this run". It is the only
place that reports what the pipeline could not do, so it errs towards saying
too much: a route the portal never returned, a flow whose eligibility could not
be evaluated, tonnage that changed between input and output.
"""
import pandas as pd

from core import schema
from agents.agent_4_diversion import UNKNOWN


class QAAudit:
    def __init__(self):
        self.exception_log = []

    def log(self, row_index, issue, details=""):
        self.exception_log.append({"Row Index": row_index, "Issue": issue, "Details": details})

    def run_audit(self, original_df, final_df, corridor_df, mapper=None, validation=None):
        # A tonnage column that was defaulted rather than found makes every
        # tonnage figure in the report a zero that looks like a measurement.
        # Say so loudly; this is the same shape of failure as the through-traffic
        # bug, where a plausible zero went unquestioned for weeks.
        filled = (validation or {}).get("filled_columns") or []
        if schema.TONNAGE in filled:
            self.log(-1, "Tonnage column not found",
                     "No tonnage column was recognised in the OD workbook, so every "
                     "tonnage figure in this run is zero by default, not by measurement. "
                     "Route and station counts remain valid.")
        if schema.UNITS in filled:
            self.log(-1, "Rake/unit column not found",
                     "No rake or wagon-unit column was recognised; those totals are zero "
                     "by default.")
        return self._audit(original_df, final_df, corridor_df, mapper)

    def _audit(self, original_df, final_df, corridor_df, mapper=None):
        if schema.TONNAGE in original_df.columns and schema.TONNAGE in final_df.columns:
            original = original_df[schema.TONNAGE].sum()
            final = final_df[schema.TONNAGE].sum()
            # Relative tolerance: an absolute 0.1 is meaningless against a
            # hundred-million-tonne total and spurious against a tiny one.
            if abs(original - final) > max(0.1, abs(original) * 1e-9):
                self.log(-1, "Tonnage mismatch",
                         f"Input {original:,.2f} vs output {final:,.2f}")

        missing_route = 0
        portal_errors = 0
        unknown_eligibility = 0
        no_overlap = 0

        for index, row in final_df.iterrows():
            distance = row.get(schema.IR_DISTANCE)
            if pd.isna(distance) or distance == 0:
                missing_route += 1
                # A portal failure and a genuine no-route are different facts
                # needing different follow-up: one is "retry", the other is
                # "check the station codes".
                state = row.get(schema.ROUTE_SOURCE, "UNKNOWN")
                if state == "ERROR":
                    portal_errors += 1
                    issue = "RBS portal failure (retry)"
                elif state == "NO_ROUTE":
                    issue = "No route returned by RBS"
                else:
                    issue = "Missing route"
                self.log(index, issue,
                         f"{row.get(schema.FROM_CODE)} -> {row.get(schema.TO_CODE)}")

            if row.get(schema.ELIGIBLE) == UNKNOWN:
                unknown_eligibility += 1
                self.log(index, "Eligibility not evaluated",
                         "The selected rule needs chainage or distance that this row lacks.")

            if row.get(schema.CORRIDOR_OVERLAP) == "NO":
                no_overlap += 1

        if missing_route:
            self.log(-1, "Summary: routes unresolved",
                     f"{missing_route} of {len(final_df)} rows have no route from the portal.")
        if portal_errors:
            # Worth separating loudly: these rows are recoverable by re-running
            # once the portal is reachable, whereas a genuine no-route is not.
            self.log(-1, "Summary: portal unreachable",
                     f"{portal_errors} row(s) failed on the connection rather than on the "
                     f"data. Re-run once the portal is reachable; purge the ERROR rows "
                     f"first with RBSCache().purge_status('ERROR').")
        if unknown_eligibility:
            self.log(-1, "Summary: eligibility unknown",
                     f"{unknown_eligibility} of {len(final_df)} rows could not be judged.")
        if len(final_df) and no_overlap == len(final_df):
            # The single most common misconfiguration: a corridor station list
            # whose codes are not IR codes, so nothing can ever match.
            self.log(-1, "No route touches the corridor",
                     "Check that the corridor station codes are the same codes the "
                     "RBS portal returns, or enable proximity matching.")

        if mapper is not None and getattr(mapper, "spatial_matches", None):
            self.log(-1, "Stations matched by proximity",
                     f"{len(mapper.spatial_matches)} station(s) were treated as on-corridor "
                     f"by distance rather than code: "
                     f"{', '.join(sorted(mapper.spatial_matches)[:20])}")

        return pd.DataFrame(self.exception_log)
