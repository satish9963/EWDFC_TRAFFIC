"""Corridor alignments read from KML/KMZ, GeoJSON or shapefile.

Why this exists: a railway project is normally handed over as a drawing, not as
a list of station codes. Requiring the code list first meant somebody had to
hand-convert the alignment before the tool could be used at all, and it meant a
route running physically alongside the corridor scored zero if its IR station
codes happened not to appear in that list.

Two things are extracted from an alignment:

  * the corridor centreline, which gives chainage (how far along the corridor a
    point sits) and therefore corridor-km used by a flow;
  * station placemarks, which seed the corridor station list when the drawing
    carries them.

Distances are computed in an azimuthal equidistant projection centred on the
corridor itself. Buffering in degrees would be wrong -- a degree of longitude
is about 96 km at Kanyakumari and 87 km at Amritsar -- and a national grid like
Web Mercator inflates distance by the secant of the latitude, roughly 1.2x
across northern India. Centring the projection keeps the error negligible over
a corridor's own extent.
"""
import json
import math
import zipfile
from pathlib import Path

# defusedxml, not xml.etree: this app accepts KML uploads from whoever can
# reach it, and the stdlib parser will happily expand a billion-laughs bomb or
# fetch an external entity off the filesystem.
from defusedxml.ElementTree import fromstring as safe_fromstring
from pyproj import CRS, Transformer
from shapely.geometry import LineString, MultiLineString, Point, shape
from shapely.ops import linemerge, transform as shapely_transform

KML_NS = "{http://www.opengis.net/kml/2.2}"

# A placemark folder is treated as station-bearing when its name says so.
STATION_HINTS = ("station", "stn", "junction", "halt", "yard", "terminal")
# ...and as the alignment when its name says that instead.
ALIGNMENT_HINTS = ("align", "centre line", "center line", "centreline",
                   "centerline", "corridor", "route", "track")


# Endpoint snapping tolerance in degrees, ~10 cm. CAD exports write the shared
# endpoint of two adjacent polylines with float noise past the seventh decimal,
# so linemerge sees 44,000 disjoint segments where there is one railway. At 1e-6
# the EWDFC alignment collapses to a single continuous line with no measurable
# change in length; at 1e-4 it starts short-cutting curves.
STITCH_TOLERANCE_DEG = 1e-6


def stitch_lines(lines, tolerance=STITCH_TOLERANCE_DEG):
    """Merge segments into as few continuous lines as the geometry allows."""
    if not lines:
        raise ValueError("No line geometry to stitch")
    snapped = []
    for line in lines:
        coords = [(round(x / tolerance) * tolerance, round(y / tolerance) * tolerance)
                  for x, y in line.coords]
        # Snapping can collapse a short segment to a single point.
        deduped = [coords[0]]
        for point in coords[1:]:
            if point != deduped[-1]:
                deduped.append(point)
        if len(deduped) >= 2:
            snapped.append(LineString(deduped))
    if not snapped:
        raise ValueError("All line geometry collapsed while stitching")
    if len(snapped) == 1:
        return snapped[0]
    return linemerge(MultiLineString(snapped))


# Sections of one corridor are often drawn as separate files and exported with
# a small gap where they abut -- 575 m and 687 m between the three EWDFC
# sections. Bridging those recovers a single ordered centreline; anything
# larger is a genuine break or a branch and is left alone.
MAX_BRIDGE_GAP_KM = 1.0
_MAX_BRIDGE_COMPONENTS = 200


def _endpoint_gap_km(a, b):
    """Smallest distance between the endpoints of two lines, with how to join."""
    ends_a = [a.coords[0], a.coords[-1]]
    ends_b = [b.coords[0], b.coords[-1]]
    best = None
    for i, pa in enumerate(ends_a):
        for j, pb in enumerate(ends_b):
            dx = (pb[0] - pa[0]) * 111.32 * math.cos(math.radians((pa[1] + pb[1]) / 2))
            dy = (pb[1] - pa[1]) * 110.57
            gap = math.hypot(dx, dy)
            if best is None or gap < best[0]:
                best = (gap, i, j)
    return best


def bridge_gaps(geometry, max_gap_km=MAX_BRIDGE_GAP_KM):
    """Join components separated by less than max_gap_km.

    Returns (geometry, bridges) where bridges lists the gap lengths closed, so
    the caller can report what was assumed rather than silently presenting a
    stitched line as if it came that way.
    """
    parts = list(geometry.geoms) if geometry.geom_type == "MultiLineString" else [geometry]
    bridges = []
    if len(parts) < 2 or len(parts) > _MAX_BRIDGE_COMPONENTS:
        return geometry, bridges

    while len(parts) > 1:
        best = None
        for i in range(len(parts)):
            for j in range(i + 1, len(parts)):
                gap, end_i, end_j = _endpoint_gap_km(parts[i], parts[j])
                if best is None or gap < best[0]:
                    best = (gap, i, j, end_i, end_j)
        gap, i, j, end_i, end_j = best
        if gap > max_gap_km:
            break
        # Orient both so the joining endpoints meet in the middle.
        left = list(parts[i].coords)
        right = list(parts[j].coords)
        if end_i == 0:
            left.reverse()
        if end_j == 1:
            right.reverse()
        joined = LineString(left + right)
        parts = [p for k, p in enumerate(parts) if k not in (i, j)] + [joined]
        bridges.append(gap)

    merged = parts[0] if len(parts) == 1 else MultiLineString(parts)
    return merged, bridges


class CorridorGeometry:
    """A corridor centreline plus whatever station points came with it.

    The full geometry is kept for proximity tests, which work perfectly well on
    a branched or partly broken alignment. Chainage needs a single ordered line,
    so it is measured along the longest continuous component -- and the share of
    total length that component covers is reported, rather than assumed.
    """

    def __init__(self, line, stations=None, source="", max_gap_km=MAX_BRIDGE_GAP_KM):
        if line is None or line.is_empty:
            raise ValueError("Corridor geometry has no usable centreline")
        line, self.bridged_gaps_km = bridge_gaps(line, max_gap_km)
        self.line_wgs84 = line
        self.stations = stations or []   # list of {name, lat, lon}
        self.source = source

        parts = list(line.geoms) if line.geom_type == "MultiLineString" else [line]
        self.component_count = len(parts)
        self._primary_wgs84 = max(parts, key=lambda g: g.length)

        centroid = line.centroid
        # Equidistant projection pinned to this corridor's own centre.
        self._crs = CRS.from_proj4(
            f"+proj=aeqd +lat_0={centroid.y} +lon_0={centroid.x} "
            f"+x_0=0 +y_0=0 +datum=WGS84 +units=m +no_defs"
        )
        self._to_metric = Transformer.from_crs("EPSG:4326", self._crs, always_xy=True).transform
        self.line_metric = shapely_transform(self._to_metric, line)
        self.length_km = self.line_metric.length / 1000.0

        # Chainage is measured along this one; a branch or a gap cannot be
        # ordered, so it is deliberately not the whole geometry.
        self.primary_metric = shapely_transform(self._to_metric, self._primary_wgs84)
        self.primary_length_km = self.primary_metric.length / 1000.0
        self.chainage_coverage = (
            self.primary_length_km / self.length_km if self.length_km else 0.0
        )

        self._buffer_km = None
        self._buffer_metric = None

    def reverse_chainage(self):
        """Flip the direction chainage is measured in."""
        self._primary_wgs84 = LineString(list(self._primary_wgs84.coords)[::-1])
        self.primary_metric = LineString(list(self.primary_metric.coords)[::-1])

    def orient_by_reference(self, references):
        """Point chainage the same way as a known set of chainages.

        A drawing carries no notion of which end is zero, so a freshly parsed
        alignment measures from whichever end the CAD happened to start at --
        putting the EWDFC's origin at 1955.7 km instead of 0. Given any station
        list with a Chainage column, the direction that agrees with it is the
        right one.

        `references` is an iterable of (lat, lon, known_chainage_km). Returns
        True if the line was flipped.
        """
        pairs = []
        for lat, lon, known in references:
            if known is None:
                continue
            try:
                pairs.append((self.chainage_km(lat, lon), float(known)))
            except (TypeError, ValueError):
                continue
        if len(pairs) < 2:
            return False

        mean_measured = sum(p[0] for p in pairs) / len(pairs)
        mean_known = sum(p[1] for p in pairs) / len(pairs)
        covariance = sum((m - mean_measured) * (k - mean_known) for m, k in pairs)
        if covariance < 0:
            self.reverse_chainage()
            return True
        return False

    @property
    def chainage_available(self):
        """True when the ordered component covers most of the corridor.

        Below this, a chainage figure would be quietly measured along a
        fragment, so the pipeline falls back to the station list's own Chainage
        column instead of reporting a number that looks authoritative and isn't.
        """
        return self.chainage_coverage >= 0.5

    # -- buffer matching --------------------------------------------------

    def _ensure_buffer(self, buffer_km):
        if self._buffer_km != buffer_km:
            self._buffer_metric = self.line_metric.buffer(buffer_km * 1000.0)
            self._buffer_km = buffer_km
        return self._buffer_metric

    def contains(self, lat, lon, buffer_km):
        """Is this coordinate within buffer_km of the centreline?"""
        buffer_geom = self._ensure_buffer(buffer_km)
        x, y = self._to_metric(lon, lat)
        return buffer_geom.contains(Point(x, y))

    def contains_many(self, coordinates, buffer_km):
        """Vectorised membership test.

        Returns a list of booleans matching the input order. Called once per
        pipeline run over the whole station gazetteer rather than once per
        route, which is the difference between one buffer test and millions.
        """
        buffer_geom = self._ensure_buffer(buffer_km)
        results = []
        for lat, lon in coordinates:
            x, y = self._to_metric(lon, lat)
            results.append(buffer_geom.contains(Point(x, y)))
        return results

    # -- chainage ---------------------------------------------------------

    def chainage_km(self, lat, lon):
        """Distance along the corridor from its start, in km.

        This is what turns a set of touched stations into a length: the corridor
        km a flow actually uses is the span between its entry and exit chainage.
        """
        x, y = self._to_metric(lon, lat)
        return self.primary_metric.project(Point(x, y)) / 1000.0

    def offset_km(self, lat, lon):
        """Perpendicular distance from the corridor, in km."""
        x, y = self._to_metric(lon, lat)
        return self.line_metric.distance(Point(x, y)) / 1000.0


# --- parsing -------------------------------------------------------------

def _kml_text(source):
    """Return KML text from a .kml or .kmz path or bytes."""
    if isinstance(source, (str, Path)):
        path = Path(source)
        data = path.read_bytes()
        suffix = path.suffix.lower()
    else:
        data = source.read() if hasattr(source, "read") else source
        suffix = ".kmz" if data[:2] == b"PK" else ".kml"

    if suffix == ".kmz" or data[:2] == b"PK":
        import io
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            names = [n for n in archive.namelist() if n.lower().endswith(".kml")]
            if not names:
                raise ValueError("KMZ contains no .kml entry")
            # doc.kml is the conventional root when several are present.
            name = next((n for n in names if n.lower().endswith("doc.kml")), names[0])
            data = archive.read(name)
    return data.decode("utf-8", errors="replace")


def _parse_coordinates(text):
    """KML coordinate blobs are 'lon,lat[,alt] lon,lat[,alt] ...'."""
    points = []
    for chunk in text.replace("\n", " ").split():
        parts = chunk.split(",")
        if len(parts) >= 2:
            try:
                points.append((float(parts[0]), float(parts[1])))
            except ValueError:
                continue
    return points


def read_kml_placemarks(source):
    """Walk a KML tree, yielding placemarks tagged with their folder path.

    The folder path matters because CAD exports bury the useful layers among
    thousands of block references and chainage ticks, and only the folder name
    distinguishes them.
    """
    root = safe_fromstring(_kml_text(source))
    placemarks = []

    # Geometry containers whose child <coordinates> we care about. Reading the
    # container tag directly avoids searching for each coordinate node's parent,
    # which on a 97,000-linestring CAD export is the difference between seconds
    # and not finishing.
    line_tags = (f"{KML_NS}LineString", f"{KML_NS}LinearRing")
    point_tag = f"{KML_NS}Point"

    def collect(placemark):
        lines, points = [], []
        for node in placemark.iter():
            if node.tag not in line_tags and node.tag != point_tag:
                continue
            coord_node = node.find(f"{KML_NS}coordinates")
            if coord_node is None:
                continue
            coords = _parse_coordinates(coord_node.text or "")
            if not coords:
                continue
            if node.tag == point_tag:
                points.append(coords[0])
            elif len(coords) >= 2:
                lines.append(coords)
        return lines, points

    def walk(element, folders):
        for child in element:
            tag = child.tag
            if tag in (f"{KML_NS}Folder", f"{KML_NS}Document"):
                name_node = child.find(f"{KML_NS}name")
                name = (name_node.text or "").strip() if name_node is not None else ""
                walk(child, folders + [name] if name else folders)
            elif tag == f"{KML_NS}Placemark":
                name_node = child.find(f"{KML_NS}name")
                name = (name_node.text or "").strip() if name_node is not None else ""
                lines, points = collect(child)
                if lines or points:
                    placemarks.append({
                        "name": name,
                        "folders": list(folders),
                        "lines": lines,
                        "points": points,
                    })
            else:
                walk(child, folders)

    walk(root, [])
    return placemarks


def _matches(text, hints):
    lowered = text.lower()
    return any(hint in lowered for hint in hints)


def layer_key(placemark, depth=None):
    folders = placemark["folders"]
    if depth is not None:
        folders = folders[:depth]
    return " / ".join(folders) or "(root)"


def summarise_layers(placemarks, depth=None):
    """Per-folder counts, so a user can pick the alignment layer in the UI.

    `depth` rolls sub-folders up to a chosen level. A CAD export of this kind
    has ~300 leaf folders but only a handful of meaningful groups, and picking
    from 300 is not a choice anyone can make.
    """
    summary = {}
    for placemark in placemarks:
        entry = summary.setdefault(
            layer_key(placemark, depth), {"lines": 0, "points": 0, "vertices": 0}
        )
        entry["lines"] += len(placemark["lines"])
        entry["points"] += len(placemark["points"])
        entry["vertices"] += sum(len(line) for line in placemark["lines"])
    return summary


def _station_name_from_folders(folders):
    """Recover a station name from the folder path.

    CAD exports name the placemarks after drawing entities ("Block Reference
    [12BE3]") and the station after the folder that holds them, so the folder
    path is where the real name lives.
    """
    for folder in reversed(folders):
        if _matches(folder, STATION_HINTS):
            cleaned = folder
            for suffix in ("Junction Station", "Junction Stations", "Station", "Stations"):
                if cleaned.lower().endswith(suffix.lower()):
                    cleaned = cleaned[: -len(suffix)].strip(" -_")
                    break
            if cleaned:
                return cleaned
    return ""


def extract_stations(placemarks, station_layer=None, depth=None):
    """One representative point per station, not every vertex in its drawing.

    A station folder in a CAD export holds a hundred-odd points describing
    platforms, connectivity lines and chainage ticks. What the pipeline needs is
    a single coordinate per station, so points are grouped by the folder that
    names the station and reduced to their centroid.
    """
    groups = {}
    for placemark in placemarks:
        if not placemark["points"]:
            continue
        folders = placemark["folders"]
        if station_layer and not layer_key(placemark).startswith(station_layer):
            continue
        name = _station_name_from_folders(folders)
        if not name:
            if station_layer is None:
                continue
            name = placemark["name"] or layer_key(placemark, depth)
        groups.setdefault(name, []).extend(placemark["points"])

    stations = []
    for name, coords in groups.items():
        lons = [c[0] for c in coords]
        lats = [c[1] for c in coords]
        stations.append({
            "name": name,
            "lat": sum(lats) / len(lats),
            "lon": sum(lons) / len(lons),
            "point_count": len(coords),
        })
    return sorted(stations, key=lambda s: s["name"])


def build_from_placemarks(placemarks, alignment_layer=None, station_layer=None):
    """Assemble a CorridorGeometry from parsed placemarks.

    With no layer named, the alignment is taken from folders whose name looks
    like an alignment, falling back to the single longest line in the file --
    which is the right answer for a clean two-placemark KML and a reasonable
    one for a CAD export.
    """
    # Prefix match, not equality: a chosen alignment layer normally has the
    # drawing's sub-folders beneath it, and selecting the parent should mean
    # "this branch of the tree".
    def in_layer(placemark, layer):
        return layer is None or layer_key(placemark).startswith(layer)

    line_candidates = []
    for placemark in placemarks:
        if not placemark["lines"] or not in_layer(placemark, alignment_layer):
            continue
        folder_text = " ".join(placemark["folders"]) + " " + placemark["name"]
        preferred = _matches(folder_text, ALIGNMENT_HINTS)
        for coords in placemark["lines"]:
            if len(coords) >= 2:
                line_candidates.append((preferred, LineString(coords)))

    if not line_candidates:
        raise ValueError(
            "No line geometry found in the alignment file. If this is a CAD "
            "export, pick the alignment layer explicitly."
        )

    preferred_lines = [geom for preferred, geom in line_candidates if preferred]
    pool = preferred_lines or [geom for _, geom in line_candidates]

    stations = extract_stations(placemarks, station_layer)
    return CorridorGeometry(stitch_lines(pool), stations=stations, source="kml")


def load_alignment(source, alignment_layer=None, station_layer=None, suffix=None):
    """Read an alignment from KML/KMZ, GeoJSON, or shapefile."""
    if suffix is None and isinstance(source, (str, Path)):
        suffix = Path(source).suffix.lower()
    suffix = (suffix or "").lower()

    if suffix in (".geojson", ".json"):
        raw = source.read() if hasattr(source, "read") else Path(source).read_bytes()
        document = json.loads(raw)
        features = document.get("features", [document])
        lines, stations = [], []
        for feature in features:
            geometry = shape(feature["geometry"])
            properties = feature.get("properties") or {}
            if geometry.geom_type == "LineString":
                lines.append(geometry)
            elif geometry.geom_type == "MultiLineString":
                lines.extend(geometry.geoms)
            elif geometry.geom_type == "Point":
                stations.append({
                    "name": properties.get("name") or properties.get("Name") or "",
                    "lat": geometry.y, "lon": geometry.x,
                })
        if not lines:
            raise ValueError("GeoJSON contains no line geometry")
        return CorridorGeometry(stitch_lines(lines), stations=stations, source="geojson")

    if suffix in (".shp", ".gpkg"):
        try:
            import geopandas as gpd
        except ImportError as exc:
            raise ValueError(
                "Reading shapefiles needs geopandas, which is not installed. "
                "Export the alignment as KML or GeoJSON instead."
            ) from exc
        frame = gpd.read_file(source).to_crs("EPSG:4326")
        lines = [g for g in frame.geometry if g.geom_type in ("LineString", "MultiLineString")]
        if not lines:
            raise ValueError("Shapefile contains no line geometry")
        flat = []
        for geom in lines:
            flat.extend(geom.geoms if geom.geom_type == "MultiLineString" else [geom])
        return CorridorGeometry(stitch_lines(flat), source="shapefile")

    placemarks = read_kml_placemarks(source)
    return build_from_placemarks(placemarks, alignment_layer, station_layer)
