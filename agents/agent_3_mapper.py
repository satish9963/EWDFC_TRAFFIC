"""Agent 3 -- decide how a route interacts with the corridor.

Two questions are asked of every route:

  which of its stations are on the corridor, and how much corridor does it use?

The first was previously answered only by exact station-code match. That is
correct and cheap when the corridor is an existing line whose stations carry the
same IR codes the client's list uses -- and useless otherwise, which is most
other projects. A proposed line has no IR codes at all; a client list may name
stations differently from RBS. So codes are now one of two matchers, the other
being distance from the alignment.

The second question could not be answered at all before. Counting stations
touched treats a flow that clips one end of the corridor the same as one that
runs its full length, which for anything other than a pure DFC screen is the
wrong question. With chainage -- from the station list, or measured along the
alignment -- the corridor length a flow actually uses becomes available.
"""
from core import schema


class RouteMapping(dict):
    """Plain dict; named for readability at call sites."""


EMPTY_MAPPING = {
    "overlap": False,
    "route_origin": None,
    "route_destination": None,
    "entry_station": None,
    "exit_station": None,
    "stations_touched": [],
    "interaction_count": 0,
    "corridor_km": None,
    "corridor_share": None,
    "match_mode": "",
}


class CorridorMapper:
    def __init__(self, corridor_df, geometry=None, gazetteer=None,
                 buffer_km=5.0, by_code=True, by_proximity=False):
        self.geometry = geometry
        self.gazetteer = gazetteer
        self.buffer_km = buffer_km
        self.by_code = by_code
        self.by_proximity = by_proximity and geometry is not None and gazetteer is not None

        self.corridor_codes = set(
            corridor_df[schema.CORRIDOR_CODE].astype(str).str.strip().str.upper()
        )

        # Chainage per corridor station, preferring the client's own figures
        # over anything measured off a drawing.
        self.chainage = {}
        if schema.CHAINAGE in corridor_df.columns:
            for _, row in corridor_df.iterrows():
                value = row.get(schema.CHAINAGE)
                if value is not None and value == value:  # not NaN
                    self.chainage[str(row[schema.CORRIDOR_CODE]).strip().upper()] = float(value)
        self.chainage_source = "station list" if self.chainage else None

        # Resolved once per run, not per route: a proximity test is a point-in-
        # polygon against a buffered 2,000 km line, and routes repeat stations
        # constantly. Caching by code turns millions of tests into thousands.
        self._proximity_cache = {}
        self.spatial_matches = set()

        # Proximity matching can only ever see stations whose position is known.
        # Tracking what it could not place is the difference between "this route
        # does not touch the corridor" and "we could not tell".
        self.seen_stations = set()
        self.unlocatable_stations = set()

    @property
    def gazetteer_coverage(self):
        """Share of route stations this run could place, or None if unused."""
        if not self.by_proximity or not self.seen_stations:
            return None
        located = len(self.seen_stations) - len(self.unlocatable_stations)
        return located / len(self.seen_stations)

    # -- membership -------------------------------------------------------

    def _is_near_corridor(self, code):
        if not self.by_proximity:
            return False
        if code in self._proximity_cache:
            return self._proximity_cache[code]
        self.seen_stations.add(code)
        position = self.gazetteer.get(code)
        if position is None:
            self.unlocatable_stations.add(code)
            self._proximity_cache[code] = False
            return False
        inside = self.geometry.contains(position[0], position[1], self.buffer_km)
        self._proximity_cache[code] = inside
        if inside:
            self.spatial_matches.add(code)
        return inside

    def _chainage_for(self, code):
        """Chainage of a corridor station, from the list or from the alignment."""
        if code in self.chainage:
            return self.chainage[code]
        if self.geometry is not None and self.geometry.chainage_available and self.gazetteer:
            position = self.gazetteer.get(code)
            if position is not None:
                return self.geometry.chainage_km(position[0], position[1])
        return None

    # -- main entry point -------------------------------------------------

    def map_route(self, route_sequence, ir_distance=None):
        if not route_sequence:
            return dict(EMPTY_MAPPING)

        touched, modes = [], set()
        for station in route_sequence:
            code = str(station).strip().upper()
            matched_by_code = self.by_code and code in self.corridor_codes
            matched_by_space = (not matched_by_code) and self._is_near_corridor(code)
            if matched_by_code or matched_by_space:
                touched.append(code)
                modes.add("CODE" if matched_by_code else "SPATIAL")

        interaction_count = len(touched)
        origin = route_sequence[0]
        destination = route_sequence[-1]

        if interaction_count == 0:
            mapping = dict(EMPTY_MAPPING)
            mapping.update({"route_origin": origin, "route_destination": destination})
            return mapping

        entry, exit_ = touched[0], touched[-1]

        # Corridor length used is the span between the extreme chainages of the
        # stations touched -- not entry-to-exit, because a route can run out and
        # back, and not the sum of gaps, because that double-counts.
        corridor_km = None
        chainages = [c for c in (self._chainage_for(code) for code in touched) if c is not None]
        if len(chainages) >= 2:
            corridor_km = abs(max(chainages) - min(chainages))
        elif len(chainages) == 1:
            corridor_km = 0.0

        corridor_share = None
        if corridor_km is not None and ir_distance:
            try:
                # Clamp: a corridor measured off a drawing and a distance from
                # the portal are different measurements of the same ground, and
                # rounding can otherwise yield a share above 1.
                corridor_share = min(1.0, corridor_km / float(ir_distance))
            except (TypeError, ValueError, ZeroDivisionError):
                corridor_share = None

        return {
            "overlap": True,
            "route_origin": origin,
            "route_destination": destination,
            "entry_station": entry,
            "exit_station": exit_,
            "stations_touched": touched,
            "interaction_count": interaction_count,
            "corridor_km": corridor_km,
            "corridor_share": corridor_share,
            "match_mode": "MIXED" if len(modes) > 1 else next(iter(modes)),
        }
