"""Station search and pair parsing for tools/rbs_lookup.py.

The ranking test is the important one. The first version of the search stopped
scanning once it had collected enough substring hits, so `--station GAR` filled
up on "...nagar" and never reached the station whose code is literally GAR --
the one row the user was after was the one row they could not see.
"""
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools import rbs_lookup  # noqa: E402


GAZETTEER = """code,name,lat,lon,junction,source
A01,New Ashok Nagar,28.591321,77.30252,,osm
AGP,Agarpara,22.6829,88.3854,,osm
AKN,Akbarnagar,25.2353,86.8353,,osm
GARA,Garha,26.4580,85.5117,,osm
GAR,Gadarwara,22.9004,78.7927,,osm
PUNE,Pune Junction,18.5289,73.8744,,osm
,Bhusaval Junction,21.0465,75.7887,,esri
"""


@pytest.fixture
def gazetteer(tmp_path, monkeypatch):
    path = tmp_path / "stations.csv"
    path.write_text(GAZETTEER, encoding="utf-8")
    monkeypatch.setenv("RAIL_GAZETTEER", str(path))
    return path


def test_exact_code_wins_even_when_many_names_contain_it(gazetteer):
    """`GAR` must return Gadarwara, not a page of "...nagar" stations."""
    rows, total, top_rank = rbs_lookup.find_stations("GAR")
    assert rows[0]["code"] == "GAR"
    assert top_rank == 0
    assert total > 1                       # the name matches are still offered


def test_exact_code_is_found_however_far_down_the_file_it_sits(gazetteer):
    """The bug this pins: an early exit hid rows past the first N matches."""
    rows, _total, _rank = rbs_lookup.find_stations("GAR", limit=1)
    assert [r["code"] for r in rows] == ["GAR"]


def test_a_word_starting_with_the_query_beats_one_containing_it(gazetteer):
    rows, _total, _rank = rbs_lookup.find_stations("garh")
    assert rows[0]["name"] == "Garha"


def test_exact_name_ranks_above_partial_matches(gazetteer):
    rows, _total, top_rank = rbs_lookup.find_stations("bhusaval")
    assert rows[0]["name"] == "Bhusaval Junction"     # normalises to "bhusaval"
    assert top_rank == 1


def test_a_query_matching_nothing_returns_nothing(gazetteer):
    rows, total, top_rank = rbs_lookup.find_stations("zzzznotastation")
    assert (rows, total, top_rank) == ([], 0, None)


def test_missing_gazetteer_is_reported_not_raised(tmp_path, monkeypatch):
    monkeypatch.setenv("RAIL_GAZETTEER", str(tmp_path / "absent.csv"))
    assert rbs_lookup.find_stations("GAR") == ([], 0, None)


def test_code_shaped_query_is_distinguished_from_a_name(gazetteer):
    """Drives the "no station carries this code" note."""
    assert rbs_lookup._looks_like_code("NGP") is True
    assert rbs_lookup._looks_like_code("new delhi") is False


# --- pair parsing ---------------------------------------------------------

class Args:
    """Stand-in for the argparse namespace."""

    def __init__(self, pair=None, pairs=None, from_excel=None):
        self.pair = pair or []
        self.pairs = pairs
        self.from_excel = from_excel


@pytest.mark.parametrize("text,expected", [
    ("NGP-BPL", [("NGP", "BPL")]),
    ("NGP-BPL, HWH-NDLS", [("NGP", "BPL"), ("HWH", "NDLS")]),
    ("ngp to bpl", [("NGP", "BPL")]),
    ("NGP BPL", [("NGP", "BPL")]),
    ("NGP>BPL", [("NGP", "BPL")]),
    ("NGP:BPL", [("NGP", "BPL")]),
])
def test_pairs_are_read_from_any_of_the_accepted_separators(text, expected):
    assert rbs_lookup.parse_pairs(Args(pairs=text)) == expected


def test_a_pair_with_no_separator_is_reported_not_silently_dropped(capsys):
    """One typo in a long --pairs string would otherwise just shorten the run."""
    assert rbs_lookup.parse_pairs(Args(pairs="NGP-BPL, NOSEPARATOR")) == [("NGP", "BPL")]
    assert "NOSEPARATOR" in capsys.readouterr().out


def test_repeated_pairs_are_looked_up_once(capsys):
    """An OD workbook repeats a pair per commodity; the route is the same."""
    pairs = rbs_lookup.parse_pairs(Args(pairs="NGP-BPL, ngp-bpl, NGP - BPL"))
    assert pairs == [("NGP", "BPL")]
