import streamlit as st
import pandas as pd
import io
from core.orchestrator import WorkflowOrchestrator
from config import SUPPORTED_THRESHOLDS, DEFAULT_THRESHOLD

st.set_page_config(page_title="EWDFC Traffic Assessment", layout="wide")

st.title("🚂 EWDFC Traffic Assessment & Route Mapping")

st.markdown("""
This system processes Indian Railways freight OD traffic data, retrieves routes from the RBS portal, 
maps traffic against the EWDFC alignment, and identifies diversion potential.
""")

st.sidebar.header("Configuration")
threshold = st.sidebar.selectbox("Minimum DFC Interaction Count (Threshold)", options=SUPPORTED_THRESHOLDS, index=SUPPORTED_THRESHOLDS.index(DEFAULT_THRESHOLD))

st.sidebar.markdown("---")
st.sidebar.header("Upload Data")

od_file = st.sidebar.file_uploader("Upload OD Traffic (Excel)", type=["xlsx", "xls"])
dfc_file = st.sidebar.file_uploader("Upload DFC Station List (Excel)", type=["xlsx", "xls"])

# Helper to convert df to excel in memory
def to_excel(df_dict):
    output = io.BytesIO()
    with pd.ExcelWriter(output, engine='openpyxl') as writer:
        for sheet_name, df in df_dict.items():
            df.to_excel(writer, index=False, sheet_name=sheet_name)
    processed_data = output.getvalue()
    return processed_data

if "results" not in st.session_state:
    st.session_state.results = None
if "excel_data" not in st.session_state:
    st.session_state.excel_data = None

if st.button("Run Assessment Pipeline"):
    if od_file is None or dfc_file is None:
        st.error("Please upload both the OD Traffic and DFC Station Excel files.")
    else:
        # Load data
        try:
            od_df = pd.read_excel(od_file)
            dfc_df = pd.read_excel(dfc_file)
        except Exception as e:
            st.error(f"Error reading Excel files: {e}")
            st.stop()
            
        progress_bar = st.progress(0)
        status_text = st.empty()
        
        def update_progress(pct, msg):
            progress_bar.progress(pct)
            status_text.markdown(f"### ⏳ Overall Progress: {pct}%\n**Current Task:** {msg}")
            
        try:
            orchestrator = WorkflowOrchestrator(od_df, dfc_df, threshold)
            results = orchestrator.run(progress_callback=update_progress)
            
            # Save to session state
            st.session_state.results = results
            
            export_dict = {
                "Master OD": results['master_od'],
                f"Eligible_T{threshold}": results[f'threshold_{threshold}_eligible'],
                "Station Summary": results['station_summary'],
                "Route Combos": results['route_combos'],
                "Exceptions": results['exception_log']
            }
            st.session_state.excel_data = to_excel(export_dict)
            
            st.success("Processing Complete!")
            
        except Exception as e:
            st.error(f"Pipeline failed: {e}")

# Display results if available in session state
if st.session_state.results is not None:
    results = st.session_state.results
    
    st.header("Results Dashboard")
    col1, col2, col3 = st.columns(3)
    col1.metric("Total OD Routes Processed", len(results['master_od']))
    col2.metric("Eligible OD Routes", len(results['master_od'][results['master_od']['Eligible'] == 'YES']))
    col3.metric("Exceptions Found", len(results['exception_log']))
    
    tab1, tab2, tab3, tab4, tab5 = st.tabs(["Master OD", "Station Summary", "Route Combos", "Eligible Traffic", "Exceptions"])
    with tab1:
        st.dataframe(results['master_od'])
    with tab2:
        st.dataframe(results['station_summary'])
    with tab3:
        st.dataframe(results['route_combos'])
    with tab4:
        st.dataframe(results[f'threshold_{threshold}_eligible'])
    with tab5:
        st.dataframe(results['exception_log'])
        
if st.session_state.excel_data is not None:
    st.sidebar.markdown("---")
    st.sidebar.header("Export Results")
    st.sidebar.download_button(
        label="📥 Download Full Report (Excel)",
        data=st.session_state.excel_data,
        file_name=f"EWDFC_Traffic_Report_T{threshold}.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )
