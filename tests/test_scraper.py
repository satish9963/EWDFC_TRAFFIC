"""Scraper: code normalisation, parsing tolerance, and failure classification.

Every case here is one that cost real debugging time. None of them hit the
network -- the HTML fixtures below are the shapes the portal actually returns.
"""
import pandas as pd
import pytest

from agents.agent_1_validator import InputValidator
from agents.agent_2_rbs_scraper import RBSScraper, norm_code
from core import schema


# --- station code normalisation ------------------------------------------

@pytest.mark.parametrize("raw,expected", [
    ("bsl", "BSL"),
    ("  BSL  ", "BSL"),
    ("1234.0", "1234"),      # Excel float64 coercion: the silent cache-miss bug
    ("1234", "1234"),
    ("12.5", "12.5"),        # not a trailing ".0", leave alone
    ("ABC.0", "ABC.0"),      # only strip ".0" when the stem is numeric
])
def test_norm_code(raw, expected):
    assert norm_code(raw) == expected


def test_norm_code_is_the_same_function_the_schema_exports():
    """One definition, or the two sides drift apart silently."""
    assert norm_code is schema.normalise_station_code


def test_validator_applies_the_same_normalisation():
    """A blank cell makes pandas read the whole column as float64."""
    df = pd.DataFrame({
        "FROMSTTN": [1234, None, 5678],
        "TOSTTN": ["BSL", "BSL", "BSL"],
    })
    clean, _ = InputValidator().validate_od_data(df)
    # Without normalisation these would be "1234.0" and "5678.0".
    assert set(clean[schema.FROM_CODE]) == {"1234", "5678"}


# --- parsing --------------------------------------------------------------

POSITIONAL_HTML = """
<table>
  <tr><td>1</td><td>BSL</td><td>Bhusaval Jn</td><td>0</td></tr>
  <tr><td>2</td><td>JL</td><td>Jalgaon</td><td>24.5</td></tr>
  <tr><td>3</td><td>NGP</td><td>Nagpur</td><td>1,234.5</td></tr>
</table>
"""


def test_parses_the_positional_layout():
    distance, codes, junctions = RBSScraper._parse(POSITIONAL_HTML)
    assert codes == ["BSL", "JL", "NGP"]
    # Thousands separator tolerated; the old int-only check rejected it.
    assert distance == pytest.approx(1234.5)
    assert junctions == []


def test_distance_is_the_maximum_not_the_last_cell():
    """Cumulative distance can be out of order or blank on the final row."""
    html = """
    <table>
      <tr><td>1</td><td>AAA</td><td>A</td><td>100</td></tr>
      <tr><td>2</td><td>BBB</td><td>B</td><td>500</td></tr>
      <tr><td>3</td><td>CCC</td><td>C</td><td></td></tr>
    </table>
    """
    distance, codes, _ = RBSScraper._parse(html)
    assert codes == ["AAA", "BBB", "CCC"]
    assert distance == 500


def test_junction_detection_uses_the_code_rule():
    html = """
    <table>
      <tr><td>1</td><td>ADRAJN</td><td>Adra Junction</td><td>10</td></tr>
      <tr><td>2</td><td>BSL</td><td>Bhusaval</td><td>20</td></tr>
    </table>
    """
    _distance, _codes, junctions = RBSScraper._parse(html)
    assert junctions == ["ADRAJN"]


def test_rejects_cells_that_are_not_station_codes():
    """Row numbers, names and blanks must not be read as codes."""
    html = """
    <table>
      <tr><td>1</td><td>Bhusaval Junction</td><td>x</td><td>10</td></tr>
      <tr><td>2</td><td>TOOLONGCODE</td><td>x</td><td>20</td></tr>
      <tr><td>3</td><td></td><td>x</td><td>30</td></tr>
    </table>
    """
    _distance, codes, _ = RBSScraper._parse(html)
    assert codes == []


def test_header_aware_fallback_when_the_layout_moves():
    """Only a fallback: positional is the layout proven against this servlet.

    The names here contain spaces, so the positional pass finds no valid code
    and falls through. That matters -- see the test below for what happens when
    a moved layout still yields code-shaped text positionally.
    """
    html = """
    <table>
      <tr><th>Sr</th><th>Station Name</th><th>Station Code</th><th>Cumulative Distance (km)</th></tr>
      <tr><td>1</td><td>Bhusaval Junction</td><td>BSL</td><td>0</td></tr>
      <tr><td>2</td><td>Jalgaon Town</td><td>JL</td><td>24.5</td></tr>
    </table>
    """
    distance, codes, _ = RBSScraper._parse(html)
    assert codes == ["BSL", "JL"]
    assert distance == pytest.approx(24.5)


def test_positional_pass_wins_even_when_it_is_wrong():
    """Documents a known limitation rather than asserting desired behaviour.

    A single-word station name of 2-8 characters is indistinguishable from a
    station code, so if the servlet ever swaps the name and code columns the
    positional pass succeeds with names and the header-aware fallback never
    runs. It is accepted because positional is the layout this servlet actually
    uses; if that ever changes, this test fails and says why.
    """
    html = """
    <table>
      <tr><th>Sr</th><th>Station Name</th><th>Station Code</th><th>Distance</th></tr>
      <tr><td>1</td><td>Bhusaval</td><td>BSL</td><td>0</td></tr>
    </table>
    """
    _distance, codes, _ = RBSScraper._parse(html)
    assert codes == ["BHUSAVAL"], "positional pass no longer wins -- re-check _parse order"


def test_empty_response_parses_to_nothing_rather_than_raising():
    assert RBSScraper._parse("<html><body>No route found</body></html>") == (0.0, [], [])


# The real servlet markup for a station that has sidings: the code cell holds
# the code *and* a link listing them. Taken from a live BBTR -> BIA response.
SIDINGS_HTML = """
<TR class=" serving-stn ">
  <TD width="29"><A href="javascript:getInterStation(1,'NGP','CR')">+</A></TD>
  <TD align="center">
    GDYA
    <a href="javascript:viewServingStation('[MPBG, THSG]')">Sidings: [MPBG, THSG]</a>
  </TD>
  <TD align="center"><b>  Ghoradongri</b></TD>
  <TD align="center">11.86</TD>
</TR>
<TR>
  <TD width="29"></TD><TD align="center">BZU</TD>
  <TD align="center"><b>Betul</b></TD><TD align="center">24.5</TD>
</TR>
<TR>
  <TD width="29"></TD><TD align="center">G</TD>
  <TD align="center"><b>stray one-letter cell</b></TD><TD align="center">30</TD>
</TR>
<TR class=" serving-stn ">
  <TD width="29"><A href="javascript:getInterStation(3,'NGP','CR')">+</A></TD>
  <TD align="center">
    BIA
    <a href="javascript:viewServingStation('[BSPC, JCGA]')">Sidings: [BSPC, JCGA]</a>
  </TD>
  <TD align="center"><b>Bhilai</b></TD>
  <TD align="center">118.6</TD>
</TR>
"""


def test_a_station_with_sidings_is_not_dropped():
    """The bug this pins cost 16 of 24 stations on a real route.

    A station with sidings carries a link inside the code cell, so joining the
    cell's strings with a space produced `GDYA Sidings: [MPBG, THSG]`, which
    failed the code pattern -- and the station was skipped in silence. The
    destination was among the stations lost, so a route could be recorded as
    never reaching where it was going.
    """
    _distance, codes, _junctions = RBSScraper._parse(SIDINGS_HTML)
    assert "GDYA" in codes
    assert "BIA" in codes, "the destination itself was being dropped"


def test_the_strict_code_rule_still_rejects_junk_cells():
    """Fixing the extraction must not turn into accepting anything."""
    _distance, codes, _junctions = RBSScraper._parse(SIDINGS_HTML)
    assert codes == ["GDYA", "BZU", "BIA"]   # the one-letter cell stays out


def test_distance_recovers_when_the_dropped_rows_come_back():
    """Distance is the max over kept rows, so dropped rows shortened routes.

    The destination carries sidings here, exactly as BIA does on the real
    response, so losing it under-reads the route's total length.
    """
    distance, _codes, _junctions = RBSScraper._parse(SIDINGS_HTML)
    assert distance == pytest.approx(118.6)  # not 24.5 from a truncated read


def test_parse_detailed_reads_the_same_sidings_cell():
    stations = RBSScraper.parse_detailed(SIDINGS_HTML)
    assert [s["code"] for s in stations] == ["GDYA", "BZU", "BIA"]
    assert stations[0]["name"] == "Ghoradongri"


def test_parse_detailed_keeps_the_names_parse_throws_away():
    """The pipeline needs codes; a human reading a route needs names too."""
    stations = RBSScraper.parse_detailed(POSITIONAL_HTML)
    assert [s["code"] for s in stations] == ["BSL", "JL", "NGP"]
    assert [s["name"] for s in stations] == ["Bhusaval Jn", "Jalgaon", "Nagpur"]
    assert stations[-1]["cumulative_km"] == pytest.approx(1234.5)


def test_parse_detailed_agrees_with_parse_on_the_codes():
    """Two parsers reading one page must not disagree about which stations."""
    _distance, codes, _junctions = RBSScraper._parse(POSITIONAL_HTML)
    assert [s["code"] for s in RBSScraper.parse_detailed(POSITIONAL_HTML)] == codes


# --- degenerate routes ----------------------------------------------------

@pytest.mark.parametrize("source,destination,route,usable", [
    ("DURG", "RJN", ["DURG"], False),       # portal echoed the origin back
    ("DURG", "RJN", [], False),             # nothing at all
    ("DURG", "BPL", ["DURG", "BPL"], True),
    ("DURG", "DURG", ["DURG"], True),       # same place: one station is correct
    ("durg", " DURG ", ["DURG"], True),     # ...and normalisation applies first
])
def test_single_station_answer_is_only_a_route_to_itself(source, destination,
                                                         route, usable):
    """A one-station 0 km 'route' cached as SUCCESS is worse than a cache miss.

    It is never retried, never reaches the unresolved audit, and downstream it
    reads as a real flow that happens to touch no corridor station. RBS returns
    this shape for a destination code that does not exist.
    """
    from agents.agent_2_rbs_scraper import is_usable_route
    assert is_usable_route(source, destination, route) is usable


# --- failure classification ----------------------------------------------

def test_circuit_opens_after_repeated_connection_errors(monkeypatch, tmp_path):
    """A refused handshake will not heal inside one run; fail fast instead."""
    import requests
    from agents import agent_2_rbs_scraper as module

    scraper = RBSScraper.__new__(RBSScraper)          # skip __init__/DB
    scraper._stats_lock = __import__("threading").Lock()
    scraper.stats = {"skipped_circuit_open": 0}
    scraper._consecutive_conn_errors = 0
    scraper.circuit_open = False
    scraper.url = "http://example.invalid"
    scraper._limiter = module.RateLimiter(0)

    def always_refuse(*_a, **_k):
        raise requests.exceptions.ConnectionError("refused")

    monkeypatch.setattr(scraper, "_session", lambda: type(
        "S", (), {"post": staticmethod(always_refuse)})())

    for _ in range(module.CIRCUIT_BREAK_AFTER):
        with pytest.raises(module.RBSError):
            scraper._post("AAA", "BBB")

    assert scraper.circuit_open is True
    # Subsequent calls are skipped rather than retried.
    with pytest.raises(module.RBSError, match="circuit open"):
        scraper._post("CCC", "DDD")
    assert scraper.stats["skipped_circuit_open"] == 1


def test_tls_verification_is_on_by_default():
    """These distances reach client deliverables; do not ship verify=False."""
    from agents import agent_2_rbs_scraper as module
    assert module.VERIFY_TLS is True


# --- egress ---------------------------------------------------------------

def test_proxy_defaults_to_the_configured_one(monkeypatch):
    """RBS_PROXY and RAIL_RBS_PROXY resolve through config, so there is one
    definition rather than a second name that silently does nothing."""
    from agents import agent_2_rbs_scraper as module

    monkeypatch.setattr(module, "RBS_PROXY", "http://configured:8080")
    monkeypatch.setattr(module, "RBSCache", lambda *a, **k: object())

    assert RBSScraper().proxy == "http://configured:8080"
    # An explicit argument wins over the configured value...
    assert RBSScraper(proxy="http://explicit:3128").proxy == "http://explicit:3128"
    # ...including an explicit "no proxy", which must not fall back to config.
    assert RBSScraper(proxy="").proxy == ""


def test_a_proxy_does_not_change_the_request_rate():
    """A proxy decides where requests leave from, not how many are sent.

    Removing the limiter would be asking the portal for more, which is a
    different thing from making a blocked deployment work at all.
    """
    from agents import agent_2_rbs_scraper as module
    assert module.MIN_INTERVAL >= 0.30
    assert module.MAX_WORKERS <= 6
