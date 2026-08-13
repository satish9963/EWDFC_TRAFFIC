import pandas as pd

from agents.agent_6_aggregator import TrafficAggregator

DFC_STATIONS = pd.DataFrame({
    "DFC Station Code": ["AAA", "BBB", "CCC"],
    "Station Name": ["Alpha", "Bravo", "Charlie"],
})


def _master_row(**overrides):
    """One master-OD row entering at AAA, passing BBB, exiting at CCC."""
    row = {
        "Annual Tonnage": 1000,
        "No. of Rakes / Wagon Units": 10,
        "Entry DFC": "AAA",
        "Exit DFC": "CCC",
        "DFC stations touched": "AAA, BBB, CCC",
    }
    row.update(overrides)
    return row


def test_counts_through_traffic_at_intermediate_station():
    """BBB is touched but is neither entry nor exit, so it carries through traffic."""
    master = pd.DataFrame([_master_row()])

    summary = TrafficAggregator(DFC_STATIONS).aggregate(master)
    bbb = summary.set_index("DFC Station Code").loc["BBB"]

    assert bbb["through_tonnage"] == 1000
    assert bbb["through_rakes"] == 10
    assert bbb["through_od_count"] == 1


def test_entry_and_exit_stations_are_not_counted_as_through():
    master = pd.DataFrame([_master_row()])

    summary = TrafficAggregator(DFC_STATIONS).aggregate(master).set_index("DFC Station Code")

    assert summary.loc["AAA"]["entering_tonnage"] == 1000
    assert summary.loc["AAA"]["through_tonnage"] == 0
    assert summary.loc["CCC"]["exiting_tonnage"] == 1000
    assert summary.loc["CCC"]["through_tonnage"] == 0


def test_total_tonnage_includes_through_traffic():
    master = pd.DataFrame([_master_row()])

    summary = TrafficAggregator(DFC_STATIONS).aggregate(master).set_index("DFC Station Code")

    assert summary.loc["BBB"]["total_tonnage"] == 1000
