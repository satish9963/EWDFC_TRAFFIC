"""Aggregator tests.

The through-traffic assertion is the important one. A column-name mismatch
between the orchestrator and the aggregator once made every through_* total
zero, and it went unnoticed because zero is a plausible-looking number. If that
regresses, this fails with `assert 0 == 1000`.
"""
import pandas as pd
import pytest

from agents.agent_6_aggregator import TrafficAggregator
from core import schema


def corridor(codes=("A", "B", "C", "D")):
    return pd.DataFrame({
        schema.CORRIDOR_CODE: list(codes),
        schema.CORRIDOR_NAME: [f"Station {c}" for c in codes],
        schema.CHAINAGE: [float(i * 100) for i in range(len(codes))],
    })


def od_row(entry, exit_, touched, tonnage=1000, units=10):
    return {
        schema.TONNAGE: tonnage,
        schema.UNITS: units,
        schema.ENTRY_STATION: entry,
        schema.EXIT_STATION: exit_,
        schema.STATIONS_TOUCHED: touched,
    }


def test_through_traffic_is_counted():
    """A flow passing A -> B -> C credits B with through traffic."""
    df = pd.DataFrame([od_row("A", "C", "A, B, C")])
    summary = TrafficAggregator(corridor()).aggregate(df)
    by_station = summary.set_index(schema.CORRIDOR_CODE)

    assert by_station.loc["B", schema.THROUGH_TONNAGE] == 1000
    assert by_station.loc["B", schema.THROUGH_OD_COUNT] == 1
    # ...and the endpoints are entry/exit, not through.
    assert by_station.loc["A", schema.ENTERING_TONNAGE] == 1000
    assert by_station.loc["A", schema.THROUGH_TONNAGE] == 0
    assert by_station.loc["C", schema.EXITING_TONNAGE] == 1000
    assert by_station.loc["C", schema.THROUGH_TONNAGE] == 0


def test_touched_accepts_a_list_as_well_as_a_string():
    """In-process the column holds a list; read back from Excel it is a string."""
    as_string = TrafficAggregator(corridor()).aggregate(
        pd.DataFrame([od_row("A", "C", "A, B, C")]))
    as_list = TrafficAggregator(corridor()).aggregate(
        pd.DataFrame([od_row("A", "C", ["A", "B", "C"])]))
    pd.testing.assert_frame_equal(as_string, as_list)


def test_totals_are_the_sum_of_movements():
    df = pd.DataFrame([od_row("A", "D", "A, B, C, D"), od_row("B", "C", "B, C", tonnage=500)])
    summary = TrafficAggregator(corridor()).aggregate(df)
    for _, row in summary.iterrows():
        assert row[schema.TOTAL_TONNAGE] == (
            row[schema.ENTERING_TONNAGE] + row[schema.EXITING_TONNAGE]
            + row[schema.THROUGH_TONNAGE]
        )


def test_entry_equal_to_exit_is_not_double_counted():
    """A flow touching one corridor station enters there and does not also exit."""
    df = pd.DataFrame([od_row("A", "A", "A")])
    summary = TrafficAggregator(corridor()).aggregate(df).set_index(schema.CORRIDOR_CODE)
    assert summary.loc["A", schema.ENTERING_TONNAGE] == 1000
    assert summary.loc["A", schema.EXITING_TONNAGE] == 0
    assert summary.loc["A", schema.TOTAL_TONNAGE] == 1000


def test_stations_off_the_route_stay_zero():
    df = pd.DataFrame([od_row("A", "B", "A, B")])
    summary = TrafficAggregator(corridor()).aggregate(df).set_index(schema.CORRIDOR_CODE)
    assert summary.loc["D", schema.TOTAL_TONNAGE] == 0


def test_output_is_ordered_by_chainage_when_available():
    df = pd.DataFrame([od_row("A", "D", "A, B, C, D")])
    summary = TrafficAggregator(corridor()).aggregate(df)
    assert list(summary[schema.CORRIDOR_CODE]) == ["A", "B", "C", "D"]


def test_empty_input_returns_empty_frame():
    assert TrafficAggregator(corridor()).aggregate(pd.DataFrame()).empty


def test_unknown_touched_station_is_ignored():
    """A route may touch stations that are not on this corridor."""
    df = pd.DataFrame([od_row("A", "C", "A, ZZ, B, C")])
    summary = TrafficAggregator(corridor()).aggregate(df).set_index(schema.CORRIDOR_CODE)
    assert summary.loc["B", schema.THROUGH_TONNAGE] == 1000
    assert "ZZ" not in summary.index
