"""Results dashboard: headline figures, charts and detail tables."""
import altair as alt
import pandas as pd
import streamlit as st

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

TONNAGE = "Annual Tonnage"


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


def _headline(master, exceptions, threshold):
    total_t = master[TONNAGE].sum() if TONNAGE in master else 0
    eligible = master[master["Eligible"] == "YES"] if "Eligible" in master else master.iloc[0:0]
    eligible_t = eligible[TONNAGE].sum() if TONNAGE in eligible else 0

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("OD routes processed", format_number(len(master)))
    c2.metric(
        f"Divertible routes (T{threshold})",
        format_number(len(eligible)),
        help="Routes touching at least the threshold number of DFC stations.",
    )
    c3.metric(
        "Divertible tonnage",
        format_number(eligible_t),
        delta=f"{format_percent(eligible_t, total_t)} of {format_number(total_t)}",
        delta_color="off",
    )
    c4.metric("Exceptions raised", format_number(len(exceptions)))


def _station_chart(station_summary):
    cols = ["entering_tonnage", "exiting_tonnage", "through_tonnage"]
    if station_summary.empty or not all(c in station_summary for c in cols):
        st.info("No station traffic to chart.")
        return

    top = station_summary.nlargest(12, "total_tonnage")
    if top["total_tonnage"].sum() == 0:
        st.info("No station traffic to chart.")
        return

    long = top.melt(
        id_vars=["DFC Station Code", "total_tonnage"],
        value_vars=cols,
        var_name="Movement",
        value_name="Tonnage",
    )
    long["Movement"] = long["Movement"].str.replace("_tonnage", "", regex=False).str.capitalize()

    order = alt.Y("DFC Station Code:N", sort="-x", title=None)
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
            tooltip=["DFC Station Code", "Movement", alt.Tooltip("Tonnage:Q", format=",.0f")],
        )
    )
    # Direct totals: the relief required by the aqua contrast warning.
    labels = (
        alt.Chart(top)
        .mark_text(align="left", dx=4, color=INK_SECONDARY, fontSize=11)
        .encode(
            x=alt.X("total_tonnage:Q", title="Tonnage"),
            y=order,
            text=alt.Text("total_tonnage:Q", format=",.0f"),
        )
    )
    st.altair_chart(_style((bars + labels).properties(height=340)), width="stretch")


def _commodity_chart(master):
    if "Commodity" not in master or TONNAGE not in master:
        st.info("No commodity column in this dataset.")
        return
    eligible = master[master["Eligible"] == "YES"]
    if eligible.empty:
        st.info("No divertible traffic at this threshold.")
        return

    by_commodity = (
        eligible.groupby("Commodity", as_index=False)[TONNAGE].sum()
        .nlargest(12, TONNAGE)
    )
    chart = (
        alt.Chart(by_commodity)
        .mark_bar(cornerRadiusEnd=4, color=SINGLE_HUE)
        .encode(
            x=alt.X(f"{TONNAGE}:Q", title="Divertible tonnage"),
            y=alt.Y("Commodity:N", sort="-x", title=None),
            tooltip=["Commodity", alt.Tooltip(f"{TONNAGE}:Q", format=",.0f")],
        )
        .properties(height=320)
    )
    st.altair_chart(_style(chart), width="stretch")


def _threshold_chart(results):
    rows = []
    for t in (2, 3):
        frame = results.get(f"threshold_{t}_eligible")
        if frame is None or TONNAGE not in frame:
            continue
        rows.append({"Threshold": f"T{t}", "Tonnage": frame[TONNAGE].sum(), "Routes": len(frame)})
    if not rows:
        st.info("Threshold comparison unavailable.")
        return

    data = pd.DataFrame(rows)
    bars = (
        alt.Chart(data)
        .mark_bar(cornerRadiusEnd=4, color=SINGLE_HUE, size=48)
        .encode(
            x=alt.X("Threshold:N", title=None),
            y=alt.Y("Tonnage:Q", title="Divertible tonnage"),
            tooltip=["Threshold", alt.Tooltip("Tonnage:Q", format=",.0f"), "Routes"],
        )
    )
    labels = bars.mark_text(dy=-8, color=INK_SECONDARY, fontSize=11).encode(
        text=alt.Text("Tonnage:Q", format=",.0f")
    )
    st.altair_chart(_style((bars + labels).properties(height=300)), width="stretch")


def render(results, threshold):
    master = results["master_od"]
    exceptions = results["exception_log"]

    st.subheader("Headline")
    _headline(master, exceptions, threshold)

    st.subheader("Where the traffic sits")
    left, right = st.columns([3, 2])
    with left:
        st.caption("Top DFC stations by tonnage, split by movement")
        _station_chart(results["station_summary"])
    with right:
        st.caption("Divertible tonnage by threshold")
        _threshold_chart(results)

    st.caption("Divertible tonnage by commodity")
    _commodity_chart(master)

    st.subheader("Detail")
    tabs = st.tabs(
        ["Master OD", "Station summary", "Route combinations",
         f"Divertible (T{threshold})", "Exceptions"]
    )
    frames = [
        master,
        results["station_summary"],
        results["route_combos"],
        results[f"threshold_{threshold}_eligible"],
        exceptions,
    ]
    for tab, frame in zip(tabs, frames):
        with tab:
            if frame is None or frame.empty:
                st.info("Nothing to show here.")
            else:
                st.dataframe(display_columns(frame), width="stretch", hide_index=True)
