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


# --- unresolved pairs export ---------------------------------------------

def test_unresolved_pairs_lists_only_what_has_no_route():
    """A hosted run cannot reach RBS, so it must say which pairs to fetch.

    NO_ROUTE is an answer from the portal and must not appear -- refetching it
    changes nothing. ERROR and UNKNOWN mean the portal was never heard from.
    """
    import pandas as pd

    from core import schema
    from ui.results import _unresolved_pairs

    master = pd.DataFrame({
        schema.FROM_CODE: ["A", "B", "C", "D", "A"],
        schema.TO_CODE: ["X", "Y", "Z", "W", "X"],
        schema.ROUTE_SOURCE: ["ERROR", "UNKNOWN", "NO_ROUTE", "CACHE", "ERROR"],
    })
    missing = _unresolved_pairs(master)

    assert list(missing[schema.FROM_CODE]) == ["A", "B"]   # duplicate A->X collapsed
    assert list(missing[schema.TO_CODE]) == ["X", "Y"]


def test_unresolved_pairs_is_none_when_everything_resolved():
    import pandas as pd

    from core import schema
    from ui.results import _unresolved_pairs

    master = pd.DataFrame({
        schema.FROM_CODE: ["A"], schema.TO_CODE: ["X"],
        schema.ROUTE_SOURCE: ["CACHE"],
    })
    assert _unresolved_pairs(master) is None


def test_unresolved_pairs_export_is_readable_by_the_fetcher():
    """The download must be usable without editing: same headers the app reads."""
    import pandas as pd

    from agents.agent_1_validator import InputValidator
    from core import schema
    from ui.results import _unresolved_pairs

    master = pd.DataFrame({
        schema.FROM_CODE: ["NGN"], schema.TO_CODE: ["AKV"],
        schema.ROUTE_SOURCE: ["ERROR"],
    })
    clean, _ = InputValidator().validate_od_data(_unresolved_pairs(master))
    assert clean[schema.FROM_CODE].tolist() == ["NGN"]
    assert clean[schema.TO_CODE].tolist() == ["AKV"]
