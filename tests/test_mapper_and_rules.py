"""Corridor matching and eligibility rules."""
import pandas as pd
import pytest

from agents.agent_3_mapper import CorridorMapper
from agents.agent_4_diversion import DiversionEngine, NO, UNKNOWN, YES
from core import schema
from core.gazetteer import Gazetteer
from core.projects import EligibilitySettings, RULE_MIN_CORRIDOR_KM, \
    RULE_MIN_CORRIDOR_SHARE, RULE_MIN_STATIONS


def corridor(codes=("B", "C", "D"), chainages=(100.0, 200.0, 350.0)):
    return pd.DataFrame({
        schema.CORRIDOR_CODE: list(codes),
        schema.CHAINAGE: list(chainages),
    })


def test_code_matching_finds_corridor_stations():
    mapper = CorridorMapper(corridor())
    result = mapper.map_route(["A", "B", "C", "E"])

    assert result["overlap"] is True
    assert result["stations_touched"] == ["B", "C"]
    assert result["interaction_count"] == 2
    assert result["entry_station"] == "B"
    assert result["exit_station"] == "C"
    assert result["match_mode"] == "CODE"


def test_route_missing_the_corridor_reports_no_overlap():
    result = CorridorMapper(corridor()).map_route(["X", "Y", "Z"])
    assert result["overlap"] is False
    assert result["interaction_count"] == 0
    assert result["route_origin"] == "X" and result["route_destination"] == "Z"


def test_corridor_km_spans_the_touched_chainages():
    """B at 100 and D at 350 means 250 km of corridor used."""
    result = CorridorMapper(corridor()).map_route(["A", "B", "C", "D", "E"])
    assert result["corridor_km"] == pytest.approx(250.0)


def test_corridor_share_is_a_fraction_of_route_distance():
    result = CorridorMapper(corridor()).map_route(["A", "B", "D"], ir_distance=500.0)
    assert result["corridor_share"] == pytest.approx(0.5)


def test_corridor_share_is_clamped_to_one():
    """Drawing-derived corridor km and portal distance measure the same ground
    differently; the share must not exceed 1."""
    result = CorridorMapper(corridor()).map_route(["B", "D"], ir_distance=10.0)
    assert result["corridor_share"] == 1.0


def test_single_touched_station_uses_zero_corridor_km():
    result = CorridorMapper(corridor()).map_route(["A", "B", "E"])
    assert result["corridor_km"] == 0.0


def test_empty_route_is_handled():
    result = CorridorMapper(corridor()).map_route([])
    assert result["interaction_count"] == 0 and result["overlap"] is False


class FakeGeometry:
    """Everything within 1 degree of latitude 20 counts as on-corridor."""
    chainage_available = False

    def contains(self, lat, lon, buffer_km):
        return abs(lat - 20.0) < 1.0


def test_proximity_matching_finds_stations_absent_from_the_code_list():
    gazetteer = Gazetteer({"P": (20.1, 77.0), "Q": (28.6, 77.2)})
    mapper = CorridorMapper(corridor(), geometry=FakeGeometry(), gazetteer=gazetteer,
                            by_code=True, by_proximity=True)
    result = mapper.map_route(["P", "Q"])

    assert result["stations_touched"] == ["P"]     # P is near, Q is not
    assert result["match_mode"] == "SPATIAL"
    assert mapper.spatial_matches == {"P"}


def test_mixed_matching_is_reported_as_mixed():
    gazetteer = Gazetteer({"P": (20.1, 77.0)})
    mapper = CorridorMapper(corridor(), geometry=FakeGeometry(), gazetteer=gazetteer,
                            by_code=True, by_proximity=True)
    result = mapper.map_route(["B", "P"])
    assert result["match_mode"] == "MIXED"


def test_proximity_is_off_without_a_gazetteer():
    mapper = CorridorMapper(corridor(), geometry=FakeGeometry(), gazetteer=None,
                            by_proximity=True)
    assert mapper.by_proximity is False


def test_station_with_unknown_position_is_not_matched():
    mapper = CorridorMapper(corridor(), geometry=FakeGeometry(), gazetteer=Gazetteer({}),
                            by_proximity=True)
    assert mapper.map_route(["P"])["interaction_count"] == 0


# --- eligibility rules ---------------------------------------------------

def decide(rule, mapping, **kwargs):
    settings = EligibilitySettings(rule=rule, **kwargs)
    return DiversionEngine(settings).decide(mapping)["Eligible"]


def test_min_stations_rule():
    assert decide(RULE_MIN_STATIONS, {"interaction_count": 3}, threshold=3) == YES
    assert decide(RULE_MIN_STATIONS, {"interaction_count": 2}, threshold=3) == NO


def test_min_corridor_km_rule():
    mapping = {"interaction_count": 2, "corridor_km": 120.0}
    assert decide(RULE_MIN_CORRIDOR_KM, mapping, min_corridor_km=100.0) == YES
    assert decide(RULE_MIN_CORRIDOR_KM, mapping, min_corridor_km=200.0) == NO


def test_min_corridor_share_rule():
    mapping = {"interaction_count": 2, "corridor_share": 0.4}
    assert decide(RULE_MIN_CORRIDOR_SHARE, mapping, min_corridor_share=0.25) == YES
    assert decide(RULE_MIN_CORRIDOR_SHARE, mapping, min_corridor_share=0.5) == NO


def test_unevaluable_rule_returns_unknown_not_no():
    """Missing chainage must not read as a negative finding."""
    mapping = {"interaction_count": 2, "corridor_km": None}
    assert decide(RULE_MIN_CORRIDOR_KM, mapping, min_corridor_km=100.0) == UNKNOWN


# --- gazetteer name matching ---------------------------------------------

def test_gazetteer_name_normalisation_bridges_naming_conventions():
    """OSM writes "Bhusaval Junction"; a corridor list writes "New Bhusaval"."""
    from core.gazetteer import Gazetteer
    normalise = Gazetteer.normalise_name
    assert normalise("Bhusaval Junction") == normalise("Bhusaval")
    assert normalise("New Bhusaval") == normalise("Bhusaval")
    assert normalise("Bhusaval Jn") == normalise("bhusaval")
    assert normalise("Anchelli") != normalise("Bhusaval")


def test_gazetteer_looks_up_by_code_or_name():
    from core.gazetteer import Gazetteer
    gazetteer = Gazetteer({"BSL": (21.04, 75.78)}, {"gondia": (21.45, 80.12)})

    assert gazetteer.get("bsl ") == (21.04, 75.78)          # trimmed, uppercased
    assert gazetteer.get_by_name("Gondia Junction") == (21.45, 80.12)
    assert gazetteer.get("NOPE") is None
    assert gazetteer.get_by_name("Nowhere") is None


def test_locate_prefers_the_code_match():
    from core.gazetteer import Gazetteer
    gazetteer = Gazetteer({"BSL": (1.0, 1.0)}, {"bhusaval": (2.0, 2.0)})
    assert gazetteer.locate(code="BSL", name="Bhusaval") == (1.0, 1.0)
    assert gazetteer.locate(code="UNKNOWN", name="Bhusaval") == (2.0, 2.0)


def test_overlay_takes_precedence_over_the_bundled_data():
    from core.gazetteer import Gazetteer
    base = Gazetteer({"BSL": (1.0, 1.0)})
    overlaid = base.overlay({"BSL": (9.0, 9.0)})
    assert overlaid.get("BSL") == (9.0, 9.0)
    assert base.get("BSL") == (1.0, 1.0)      # original untouched


def test_station_frame_extraction_rejects_impossible_coordinates():
    from core.gazetteer import from_station_frame
    frame = pd.DataFrame({
        schema.CORRIDOR_CODE: ["A", "B"],
        schema.CORRIDOR_NAME: ["Alpha", "Beta"],
        schema.LATITUDE: [21.0, 999.0],
        schema.LONGITUDE: [77.0, 77.0],
    })
    by_code, by_name = from_station_frame(
        frame, schema.CORRIDOR_CODE, schema.LATITUDE, schema.LONGITUDE,
        name_column=schema.CORRIDOR_NAME)
    assert set(by_code) == {"A"}
    assert "alpha" in by_name
