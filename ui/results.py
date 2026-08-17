"""Results dashboard: headline figures, charts and detail tables."""
import altair as alt
import pandas as pd
import streamlit as st

from core import schema
from ui.format import display_columns, format_number, format_percent

# Palette validated for the light surface #fcfcfb (dataviz six checks).
# Slots 1-3 of the categorical theme; aqua sits below 3:1, so the stacked
# chart carries direct total labels as the required relief.
SERIES = {"Entering": "#2a78d6", "Exiting": "#eb6834", "Through": "#1baf7a"}
SINGLE_HUE = "#2a78d6"
SURFACE = "#fcfcfb"
INK_SECONDARY = "#52514e"
GRID = "#e1e0d9"
AXIS = "#c3c2b7"


def _style(chart):
    """Recessive grid and axes; text in ink tokens, never series colour."""
    return (
        chart.configure_view(strokeWidth=0)
        .configure_axis(
            grid=True, gridColor=GRID, gridOpacity=0.7,
            domainColor=AXIS, tickColor=AXIS,
            labelColor=INK_SECONDARY, titleColor=INK_SECONDARY,
            labelFontSize=11, titleFontSize=11,
        )
        .configure_legend(labelColor=INK_SECONDARY, titleColor=INK_SECONDARY, orient="top")
    )


def _headline(master, exceptions, criterion):
    total_tonnage = master[schema.TONNAGE].sum() if schema.TONNAGE in master else 0
    eligible = (master[master[schema.ELIGIBLE] == "YES"]
                if schema.ELIGIBLE in master else master.iloc[0:0])
    eligible_tonnage = eligible[schema.TONNAGE].sum() if schema.TONNAGE in eligible else 0

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("OD routes processed", format_number(len(master)))
    c2.metric("Divertible routes", format_number(len(eligible)), help=criterion)
    c3.metric(
        "Divertible tonnage",
        format_number(eligible_tonnage),
        delta=f"{format_percent(eligible_tonnage, total_tonnage)} of "
              f"{format_number(total_tonnage)}",
        delta_color="off",
    )
    c4.metric("Exceptions raised", format_number(len(exceptions)))

    # Corridor length used only exists when chainage was available; showing it
    # as zero when it is simply unknown would misrepresent the run.
    if schema.CORRIDOR_KM in master and master[schema.CORRIDOR_KM].notna().any():
        used = master.loc[master[schema.ELIGIBLE] == "YES", schema.CORRIDOR_KM]
        d1, d2 = st.columns(2)
        d1.metric("Mean corridor length used", f"{format_number(used.mean(), 1)} km")
        d2.metric("Max corridor length used", f"{format_number(used.max(), 1)} km")


def _station_chart(station_summary):
    columns = schema.MOVEMENT_TONNAGE_COLUMNS
    if station_summary.empty or not all(c in station_summary for c in columns):
        st.info("No station traffic to chart.")
        return

    top = station_summary.nlargest(12, schema.TOTAL_TONNAGE)
    if top[schema.TOTAL_TONNAGE].sum() == 0:
        st.info("No station traffic to chart.")
        return

    long = top.melt(
        id_vars=[schema.CORRIDOR_CODE, schema.TOTAL_TONNAGE],
        value_vars=columns,
        var_name="Movement",
        value_name="Tonnage",
    )
    long["Movement"] = long["Movement"].str.replace("_tonnage", "", regex=False).str.capitalize()

    order = alt.Y(f"{schema.CORRIDOR_CODE}:N", sort="-x", title=None)
    bars = (
        alt.Chart(long)
        .mark_bar(cornerRadiusEnd=4, stroke=SURFACE, strokeWidth=2)
        .encode(
            x=alt.X("Tonnage:Q", title="Tonnage", stack="zero"),
            y=order,
            color=alt.Color(
                "Movement:N",
                scale=alt.Scale(domain=list(SERIES), range=list(SERIES.values())),
                legend=alt.Legend(title=None),
            ),
            tooltip=[schema.CORRIDOR_CODE, "Movement", alt.Tooltip("Tonnage:Q", format=",.0f")],
        )
    )
    # Direct totals: the relief required by the aqua contrast warning.
    labels = (
        alt.Chart(top)
        .mark_text(align="left", dx=4, color=INK_SECONDARY, fontSize=11)
        .encode(
            x=alt.X(f"{schema.TOTAL_TONNAGE}:Q", title="Tonnage"),
            y=order,
            text=alt.Text(f"{schema.TOTAL_TONNAGE}:Q", format=",.0f"),
        )
    )
    st.altair_chart(_style((bars + labels).properties(height=340)), width="stretch")


def _commodity_chart(master):
    if schema.COMMODITY not in master or schema.TONNAGE not in master:
        st.info("No commodity column in this dataset.")
        return
    eligible = master[master[schema.ELIGIBLE] == "YES"]
    if eligible.empty:
        st.info("No divertible traffic on this basis.")
        return

    by_commodity = (eligible.groupby(schema.COMMODITY, as_index=False)[schema.TONNAGE].sum()
                    .nlargest(12, schema.TONNAGE))
    chart = (
        alt.Chart(by_commodity)
        .mark_bar(cornerRadiusEnd=4, color=SINGLE_HUE)
        .encode(
            x=alt.X(f"{schema.TONNAGE}:Q", title="Divertible tonnage"),
            y=alt.Y(f"{schema.COMMODITY}:N", sort="-x", title=None),
            tooltip=[schema.COMMODITY, alt.Tooltip(f"{schema.TONNAGE}:Q", format=",.0f")],
        )
        .properties(height=320)
    )
    st.altair_chart(_style(chart), width="stretch")


def _threshold_chart(results, thresholds):
    rows = []
    for threshold in thresholds:
        frame = results.get(f"threshold_{threshold}_eligible")
        if frame is None or schema.TONNAGE not in frame:
            continue
        rows.append({
            "Threshold": f"T{threshold}",
            "Tonnage": frame[schema.TONNAGE].sum(),
            "Routes": len(frame),
        })
    if not rows:
        st.info("Threshold comparison unavailable.")
        return

    data = pd.DataFrame(rows)
    bars = (
        alt.Chart(data)
        .mark_bar(cornerRadiusEnd=4, color=SINGLE_HUE, size=44)
        .encode(
            x=alt.X("Threshold:N", title=None, sort=list(data["Threshold"])),
            y=alt.Y("Tonnage:Q", title="Divertible tonnage"),
            tooltip=["Threshold", alt.Tooltip("Tonnage:Q", format=",.0f"), "Routes"],
        )
    )
    labels = bars.mark_text(dy=-8, color=INK_SECONDARY, fontSize=11).encode(
        text=alt.Text("Tonnage:Q", format=",.0f")
    )
    st.altair_chart(_style((bars + labels).properties(height=300)), width="stretch")


def _corridor_profile(station_summary):
    """Traffic against chainage -- the loading diagram a corridor study wants."""
    if (schema.CHAINAGE not in station_summary
            or station_summary[schema.CHAINAGE].isna().all()):
        return False
    data = station_summary.dropna(subset=[schema.CHAINAGE])
    chart = (
        alt.Chart(data)
        .mark_area(color=SINGLE_HUE, opacity=0.25, line={"color": SINGLE_HUE})
        .encode(
            x=alt.X(f"{schema.CHAINAGE}:Q", title="Chainage (km)"),
            y=alt.Y(f"{schema.TOTAL_TONNAGE}:Q", title="Total tonnage"),
            tooltip=[schema.CORRIDOR_CODE, alt.Tooltip(f"{schema.CHAINAGE}:Q", format=",.1f"),
                     alt.Tooltip(f"{schema.TOTAL_TONNAGE}:Q", format=",.0f")],
        )
        .properties(height=260)
    )
    st.altair_chart(_style(chart), width="stretch")
    return True


def _route_provenance(stats):
    """Where the routes came from.

    With tens of thousands of pairs already cached, a healthy run can make zero
    network calls -- which reads as "it isn't fetching". Stating the split stops
    that being mistaken for a fault, and makes a real outage unmistakable.
    """
    if not stats:
        return

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Unique OD pairs", format_number(stats.get("unique_pairs", 0)))
    c2.metric("From cache", format_number(stats.get("cache_hits", 0)))
    c3.metric("Fetched from RBS", format_number(stats.get("fetched", 0)))
    c4.metric("Portal errors", format_number(stats.get("errors", 0)))

    if stats.get("errors"):
        message = f"The RBS portal was unreachable for {stats['errors']:,} pair(s)."
        if stats.get("circuit_open"):
            message += (" Fetching stopped early after repeated connection failures, "
                        "so some pairs were never attempted.")
        if "ConnectionError" in str(stats.get("last_error") or ""):
            message += (" The TCP connection was refused — that is a network or "
                        "source-IP problem, not a data problem. Run `python net_check` "
                        "on this machine to tell which.")
        st.error(f"{message}\n\nLast error: {stats.get('last_error')}")
    elif stats.get("no_route"):
        st.warning(
            f"The portal returned no route for {stats['no_route']:,} pair(s). "
            f"These are usually station codes it does not recognise."
        )
    elif not stats.get("fetched") and stats.get("cache_hits"):
        st.info("Every OD pair was served from the cache — no live RBS calls were "
                "needed this run.")


def _run_context(results):
    """What this run actually did, stated rather than assumed."""
    project = results["project"]
    stats = results.get("route_stats", {})
    bits = [f"**{project.name}** — {results['criterion']}."]

    matching = []
    if project.matching.by_code:
        matching.append("station code")
    if project.matching.by_proximity:
        matching.append(f"within {project.matching.buffer_km:g} km of the alignment")
    bits.append(f"Corridor membership by {' or '.join(matching)}.")

    if results.get("chainage_source"):
        bits.append(f"Chainage from the {results['chainage_source']}.")
    elif results.get("geometry") is not None:
        geometry = results["geometry"]
        if geometry.chainage_available:
            bits.append(f"Chainage measured along a {geometry.primary_length_km:,.0f} km "
                        f"alignment.")
        else:
            bits.append("Alignment supplied but too fragmented for chainage; corridor "
                        "length not reported.")

    if results.get("chainage_source") is None and results.get("geometry") is None:
        bits.append("No chainage available, so corridor length is not reported.")
    st.caption(" ".join(bits))

    _route_provenance(stats)

    # Proximity matching only sees stations whose position is known. Saying so
    # is the difference between "these routes miss the corridor" and "we could
    # not tell for some of them".
    gazetteer = results.get("gazetteer") or {}
    coverage = gazetteer.get("coverage")
    if coverage is not None and coverage < 0.95:
        st.warning(
            f"Coordinates were available for {coverage:.0%} of the route stations "
            f"encountered ({gazetteer.get('unlocatable', 0):,} could not be placed), so "
            f"proximity matching is partial and corridor interaction may be understated. "
            f"Station-code matching is unaffected."
        )

    if results.get("spatial_matches"):
        with st.expander(f"{len(results['spatial_matches'])} stations matched by proximity"):
            st.write(", ".join(results["spatial_matches"]))


def render(results):
    master = results["master_od"]
    exceptions = results["exception_log"]
    thresholds = results.get("thresholds", ())

    _run_context(results)

    # A defaulted tonnage column would otherwise present as a confident zero
    # across every headline figure and chart on this page.
    filled = (results.get("validation", {}).get("od", {}) or {}).get("filled_columns") or []
    if schema.TONNAGE in filled:
        st.warning(
            "No tonnage column was recognised in the OD workbook, so every tonnage "
            "figure below is zero by default rather than by measurement. Route counts, "
            "corridor interactions and station movements are still valid. Check the "
            "column name against the template if the workbook does carry tonnage."
        )

    st.subheader("Headline")
    _headline(master, exceptions, results["criterion"])

    st.subheader("Where the traffic sits")
    left, right = st.columns([3, 2])
    with left:
        st.caption("Top corridor stations by tonnage, split by movement")
        _station_chart(results["station_summary"])
    with right:
        st.caption("Divertible tonnage by station-count threshold")
        _threshold_chart(results, thresholds)

    if _corridor_profile(results["station_summary"]):
        st.caption("Traffic along the corridor, by chainage")

    st.caption("Divertible tonnage by commodity")
    _commodity_chart(master)

    st.subheader("Detail")
    frames = {
        "Master OD": master,
        "Station summary": results["station_summary"],
        "Route combinations": results["route_combos"],
        "Divertible": master[master[schema.ELIGIBLE] == "YES"],
        "Exceptions": exceptions,
    }
    for tab, (label, frame) in zip(st.tabs(list(frames)), frames.items()):
        with tab:
            if frame is None or frame.empty:
                st.info("Nothing to show here.")
            else:
                st.dataframe(display_columns(frame), width="stretch", hide_index=True)
