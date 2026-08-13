import pandas as pd

class QAAudit:
    def __init__(self):
        self.exception_log = []

    def log_exception(self, row_index, issue, details=""):
        self.exception_log.append({
            "Row Index": row_index,
            "Issue": issue,
            "Details": details
        })

    def run_audit(self, original_df, final_df, dfc_stations):
        """
        Validates OD totals, routes, and creates an exception log.
        """
        original_tonnage = original_df['Annual Tonnage'].sum() if 'Annual Tonnage' in original_df.columns else 0
        final_tonnage = final_df['Annual Tonnage'].sum() if 'Annual Tonnage' in final_df.columns else 0
        
        if abs(original_tonnage - final_tonnage) > 0.1:
            self.log_exception(-1, "Tonnage Mismatch", f"Original: {original_tonnage}, Final: {final_tonnage}")

        for idx, row in final_df.iterrows():
            if pd.isna(row.get('IR Distance')) or row.get('IR Distance') == 0:
                self.log_exception(idx, "Missing Route / Portal Failure", f"Source: {row.get('From Station Code')}, Dest: {row.get('To Station Code')}")
            
            if row.get('Eligible') == 'YES' and row.get('Interaction Count', 0) < row.get('Threshold Applied', 2):
                self.log_exception(idx, "Logic Error", f"Eligible with count {row.get('Interaction Count')} < threshold {row.get('Threshold Applied')}")

        return pd.DataFrame(self.exception_log)
