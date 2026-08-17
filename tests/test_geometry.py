"""Alignment geometry: stitching, gap bridging, chainage and orientation.

The numbers here are small and synthetic, but each case is one that the real
21 MB EWDFC CAD export actually exhibited.
"""
import math

import pytest
from shapely.geometry import LineString

from core.geometry import (CorridorGeometry, bridge_gaps, build_from_placemarks,
                           extract_stations, layer_key, stitch_lines, summarise_layers)

# One degree of latitude is ~110.57 km; these fixtures run north along a
# meridian so expected lengths are easy to reason about.
KM_PER_DEG_LAT = 110.57


def straight(lat0, lat1, lon=77.0, steps=10):
    return LineString([(lon, lat0 + (lat1 - lat0) * i / steps) for i in range(steps + 1)])


# --- stitching -----------------------------------------------------------

def test_stitch_joins_segments_that_share_endpoints():
    parts = [straight(20.0, 20.5), straight(20.5, 21.0)]
    merged = stitch_lines(parts)
    assert merged.geom_type == "LineString"


def test_stitch_absorbs_float_noise_at_shared_endpoints():
    """The CAD case: endpoints differ past the seventh decimal."""
    a = LineString([(77.0, 20.0), (77.0, 20.5)])
    b = LineString([(77.00000001, 20.50000001), (77.0, 21.0)])

    # Without snapping these are two disjoint lines.
    from shapely.ops import linemerge
    from shapely.geometry import MultiLineString
    assert linemerge(MultiLineString([a, b])).geom_type == "MultiLineString"

    assert stitch_lines([a, b]).geom_type == "LineString"


def test_stitch_keeps_genuinely_separate_lines_separate():
    far = [straight(20.0, 20.5), straight(25.0, 25.5)]
    assert stitch_lines(far).geom_type == "MultiLineString"


def test_stitch_rejects_empty_input():
    with pytest.raises(ValueError):
        stitch_lines([])


# --- gap bridging --------------------------------------------------------

def test_bridge_joins_sections_separated_by_a_small_gap():
    """The three EWDFC sections abut with 575 m and 687 m gaps."""
    a = LineString([(77.0, 20.0), (77.0, 20.5)])
    b = LineString([(77.0, 20.505), (77.0, 21.0)])   # ~0.55 km apart
    merged, bridges = bridge_gaps(a.union(b))

    assert merged.geom_type == "LineString"
    assert len(bridges) == 1
    assert bridges[0] == pytest.approx(0.55, abs=0.1)


def test_bridge_leaves_large_gaps_alone():
    """A big gap is a real break or a branch, not a drawing artefact."""
    a = LineString([(77.0, 20.0), (77.0, 20.5)])
    b = LineString([(77.0, 22.0), (77.0, 22.5)])     # ~166 km apart
    merged, bridges = bridge_gaps(a.union(b))

    assert merged.geom_type == "MultiLineString"
    assert bridges == []


def test_bridged_gaps_are_reported_on_the_geometry():
    a = LineString([(77.0, 20.0), (77.0, 20.5)])
    b = LineString([(77.0, 20.505), (77.0, 21.0)])
    geometry = CorridorGeometry(a.union(b))
    assert len(geometry.bridged_gaps_km) == 1


# --- length, buffering, chainage -----------------------------------------

def test_length_is_measured_in_metres_not_degrees():
    geometry = CorridorGeometry(straight(20.0, 21.0))
    assert geometry.length_km == pytest.approx(KM_PER_DEG_LAT, rel=0.01)


def test_buffer_membership():
    geometry = CorridorGeometry(straight(20.0, 21.0, lon=77.0))
    assert geometry.contains(20.5, 77.0, buffer_km=5) is True
    assert geometry.contains(20.5, 77.02, buffer_km=5) is True     # ~2 km east
    assert geometry.contains(20.5, 78.0, buffer_km=5) is False     # ~104 km east


def test_offset_reports_distance_from_the_corridor():
    geometry = CorridorGeometry(straight(20.0, 21.0, lon=77.0))
    assert geometry.offset_km(20.5, 77.0) == pytest.approx(0.0, abs=0.01)
    # A degree of longitude at 20.5N is ~104 km.
    assert geometry.offset_km(20.5, 78.0) == pytest.approx(104, rel=0.05)


def test_chainage_runs_from_the_start_of_the_line():
    geometry = CorridorGeometry(straight(20.0, 21.0))
    assert geometry.chainage_km(20.0, 77.0) == pytest.approx(0.0, abs=0.5)
    assert geometry.chainage_km(21.0, 77.0) == pytest.approx(KM_PER_DEG_LAT, rel=0.01)


def test_chainage_is_unavailable_when_the_alignment_is_fragmented():
    """Below 50% coverage a chainage figure would be measured along a fragment.

    Three equal, widely separated pieces: no single one carries half the
    corridor, so none of them can be called its chainage line.
    """
    pieces = [straight(20.0, 21.0, lon=77.0),
              straight(25.0, 26.0, lon=80.0),
              straight(30.0, 31.0, lon=83.0)]
    combined = pieces[0].union(pieces[1]).union(pieces[2])
    geometry = CorridorGeometry(combined)

    assert geometry.component_count == 3
    assert geometry.chainage_coverage == pytest.approx(1 / 3, abs=0.05)
    assert geometry.chainage_available is False


def test_chainage_available_when_one_component_dominates():
    geometry = CorridorGeometry(straight(20.0, 21.0))
    assert geometry.chainage_available is True


# --- orientation ---------------------------------------------------------

def test_orientation_flips_to_match_known_chainages():
    """A drawing carries no notion of which end is zero."""
    geometry = CorridorGeometry(straight(20.0, 21.0))
    # Reference says the NORTH end is chainage 0 -- opposite to the line's order.
    references = [(21.0, 77.0, 0.0), (20.0, 77.0, KM_PER_DEG_LAT)]

    assert geometry.orient_by_reference(references) is True
    assert geometry.chainage_km(21.0, 77.0) == pytest.approx(0.0, abs=1.0)


def test_orientation_left_alone_when_it_already_agrees():
    geometry = CorridorGeometry(straight(20.0, 21.0))
    references = [(20.0, 77.0, 0.0), (21.0, 77.0, KM_PER_DEG_LAT)]
    assert geometry.orient_by_reference(references) is False


def test_orientation_survives_a_single_wild_reference():
    """A spur station carries its own chainage origin and must not flip the line.

    New Andal reads 200.0 on the spur and 1761.4 on the main line; a
    sign-of-covariance test tolerates that where a fit would not.
    """
    geometry = CorridorGeometry(straight(20.0, 21.0))
    references = [
        (20.0, 77.0, 0.0), (20.25, 77.0, 27.0),
        (20.5, 77.0, 55.0), (20.75, 77.0, 83.0),
        (21.0, 77.0, 110.0),
        (20.6, 77.0, 5000.0),      # the outlier
    ]
    assert geometry.orient_by_reference(references) is False


def test_orientation_needs_at_least_two_references():
    geometry = CorridorGeometry(straight(20.0, 21.0))
    assert geometry.orient_by_reference([(20.0, 77.0, 0.0)]) is False


# --- placemark handling --------------------------------------------------

def placemark(name, folders, lines=None, points=None):
    return {"name": name, "folders": folders, "lines": lines or [], "points": points or []}


def test_stations_are_named_from_their_folder():
    """CAD placemarks are named after drawing entities, not stations."""
    placemarks = [
        placemark("Block Reference [12BE3]:0",
                  ["EWDFCC", "Junction Stations", "Gondia Junction Station"],
                  points=[(80.12, 21.45), (80.13, 21.46)]),
    ]
    stations = extract_stations(placemarks)
    assert len(stations) == 1
    assert stations[0]["name"] == "Gondia"
    assert stations[0]["point_count"] == 2
    # Reduced to one representative point, not every vertex.
    assert stations[0]["lon"] == pytest.approx(80.125)


def test_points_outside_station_folders_are_ignored():
    """Chainage ticks ("CH: 0+100") are points too, and are not stations."""
    placemarks = [placemark("CH: 0+100", ["EWDFCC", "Alignment"], points=[(77.0, 20.0)])]
    assert extract_stations(placemarks) == []


def test_layer_summary_rolls_up_to_a_depth():
    placemarks = [
        placemark("a", ["Root", "Align", "Part1"], lines=[[(77, 20), (77, 21)]]),
        placemark("b", ["Root", "Align", "Part2"], lines=[[(77, 21), (77, 22)]]),
    ]
    assert set(summarise_layers(placemarks, depth=2)) == {"Root / Align"}
    assert len(summarise_layers(placemarks)) == 2


def test_alignment_layer_selection_matches_by_prefix():
    """Selecting a parent layer must pick up its sub-folders."""
    placemarks = [
        placemark("a", ["Root", "Align_0to50"], lines=[[(77, 20), (77, 20.5)]]),
        placemark("b", ["Root", "Align_50to100"], lines=[[(77, 20.5), (77, 21)]]),
        placemark("c", ["Root", "IR Network"], lines=[[(80, 25), (80, 26)]]),
    ]
    geometry = build_from_placemarks(placemarks, alignment_layer="Root / Align_")
    # The far-away IR Network line is excluded, so this is one continuous line.
    assert geometry.length_km == pytest.approx(KM_PER_DEG_LAT, rel=0.02)


def test_build_raises_when_no_lines_are_present():
    with pytest.raises(ValueError, match="No line geometry"):
        build_from_placemarks([placemark("p", ["Root"], points=[(77.0, 20.0)])])


def test_layer_key_handles_root_placemarks():
    assert layer_key(placemark("x", [])) == "(root)"
