"""Header aliasing: the thing that lets any project's workbook be read."""
import pandas as pd
import pytest

from agents.agent_1_validator import InputValidator
from core import schema


@pytest.mark.parametrize("header,expected", [
    ("FROMSTTN", schema.FROM_CODE),
    ("from_sttn", schema.FROM_CODE),
    ("From Station Code", schema.FROM_CODE),
    ("Origin Code", schema.FROM_CODE),
    ("TOSTTN", schema.TO_CODE),
    ("Destination Station Code", schema.TO_CODE),
    ("No.of Rakes/No. of Units", schema.UNITS),
    ("Annual Tonnage", schema.TONNAGE),
    ("MTPA", schema.TONNAGE),
    ("DFC Station Code", schema.CORRIDOR_CODE),
    ("Station Code", schema.CORRIDOR_CODE),
    ("Center Chainage", schema.CHAINAGE),
    ("Lat", schema.LATITUDE),
    ("lng", schema.LONGITUDE),
])
def test_aliases_resolve(header, expected):
    assert schema.resolve_columns([header]).get(header, expected) == expected


def test_normalise_ignores_case_and_punctuation():
    assert schema.normalise("From-Station_Code ") == schema.normalise("fromstationcode")


def test_unknown_columns_are_left_alone():
    """A client's extra columns must survive into the output."""
    mapping = schema.resolve_columns(["FROMSTTN", "Zone", "Division", "Contract Ref"])
    assert "Zone" not in mapping and "Division" not in mapping


def test_duplicate_aliases_do_not_collide():
    """Two headers claiming one canonical name: the first wins, no crash."""
    mapping = schema.resolve_columns(["FROMSTTN", "Origin Code"])
    assert list(mapping.values()).count(schema.FROM_CODE) == 1


def test_validator_accepts_ir_style_export():
    df = pd.DataFrame({
        "FROMSTTN": ["abc ", "DEF"],
        "TOSTTN": ["xyz", "uvw"],
        "No.of Rakes/No. of Units": ["1,200", "34"],
    })
    clean, report = InputValidator().validate_od_data(df)

    assert list(clean[schema.FROM_CODE]) == ["ABC", "DEF"]   # trimmed and uppercased
    assert clean[schema.UNITS].tolist() == [1200, 34]         # thousands separator handled
    assert schema.COMMODITY in clean.columns                  # filled, not demanded
    assert schema.TONNAGE in report.filled_columns


def test_validator_promotes_a_header_row_below_a_title():
    """Workbooks that open with a merged banner row."""
    df = pd.DataFrame({
        "Base Year (2024-25)": ["FROMSTTN", "ABC"],
        "Unnamed: 1": ["TOSTTN", "XYZ"],
        "Unnamed: 2": ["Annual Tonnage", 500],
    })
    clean, report = InputValidator().validate_od_data(df)
    assert list(clean[schema.FROM_CODE]) == ["ABC"]
    assert clean[schema.TONNAGE].tolist() == [500]
    assert any("promoted" in note for note in report.notes)


def test_validator_rejects_a_workbook_with_no_codes():
    df = pd.DataFrame({"Something": [1], "Else": [2]})
    with pytest.raises(ValueError, match="missing"):
        InputValidator().validate_od_data(df)


def test_corridor_list_needs_only_a_code():
    clean, report = InputValidator().validate_corridor_stations(
        pd.DataFrame({"Station Code": ["a", "B", "b"]})
    )
    assert list(clean[schema.CORRIDOR_CODE]) == ["A", "B"]   # deduplicated, uppercased
    assert report.dropped_duplicates == 1
    assert any("chainage" in note.lower() for note in report.notes)


def test_missing_tonnage_is_reported_not_silently_zeroed():
    """A defaulted tonnage column must be announced.

    Zero is a plausible-looking number. This project has already lost weeks to a
    silent zero, so a tonnage column that was invented rather than found has to
    show up in the validation report and, from there, the exception log.
    """
    df = pd.DataFrame({"FROMSTTN": ["ABC"], "TOSTTN": ["XYZ"]})
    clean, report = InputValidator().validate_od_data(df)

    assert clean[schema.TONNAGE].tolist() == [0]
    assert schema.TONNAGE in report.filled_columns
    assert schema.UNITS in report.filled_columns


def test_present_tonnage_is_not_reported_as_filled():
    df = pd.DataFrame({"FROMSTTN": ["ABC"], "TOSTTN": ["XYZ"], "Annual Tonnage": [1234]})
    clean, report = InputValidator().validate_od_data(df)

    assert clean[schema.TONNAGE].tolist() == [1234]
    assert schema.TONNAGE not in report.filled_columns


def test_phantom_empty_columns_are_dropped():
    """Client workbooks arrive with thousands of empty formatted columns."""
    from agents.agent_1_validator import drop_empty_columns
    df = pd.DataFrame({"FROMSTTN": ["ABC"], "TOSTTN": ["XYZ"]})
    for i in range(50):
        df[f"Unnamed: {i + 2}"] = None
    assert len(drop_empty_columns(df).columns) == 2
