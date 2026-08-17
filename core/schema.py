"""Canonical column names, and the aliases accepted on input.

Every module refers to a column through a constant defined here rather than by
writing the string inline. This is not stylistic: a previous version of this
pipeline wrote "DFC stations touched" in the orchestrator and read
"dfc_stations_touched" in the aggregator, so `.get()` fell through to its
default and every through-traffic figure was zero for weeks. A misspelt
constant fails at import; a misspelt literal fails silently in a deliverable.

Input aliasing is what lets the tool take a workbook from any railway project
rather than only the one it was written for. Railway OD exports name the same
seven fields a dozen different ways, and asking a client to re-head their
spreadsheet is how a tool stops being used.
"""

# --- OD traffic input ----------------------------------------------------
FROM_CODE = "From Station Code"
FROM_NAME = "From Station Name"
TO_CODE = "To Station Code"
TO_NAME = "To Station Name"
COMMODITY = "Commodity"
TONNAGE = "Annual Tonnage"
UNITS = "No. of Rakes / Wagon Units"

OD_COLUMNS = [FROM_CODE, FROM_NAME, TO_CODE, TO_NAME, COMMODITY, TONNAGE, UNITS]

# Only origin and destination are structurally required; the rest are filled
# with neutral defaults so a minimal two-column OD list still runs.
OD_REQUIRED = [FROM_CODE, TO_CODE]

# --- Corridor station list input -----------------------------------------
CORRIDOR_CODE = "Corridor Station Code"
CORRIDOR_NAME = "Station Name"
CHAINAGE = "Chainage"
LATITUDE = "Latitude"
LONGITUDE = "Longitude"

CORRIDOR_COLUMNS = [CORRIDOR_CODE, CORRIDOR_NAME, CHAINAGE, LATITUDE, LONGITUDE]
CORRIDOR_REQUIRED = [CORRIDOR_CODE]

# --- Pipeline output -----------------------------------------------------
IR_DISTANCE = "IR Distance"
ROUTE = "Route"
CORRIDOR_OVERLAP = "Corridor Overlap"
ROUTE_ORIGIN = "Route Origin"
ROUTE_DESTINATION = "Route Destination"
ENTRY_STATION = "Entry Corridor Station"
EXIT_STATION = "Exit Corridor Station"
STATIONS_TOUCHED = "Corridor Stations Touched"
INTERACTION_COUNT = "Interaction Count"
CORRIDOR_KM = "Corridor Km Used"
CORRIDOR_SHARE = "Corridor Share"
MATCH_MODE = "Match Mode"
THRESHOLD_APPLIED = "Threshold Applied"
ELIGIBLE = "Eligible"
ROUTE_COMBINATION = "Route Combination"
ROUTE_FETCHED_AT = "Route Fetched At"
# CACHE | FETCHED | NO_ROUTE | ERROR -- where this row's route came from.
# Without it a blank IR Distance is ambiguous between "the portal has no route
# for this pair" and "the portal could not be reached".
ROUTE_SOURCE = "Route Source"

# --- Station summary output ----------------------------------------------
ENTERING_TONNAGE = "entering_tonnage"
ENTERING_UNITS = "entering_units"
ENTERING_OD_COUNT = "entering_od_count"
EXITING_TONNAGE = "exiting_tonnage"
EXITING_UNITS = "exiting_units"
EXITING_OD_COUNT = "exiting_od_count"
THROUGH_TONNAGE = "through_tonnage"
THROUGH_UNITS = "through_units"
THROUGH_OD_COUNT = "through_od_count"
TOTAL_TONNAGE = "total_tonnage"
TOTAL_UNITS = "total_units"

MOVEMENT_TONNAGE_COLUMNS = [ENTERING_TONNAGE, EXITING_TONNAGE, THROUGH_TONNAGE]

STATION_STAT_KEYS = [
    ENTERING_TONNAGE, ENTERING_UNITS, ENTERING_OD_COUNT,
    EXITING_TONNAGE, EXITING_UNITS, EXITING_OD_COUNT,
    THROUGH_TONNAGE, THROUGH_UNITS, THROUGH_OD_COUNT,
]

# --- Accepted input aliases ----------------------------------------------
# Keys are compared after casefolding and stripping non-alphanumerics, so
# "FROM STTN", "from_sttn" and "From-Sttn" all collapse to "fromsttn".
_ALIASES = {
    FROM_CODE: [
        "fromsttn", "fromstationcode", "fromcode", "origincode", "originstationcode",
        "sourcecode", "srccode", "src", "ocode", "originstation", "fromstn",
        "stationfromcode", "loadingstationcode", "fromstationcd",
    ],
    FROM_NAME: [
        "fromname", "fromstationname", "originname", "originstationname",
        "sourcename", "fromstnname", "loadingstationname",
    ],
    TO_CODE: [
        "tosttn", "tostationcode", "tocode", "destinationcode", "destcode",
        "destinationstationcode", "dest", "dcode", "destinationstation", "tostn",
        "stationtocode", "unloadingstationcode", "tostationcd",
    ],
    TO_NAME: [
        "toname", "tostationname", "destinationname", "destname",
        "destinationstationname", "tostnname", "unloadingstationname",
    ],
    COMMODITY: [
        "commodity", "commoditygroup", "commodityname", "commoditytype",
        "cmdt", "cmdtgroup", "goods", "goodstype", "material",
    ],
    TONNAGE: [
        "annualtonnage", "tonnage", "tonnes", "tons", "annualtonnes",
        "totaltonnage", "nmt", "mtpa", "annualtraffic", "traffictonnage",
        "weight", "qtytonnes", "quantity", "annualthroughput",
    ],
    UNITS: [
        "noofrakesnoofunits", "noofrakeswagonunits", "noofrakes", "rakes",
        "norakes", "noofunits", "units", "wagonunits", "rakesannum",
        "noofrakesannum", "annualrakes", "wagons", "noofwagons",
    ],
    CORRIDOR_CODE: [
        "corridorstationcode", "dfcstationcode", "stationcode", "code",
        "stncode", "corridorcode", "stationcd", "dfccode", "nodecode",
    ],
    CORRIDOR_NAME: [
        "stationname", "name", "corridorstationname", "dfcstationname",
        "stnname", "junctionstationname", "nodename",
    ],
    CHAINAGE: [
        "chainage", "centerchainage", "centrechainage", "chainagekm",
        "km", "chkm", "chainagem", "distancefromstart", "cumulativechainage",
    ],
    LATITUDE: ["latitude", "lat", "ycoord", "y", "latdd", "latitudedd"],
    LONGITUDE: ["longitude", "lon", "lng", "long", "xcoord", "x", "londd", "longitudedd"],
}


def normalise(name):
    """Reduce a header to a comparable key: lowercase alphanumerics only."""
    return "".join(ch for ch in str(name).lower() if ch.isalnum())


def normalise_station_code(code):
    """Canonical station code.

    Excel is the reason this exists. A code column containing any blank cell is
    read by pandas as float64, so the code 1234 arrives as "1234.0" and never
    matches anything in the cache -- the lookup misses, IR Distance comes out
    empty, and nothing anywhere reports an error.

    Every place that produces or consumes a station code must use this, or the
    two sides disagree silently. That is the same failure mode as the column
    names above, which is why it lives here rather than in one agent.
    """
    text = str(code).strip().upper()
    if text.endswith(".0") and text[:-2].isdigit():
        text = text[:-2]
    return text


# Reverse index built once: normalised alias -> canonical column name.
ALIAS_TO_CANONICAL = {}
for canonical, aliases in _ALIASES.items():
    ALIAS_TO_CANONICAL[normalise(canonical)] = canonical
    for alias in aliases:
        ALIAS_TO_CANONICAL[alias] = canonical


def resolve_columns(columns):
    """Map a workbook's headers onto canonical names.

    Returns {original: canonical} for headers that are recognised. Unknown
    headers are left out and so survive into the output untouched -- a client's
    extra columns (division, zone, contract reference) are worth keeping.
    """
    mapping = {}
    claimed = set()
    for column in columns:
        canonical = ALIAS_TO_CANONICAL.get(normalise(column))
        # First header to claim a canonical name wins, so a duplicate later
        # column cannot overwrite a good one.
        if canonical and canonical not in claimed:
            claimed.add(canonical)
            if str(column) != canonical:
                mapping[column] = canonical
    return mapping
