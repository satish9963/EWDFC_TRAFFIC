"""Agent 1 -- validate and normalise the two input workbooks.

The tool is only as general as this agent is forgiving. Every railway project
hands over its OD data in a different shape: IR's own exports use FROMSTTN and
TOSTTN, consultants' workbooks use "Origin Code", client submissions bury the
headers under a merged title row. Rejecting those is what would confine this
pipeline to the one corridor it was written for, so headers are resolved through
the alias table in core.schema rather than matched literally.

What is *not* forgiving: tonnage units. Figures are carried through in whatever
unit the workbook uses, and no conversion is attempted, because a workbook that
says MTPA and one that says tonnes look identical to a parser and guessing
wrong changes a result by a factor of a million.
"""
import pandas as pd

from core import schema


class ValidationReport:
    """What the validator did, so the UI can show it rather than assume it."""

    def __init__(self):
        self.renamed = {}
        self.dropped_missing_codes = 0
        # Corridor stations: a station listed twice is one station, so the
        # repeat is dropped. OD rows carry quantities, so identical rows are
        # combined instead -- see _merge_duplicate_rows.
        self.dropped_duplicates = 0
        self.merged_duplicates = 0
        self.filled_columns = []
        self.rows_in = 0
        self.rows_out = 0
        self.notes = []

    def as_dict(self):
        return {
            "rows_in": self.rows_in,
            "rows_out": self.rows_out,
            "renamed": self.renamed,
            "dropped_missing_codes": self.dropped_missing_codes,
            "dropped_duplicates": self.dropped_duplicates,
            "merged_duplicates": self.merged_duplicates,
            "filled_columns": self.filled_columns,
            "notes": self.notes,
        }


def _merge_duplicate_rows(clean):
    """Combine identical OD rows by summing their quantities.

    These used to be discarded outright. That is wrong for freight OD: a row is
    a movement with a tonnage and a rake count, so two identical rows are two
    movements, not one recorded twice. Dropping the second deleted a real
    consignment and said nothing about it -- on a 555-row dataset it removed
    250 rakes and 84,410 tonnes, 24.4% of the total.

    Only *exactly* identical rows are combined, so nothing is inferred: rows
    differing in any field, tonnage included, stay separate. The row count still
    falls, which is why the count is reported rather than left to be noticed.

    Returns (frame, rows_merged).
    """
    before = len(clean)
    # Checking first keeps the common case cheap -- the 345,601-row 2024-25
    # dataset has no duplicate rows at all, and this avoids a wide groupby.
    if not clean.duplicated().any():
        return clean, 0

    columns = list(clean.columns)
    quantities = [c for c in (schema.TONNAGE, schema.UNITS) if c in columns]
    merged = (clean.groupby(columns, dropna=False, sort=False, as_index=False)
              .size())
    for column in quantities:
        merged[column] = merged[column] * merged["size"]
    merged = merged.drop(columns="size")[columns]
    return merged, before - len(merged)


def drop_empty_columns(df):
    """Discard columns that are entirely empty.

    Real client workbooks arrive with enormous phantom column ranges -- the
    EWDFC junction-chainage sheet is 76 rows by 16,382 columns, all but 9 of
    them empty, because somebody once formatted to the end of the sheet. Left
    in, they dominate every downstream operation and make header resolution
    scan sixteen thousand names to find nine.
    """
    if df.empty:
        return df
    keep = [c for c in df.columns if not df[c].isna().all()]
    return df[keep] if keep else df


def _promote_header_row(df, expected_hint_columns, max_scan=5):
    """Handle workbooks whose real header sits below a title row.

    Exports frequently open with a merged banner ("Base Year (2024-25)"), which
    pandas reads as the header and turns the true headers into row 0. The giveaway
    is a frame full of 'Unnamed:' columns.
    """
    unnamed = sum(1 for c in df.columns if str(c).startswith("Unnamed:"))
    if unnamed < max(1, len(df.columns) // 2):
        return df, False

    for row_index in range(min(max_scan, len(df))):
        candidate = df.iloc[row_index]
        resolved = schema.resolve_columns(candidate.values)
        recognised = set(resolved.values()) | {
            schema.ALIAS_TO_CANONICAL.get(schema.normalise(v))
            for v in candidate.values if v is not None
        }
        if len(recognised & set(expected_hint_columns)) >= 2:
            promoted = df.iloc[row_index + 1:].copy()
            promoted.columns = candidate.values
            return promoted.reset_index(drop=True), True
    return df, False


def _to_number(series):
    """Coerce a column that may carry thousands separators or stray text."""
    cleaned = (series.astype(str)
               .str.replace(",", "", regex=False)
               .str.replace(" ", "", regex=False)
               .str.strip())
    return pd.to_numeric(cleaned, errors="coerce").fillna(0)


class InputValidator:
    def validate_od_data(self, df, report=None):
        report = report or ValidationReport()
        report.rows_in = len(df)
        df = drop_empty_columns(df)

        df, promoted = _promote_header_row(df, [schema.FROM_CODE, schema.TO_CODE])
        if promoted:
            report.notes.append("Header row found below a title row and promoted.")

        mapping = schema.resolve_columns(df.columns)
        if mapping:
            df = df.rename(columns=mapping)
            report.renamed = {str(k): v for k, v in mapping.items()}

        df = df.loc[:, ~df.columns.duplicated()]

        missing = [c for c in schema.OD_REQUIRED if c not in df.columns]
        if missing:
            raise ValueError(
                f"The OD workbook is missing {missing}. Recognised columns were "
                f"{sorted(set(df.columns) & set(schema.OD_COLUMNS))}. Rename the "
                f"origin and destination code columns, or download the blank template."
            )

        # Optional columns are filled rather than demanded: a two-column OD list
        # is a legitimate input for a first-pass corridor screen.
        for column, default in (
            (schema.COMMODITY, "UNKNOWN"),
            (schema.TONNAGE, 0),
            (schema.UNITS, 0),
            (schema.FROM_NAME, ""),
            (schema.TO_NAME, ""),
        ):
            if column not in df.columns:
                df[column] = default
                report.filled_columns.append(column)

        clean = df.dropna(subset=schema.OD_REQUIRED).copy()
        report.dropped_missing_codes = len(df) - len(clean)

        # normalise_station_code, not .str.upper(): a code column with any blank
        # cell is read as float64, so 1234 becomes "1234.0" and never matches
        # the route cache. The scraper normalises the same way.
        for column in (schema.FROM_CODE, schema.TO_CODE):
            clean[column] = clean[column].map(schema.normalise_station_code)

        # Codes that survived as empty strings or pandas' "NAN" text.
        blank = clean[schema.FROM_CODE].isin(["", "NAN", "NONE"]) | \
            clean[schema.TO_CODE].isin(["", "NAN", "NONE"])
        report.dropped_missing_codes += int(blank.sum())
        clean = clean[~blank]

        # Quantities must be numeric before identical rows are combined.
        for column in (schema.TONNAGE, schema.UNITS):
            if column in clean.columns:
                clean[column] = _to_number(clean[column])

        clean, report.merged_duplicates = _merge_duplicate_rows(clean)

        if schema.COMMODITY in clean.columns:
            clean[schema.COMMODITY] = (clean[schema.COMMODITY].fillna("UNKNOWN")
                                       .astype(str).str.strip().replace("", "UNKNOWN"))

        report.rows_out = len(clean)
        if clean.empty:
            raise ValueError(
                "No usable OD rows survived validation -- every row was missing an "
                "origin or destination station code."
            )
        return clean.reset_index(drop=True), report

    def validate_corridor_stations(self, df, report=None):
        """Validate the corridor station list.

        Only the station code is required. Coordinates and chainage unlock
        proximity matching and corridor-length metrics respectively, and their
        absence disables those features rather than failing the run.
        """
        report = report or ValidationReport()
        report.rows_in = len(df)
        df = drop_empty_columns(df)

        df, promoted = _promote_header_row(df, [schema.CORRIDOR_CODE, schema.CORRIDOR_NAME])
        if promoted:
            report.notes.append("Header row found below a title row and promoted.")

        mapping = schema.resolve_columns(df.columns)
        if mapping:
            df = df.rename(columns=mapping)
            report.renamed = {str(k): v for k, v in mapping.items()}

        df = df.loc[:, ~df.columns.duplicated()]

        if schema.CORRIDOR_CODE not in df.columns:
            raise ValueError(
                f"The corridor station list needs a station code column. Recognised "
                f"columns were {sorted(set(df.columns) & set(schema.CORRIDOR_COLUMNS))}. "
                f"Any of 'Corridor Station Code', 'DFC Station Code' or 'Station Code' works."
            )

        clean = df.dropna(subset=[schema.CORRIDOR_CODE]).copy()
        clean[schema.CORRIDOR_CODE] = clean[schema.CORRIDOR_CODE].map(
            schema.normalise_station_code)
        clean = clean[~clean[schema.CORRIDOR_CODE].isin(["", "NAN", "NONE"])]

        for column in (schema.LATITUDE, schema.LONGITUDE, schema.CHAINAGE):
            if column in clean.columns:
                clean[column] = pd.to_numeric(clean[column], errors="coerce")

        has_coordinates = (
            schema.LATITUDE in clean.columns and schema.LONGITUDE in clean.columns
            and clean[[schema.LATITUDE, schema.LONGITUDE]].notna().all(axis=1).any()
        )
        if not has_coordinates:
            report.notes.append(
                "No usable coordinates on the station list; proximity matching will "
                "rely on the bundled gazetteer or be unavailable."
            )
        if schema.CHAINAGE not in clean.columns or clean[schema.CHAINAGE].isna().all():
            report.notes.append(
                "No chainage on the station list; corridor length used will be "
                "derived from the alignment if one is supplied."
            )

        before = len(clean)
        clean = clean.drop_duplicates(subset=[schema.CORRIDOR_CODE], keep="first")
        report.dropped_duplicates = before - len(clean)
        report.rows_out = len(clean)

        if clean.empty:
            raise ValueError("The corridor station list contains no usable station codes.")
        return clean.reset_index(drop=True), report

    # Kept so existing callers and tests that used the DFC-era name keep working.
    validate_dfc_stations = validate_corridor_stations
