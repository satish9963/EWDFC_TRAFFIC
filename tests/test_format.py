import pandas as pd

from ui.format import display_columns, format_number, format_percent


def test_format_number_groups_thousands():
    assert format_number(1234567) == "1,234,567"


def test_format_number_keeps_requested_decimals():
    assert format_number(1234.567, decimals=1) == "1,234.6"


def test_format_number_shows_dash_for_missing_value():
    assert format_number(None) == "—"


def test_format_percent_of_total():
    assert format_percent(25, 100) == "25.0%"


def test_format_percent_does_not_divide_by_zero():
    assert format_percent(5, 0) == "—"


def test_display_columns_titleises_snake_case():
    df = pd.DataFrame(columns=["DFC Station Code", "entering_tonnage", "through_od_count"])

    renamed = list(display_columns(df).columns)

    assert renamed == ["DFC Station Code", "Entering Tonnage", "Through OD Count"]
