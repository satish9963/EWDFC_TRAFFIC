"""Display helpers.

Figures are shown in whatever unit the uploaded workbook uses. Nothing here
converts or rescales a value — these only group digits and add a percent sign.
"""
import pandas as pd

MISSING = "—"

# Column-name fragments that should stay upper case when titleised.
ACRONYMS = {"od": "OD", "dfc": "DFC", "ir": "IR"}


def format_number(value, decimals=0):
    """Group thousands. Returns an em dash for a missing value rather than 0."""
    if value is None or pd.isna(value):
        return MISSING
    return f"{value:,.{decimals}f}"


def format_percent(part, whole, decimals=1):
    """Share of a total. Returns an em dash when the total is zero or missing."""
    if whole is None or pd.isna(whole) or whole == 0:
        return MISSING
    if part is None or pd.isna(part):
        return MISSING
    return f"{100 * part / whole:.{decimals}f}%"


def _titleise(name):
    if " " in name:  # already a display label, e.g. "DFC Station Code"
        return name
    words = [ACRONYMS.get(w.lower(), w.capitalize()) for w in name.split("_")]
    return " ".join(words)


def display_columns(df):
    """Rename snake_case pipeline columns to readable labels, for display only."""
    return df.rename(columns={c: _titleise(str(c)) for c in df.columns})
