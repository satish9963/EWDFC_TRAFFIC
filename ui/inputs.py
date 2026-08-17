"""Sidebar: project selection, inputs, and cache state."""
import io

import pandas as pd
import streamlit as st

from core import schema
from core.projects import RULE_LABELS, RULES, RULE_MIN_CORRIDOR_KM, \
    RULE_MIN_STATIONS, load_projects

XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def _blank_template(columns):
    """An empty workbook carrying just the required header row."""
    buffer = io.BytesIO()
    with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
        pd.DataFrame(columns=columns).to_excel(writer, index=False, sheet_name="Data")
    return buffer.getvalue()


def _project_picker():
    projects, default_key = load_projects()
    keys = list(projects)
    chosen = st.selectbox(
        "Project",
        options=keys,
        index=keys.index(default_key),
        format_func=lambda k: projects[k].short_name,
        help="Presets live in projects.yaml. Every setting below can be overridden.",
    )
    project = projects[chosen]
    if project.description:
        st.caption(project.description)
    return project


def _eligibility_controls(project):
    """Rule and threshold, defaulted from the preset but freely overridable."""
    eligibility = project.eligibility
    rule = st.selectbox(
        "Eligibility rule",
        options=list(RULES),
        index=list(RULES).index(eligibility.rule),
        format_func=lambda r: RULE_LABELS[r],
    )

    overrides = {"rule": rule}
    if rule == RULE_MIN_STATIONS:
        offered = list(eligibility.thresholds_offered)
        current = eligibility.threshold if eligibility.threshold in offered else offered[0]
        overrides["threshold"] = st.selectbox(
            "Minimum corridor stations touched",
            options=offered,
            index=offered.index(current),
            help="A route counts as divertible once it touches this many corridor stations.",
        )
    elif rule == RULE_MIN_CORRIDOR_KM:
        overrides["min_corridor_km"] = st.number_input(
            "Minimum corridor length used (km)",
            min_value=1.0, max_value=5000.0, step=10.0,
            value=float(eligibility.min_corridor_km or 50.0),
            help="Needs chainage, from the station list or a supplied alignment.",
        )
    else:
        overrides["min_corridor_share"] = st.slider(
            "Minimum share of route on corridor",
            min_value=0.05, max_value=1.0, step=0.05,
            value=float(eligibility.min_corridor_share or 0.25),
            help="Corridor km used, divided by the route's total IR distance.",
        )
    return overrides


def _matching_controls(project):
    matching = project.matching
    by_code = st.checkbox(
        "Match by station code", value=matching.by_code,
        help="A route station counts when its code appears in the corridor list.",
    )
    by_proximity = st.checkbox(
        "Match by distance from alignment", value=matching.by_proximity,
        help="A route station counts when it falls within the buffer of the "
             "uploaded alignment. Needs an alignment file and known station "
             "coordinates.",
    )
    overrides = {"by_code": by_code, "by_proximity": by_proximity}
    if by_proximity:
        overrides["buffer_km"] = st.slider(
            "Buffer from alignment (km)", min_value=0.5, max_value=50.0, step=0.5,
            value=float(matching.buffer_km),
        )
    if not (by_code or by_proximity):
        st.error("Enable at least one matching mode, or no route can touch the corridor.")
    return overrides


def render_cache_panel(stats):
    """Report cache freshness honestly, including what is of unknown age."""
    with st.expander("Route cache", expanded=False):
        total = stats["total"]
        st.write(f"**{total:,}** routes cached")
        if stats["legacy"]:
            st.warning(
                f"{stats['legacy']:,} of them carry no fetch date. They were cached "
                f"before route provenance was recorded, so their age is unknown and "
                f"they are refetched when a maximum age is set."
            )
        if stats["newest"]:
            st.caption(f"Most recent fetch: {stats['newest'][:10]}")
        if stats["oldest"]:
            st.caption(f"Oldest dated fetch: {stats['oldest'][:10]}")
        st.caption(
            "Refresh the cache with `python tools/refresh_cache.py --db cache.db`. "
            "Routes are shared across projects; they do not depend on the corridor."
        )


def render_sidebar(cache_stats=None):
    """Draw the sidebar. Returns (project, od_file, corridor_file, alignment_file)."""
    with st.sidebar:
        st.subheader("Project")
        project = _project_picker()

        st.divider()
        st.subheader("Assessment basis")
        overrides = {}
        overrides.update(_eligibility_controls(project))
        overrides.update(_matching_controls(project))
        project = project.with_overrides(**overrides)

        st.divider()
        st.subheader("Input data")
        od_file = st.file_uploader("OD traffic (Excel)", type=["xlsx", "xls"])
        corridor_file = st.file_uploader("Corridor station list (Excel)", type=["xlsx", "xls"])
        alignment_file = st.file_uploader(
            "Corridor alignment (optional)",
            type=["kml", "kmz", "geojson", "json"],
            help="Supplies chainage and enables proximity matching. A CAD export "
                 "works; you will be asked which layer holds the alignment.",
        )

        with st.expander("Required columns"):
            st.caption("**OD traffic** — only the two codes are required")
            st.write("\n".join(f"- {c}" for c in schema.OD_COLUMNS))
            st.caption("**Corridor station list** — only the code is required")
            st.write("\n".join(f"- {c}" for c in schema.CORRIDOR_COLUMNS))
            st.caption(
                "Common alternative headers are recognised automatically "
                "(`FROMSTTN`, `Origin Code`, `DFC Station Code`, and so on). "
                "Tonnage is reported in whatever unit the workbook uses — "
                "nothing is converted."
            )
            st.download_button(
                "Blank OD template", data=_blank_template(schema.OD_COLUMNS),
                file_name="OD_template.xlsx", mime=XLSX_MIME, width="stretch",
            )
            st.download_button(
                "Blank corridor station template",
                data=_blank_template(schema.CORRIDOR_COLUMNS),
                file_name="Corridor_stations_template.xlsx", mime=XLSX_MIME, width="stretch",
            )

        if cache_stats:
            st.divider()
            render_cache_panel(cache_stats)

    return project, od_file, corridor_file, alignment_file


def render_alignment_controls(layer_summary):
    """Layer picker, shown only when an alignment has been uploaded."""
    with st.sidebar:
        st.divider()
        st.subheader("Alignment layers")
        options = ["(auto-detect)"] + sorted(
            layer_summary, key=lambda k: -layer_summary[k]["vertices"]
        )

        def describe(key):
            if key == "(auto-detect)":
                return key
            counts = layer_summary[key]
            return f"{key}  ({counts['vertices']:,} vtx, {counts['points']:,} pts)"

        chosen = st.selectbox("Alignment layer", options=options, format_func=describe)
        return None if chosen == "(auto-detect)" else chosen


def render_export(excel_data, project, threshold_label):
    with st.sidebar:
        st.divider()
        st.subheader("Export")
        st.download_button(
            "Download full report (Excel)",
            data=excel_data,
            file_name=f"{project.short_name}_traffic_report_{threshold_label}.xlsx",
            mime=XLSX_MIME,
            width="stretch",
        )
