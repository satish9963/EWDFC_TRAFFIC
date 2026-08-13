import io
import time

import pandas as pd
import streamlit as st

from core.orchestrator import WorkflowOrchestrator
from ui import inputs, results as results_ui

st.set_page_config(
    page_title="EWDFC Traffic Assessment",
    page_icon="🚂",
    layout="wide",
)


def to_excel(df_dict):
    output = io.BytesIO()
    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        for sheet_name, df in df_dict.items():
            df.to_excel(writer, index=False, sheet_name=sheet_name)
    return output.getvalue()


st.title("EWDFC Traffic Assessment & Route Mapping")
st.caption(
    "Maps Indian Railways freight OD traffic onto the EWDFC alignment and "
    "identifies diversion potential. Routes are served from a bundled cache of "
    "previously fetched RBS shortest paths; only unseen station pairs are "
    "fetched live."
)

threshold, od_file, dfc_file = inputs.render_sidebar()

if "results" not in st.session_state:
    st.session_state.results = None
if "excel_data" not in st.session_state:
    st.session_state.excel_data = None
if "ran_threshold" not in st.session_state:
    st.session_state.ran_threshold = threshold

if st.button("Run assessment pipeline", type="primary"):
    if od_file is None or dfc_file is None:
        st.error("Upload both the OD traffic and DFC station workbooks before running.")
    else:
        try:
            od_df = pd.read_excel(od_file)
            dfc_df = pd.read_excel(dfc_file)
        except Exception as exc:
            st.error(f"Could not read the uploaded workbooks: {exc}")
            st.stop()

        started = time.time()
        with st.status("Running pipeline...", expanded=True) as status:
            def update_progress(pct, msg):
                status.update(label=f"{msg}  ({pct}%  ·  {time.time() - started:.0f}s)")

            try:
                orchestrator = WorkflowOrchestrator(od_df, dfc_df, threshold)
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
                state="complete",
                expanded=False,
            )

        st.session_state.results = run_results
        st.session_state.ran_threshold = threshold
        st.session_state.excel_data = to_excel({
            "Master OD": run_results["master_od"],
            f"Eligible_T{threshold}": run_results[f"threshold_{threshold}_eligible"],
            "Station Summary": run_results["station_summary"],
            "Route Combos": run_results["route_combos"],
            "Exceptions": run_results["exception_log"],
        })

if st.session_state.results is not None:
    ran = st.session_state.ran_threshold
    if ran != threshold:
        st.warning(
            f"These results were produced at threshold T{ran}. "
            f"Re-run the pipeline to apply T{threshold}."
        )
    results_ui.render(st.session_state.results, ran)

    if st.session_state.excel_data is not None:
        inputs.render_export(st.session_state.excel_data, ran)
else:
    st.info("Upload both workbooks in the sidebar, then run the pipeline.")
