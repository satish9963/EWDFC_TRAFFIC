import io
import time

import pandas as pd
import streamlit as st

from config import APP_ICON, APP_TITLE
from core import schema
from core.cache import RBSCache
from core.geometry import build_from_placemarks, load_alignment, read_kml_placemarks, \
    summarise_layers
from core.orchestrator import WorkflowOrchestrator
from ui import inputs, results as results_ui

st.set_page_config(page_title=APP_TITLE, page_icon=APP_ICON, layout="wide")


def to_excel(sheets):
    output = io.BytesIO()
    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        for name, frame in sheets.items():
            if frame is not None and not frame.empty:
                # Excel sheet names cap at 31 characters.
                frame.to_excel(writer, index=False, sheet_name=name[:31])
    return output.getvalue()


@st.cache_data(show_spinner=False)
def _cache_stats():
    try:
        return RBSCache().stats()
    except Exception:
        return None


@st.cache_data(show_spinner="Reading alignment...")
def _read_placemarks(payload, filename):
    """Parsed once per uploaded file rather than on every widget interaction.

    A CAD export runs to tens of megabytes, and Streamlit re-runs the whole
    script on each control change; without this the layer picker would reparse
    21 MB of XML every time it was touched.
    """
    return read_kml_placemarks(payload), filename


st.title(APP_TITLE)
st.caption(
    "Maps Indian Railways freight OD traffic onto a rail corridor and identifies "
    "diversion potential. Works for any corridor — dedicated freight corridors, "
    "new lines, doubling and upgrade schemes — defined by a station list, an "
    "alignment drawing, or both."
)

project, od_file, corridor_file, alignment_file = inputs.render_sidebar(_cache_stats())

for key, default in (("results", None), ("excel_data", None), ("ran_label", "")):
    if key not in st.session_state:
        st.session_state[key] = default

# --- alignment, parsed up front so its layers can be chosen before running ---
geometry = None
alignment_error = None
if alignment_file is not None:
    suffix = "." + alignment_file.name.rsplit(".", 1)[-1].lower()
    try:
        if suffix in (".kml", ".kmz"):
            placemarks, _ = _read_placemarks(alignment_file.getvalue(), alignment_file.name)
            layer = inputs.render_alignment_controls(summarise_layers(placemarks, depth=2))
            geometry = build_from_placemarks(placemarks, alignment_layer=layer)
        else:
            geometry = load_alignment(io.BytesIO(alignment_file.getvalue()), suffix=suffix)
    except Exception as exc:
        alignment_error = str(exc)

if alignment_error:
    st.warning(f"Could not read the alignment: {alignment_error}")
elif geometry is not None:
    bits = [f"Alignment read: **{geometry.length_km:,.0f} km**",
            f"{len(geometry.stations)} station placemarks"]
    if geometry.bridged_gaps_km:
        bits.append(f"{len(geometry.bridged_gaps_km)} gap(s) under 1 km bridged")
    if geometry.chainage_available:
        bits.append(f"chainage over {geometry.primary_length_km:,.0f} km "
                    f"({geometry.chainage_coverage:.0%} of the alignment)")
    else:
        bits.append("too fragmented for chainage — pick the alignment layer explicitly")
    st.info(" · ".join(bits))

if project.matching.by_proximity and geometry is None:
    st.warning(
        "Proximity matching is enabled but no alignment has been uploaded, so only "
        "station-code matching will apply."
    )

if st.button("Run assessment pipeline", type="primary"):
    if od_file is None or corridor_file is None:
        st.error("Upload both the OD traffic and corridor station workbooks before running.")
    else:
        try:
            od_df = pd.read_excel(od_file)
            corridor_df = pd.read_excel(corridor_file)
        except Exception as exc:
            st.error(f"Could not read the uploaded workbooks: {exc}")
            st.stop()

        started = time.time()
        with st.status("Running pipeline...", expanded=True) as status:
            def update_progress(pct, message):
                status.update(label=f"{message}  ({pct}%  ·  {time.time() - started:.0f}s)")

            try:
                orchestrator = WorkflowOrchestrator(od_df, corridor_df, project, geometry=geometry)
                run_results = orchestrator.run(progress_callback=update_progress)
            except ValueError as exc:
                # Validation errors already name the offending columns.
                status.update(label="Pipeline stopped", state="error")
                st.error(str(exc))
                st.stop()
            except Exception as exc:
                status.update(label="Pipeline failed", state="error")
                st.error(f"Pipeline failed: {exc}")
                st.stop()

            status.update(
                label=f"Completed in {time.time() - started:.0f}s",
                state="complete", expanded=False,
            )

        st.session_state.results = run_results
        st.session_state.ran_label = f"{project.short_name} · {run_results['criterion']}"

        sheets = {
            "Master OD": run_results["master_od"],
            "Divertible": run_results["master_od"][
                run_results["master_od"][schema.ELIGIBLE] == "YES"],
            "Station Summary": run_results["station_summary"],
            "Route Combos": run_results["route_combos"],
            "Exceptions": run_results["exception_log"],
        }
        for threshold in run_results.get("thresholds", ()):
            frame = run_results.get(f"threshold_{threshold}_eligible")
            if frame is not None and not frame.empty:
                sheets[f"Eligible_T{threshold}"] = frame
        st.session_state.excel_data = to_excel(sheets)
        _cache_stats.clear()

if st.session_state.results is not None:
    current_label = f"{project.short_name} · "
    if not st.session_state.ran_label.startswith(current_label):
        st.warning(
            f"These results were produced with: {st.session_state.ran_label}. "
            f"Re-run the pipeline to apply the current settings."
        )
    results_ui.render(st.session_state.results)

    if st.session_state.excel_data is not None:
        inputs.render_export(
            st.session_state.excel_data,
            st.session_state.results["project"],
            st.session_state.results["project"].eligibility.rule,
        )
else:
    st.info("Upload the OD traffic and corridor station workbooks in the sidebar, then run.")
