"""Station code or name -> coordinates, for proximity matching.

RBS returns a route as a list of station codes with no positions, so without a
gazetteer the only question that can be asked of a route is "is this code in the
corridor list?". That works when the corridor's stations already exist on the IR
network and the client's list uses the same codes. It fails for a proposed line,
and for any list that names stations differently from RBS.

Both a code index and a name index are kept, because the two available sources
answer different questions:

  * OpenStreetMap carries IR codes in `ref`, so it can place a route's stations.
  * ESRI Living Atlas India has better coverage but no codes at all, so it can
    only place stations given by name -- which is how corridor station lists are
    frequently written.

Sources are tried in order of authority: coordinates supplied on the corridor
station list itself always win, then the bundled gazetteer. A station whose
position is unknown is reported as unmatched rather than guessed at.
"""
import csv
from pathlib import Path

DEFAULT_PATH = Path(__file__).resolve().parent.parent / "data" / "stations.csv"

# Suffixes that differ between sources for the same station: OSM writes
# "Bhusaval Junction", a corridor list writes "Bhusaval".
_NAME_SUFFIXES = (" junction", " jn", " railway station", " station", " halt")


class Gazetteer:
    def __init__(self, coordinates=None, names=None, source=""):
        self._coords = coordinates or {}
        self._names = names or {}
        self.source = source

    def __len__(self):
        return len(self._coords)

    def __contains__(self, code):
        return self._normalise(code) in self._coords

    @property
    def name_count(self):
        return len(self._names)

    @staticmethod
    def _normalise(code):
        return str(code).strip().upper()

    @staticmethod
    def normalise_name(name):
        """Collapse a station name to something comparable across sources."""
        text = str(name).strip().lower()
        changed = True
        while changed:
            changed = False
            for suffix in _NAME_SUFFIXES:
                if text.endswith(suffix):
                    text = text[: -len(suffix)].strip()
                    changed = True
        # Drop the "New " that proposed corridor stations carry but the
        # existing IR station of the same place does not.
        if text.startswith("new "):
            text = text[4:]
        return "".join(ch for ch in text if ch.isalnum())

    def get(self, code):
        """Return (lat, lon) for a station code, or None."""
        return self._coords.get(self._normalise(code))

    def get_by_name(self, name):
        """Return (lat, lon) for a station name, or None."""
        if not name:
            return None
        return self._names.get(self.normalise_name(name))

    def locate(self, code=None, name=None):
        """Best available position, preferring the code match."""
        return (self.get(code) if code else None) or (self.get_by_name(name) if name else None)

    def overlay(self, coordinates, names=None):
        """Return a copy with extra coordinates taking precedence.

        Used to layer a project's own station list over the bundled data, so a
        client's surveyed coordinates always beat a public dataset's.
        """
        merged_codes = dict(self._coords)
        for code, position in (coordinates or {}).items():
            merged_codes[self._normalise(code)] = position
        merged_names = dict(self._names)
        for name, position in (names or {}).items():
            merged_names[self.normalise_name(name)] = position
        return Gazetteer(merged_codes, merged_names,
                         source=f"{self.source}+overlay" if self.source else "overlay")

    def coverage(self, codes):
        """Share of the given codes whose position is known."""
        codes = list(codes)
        if not codes:
            return 0.0
        known = sum(1 for code in codes if self._normalise(code) in self._coords)
        return known / len(codes)


def load_gazetteer(path=DEFAULT_PATH):
    """Load the bundled gazetteer; returns an empty one if it is absent.

    Absence is not an error. Code matching works without it, and the UI reports
    that proximity matching is unavailable rather than failing a run.
    """
    path = Path(path)
    if not path.exists():
        return Gazetteer({}, {}, source="none")

    coordinates, names = {}, {}
    with open(path, newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            try:
                position = (float(row["lat"]), float(row["lon"]))
            except (KeyError, TypeError, ValueError):
                continue
            code = (row.get("code") or "").strip().upper()
            if code:
                coordinates[code] = position
            name = (row.get("name") or "").strip()
            if name:
                # First writer wins, and coded sources are written first, so a
                # name already claimed by a coded station is not overwritten.
                names.setdefault(Gazetteer.normalise_name(name), position)
    return Gazetteer(coordinates, names, source=path.name)


def from_station_frame(frame, code_column, lat_column, lon_column, name_column=None):
    """Pull coordinates out of an uploaded corridor station list.

    Returns (by_code, by_name).
    """
    by_code, by_name = {}, {}
    if frame is None or frame.empty:
        return by_code, by_name
    if not all(c in frame.columns for c in (code_column, lat_column, lon_column)):
        return by_code, by_name

    for _, row in frame.iterrows():
        try:
            lat, lon = float(row[lat_column]), float(row[lon_column])
        except (TypeError, ValueError):
            continue
        # Reject anything outside plausible bounds, which also catches the
        # centre-of-India placeholder a failed geocode leaves behind.
        if not (-90 <= lat <= 90 and -180 <= lon <= 180):
            continue
        by_code[str(row[code_column]).strip().upper()] = (lat, lon)
        if name_column and name_column in frame.columns:
            name = row.get(name_column)
            if name is not None and str(name).strip():
                by_name.setdefault(Gazetteer.normalise_name(name), (lat, lon))
    return by_code, by_name
