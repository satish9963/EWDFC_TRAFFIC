"""Sidebar: configuration, input requirements and uploads."""
import io

import pandas as pd
import streamlit as st

from config import DEFAULT_THRESHOLD, DFC_STATION_COLUMNS, OD_COLUMNS, SUPPORTED_THRESHOLDS


def _blank_template(columns):
    """An empty workbook carrying just the required header row."""
    buffer = io.BytesIO()
    with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
        pd.DataFrame(columns=columns).to_excel(writer, index=False, sheet_name="Data")
    return buffer.getvalue()


XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def render_sidebar():
    """Draw the sidebar and return (threshold, od_file, dfc_file)."""
    with st.sidebar:
        st.subheader("Configuration")
        threshold = st.selectbox(
            "Minimum DFC interaction count",
            options=SUPPORTED_THRESHOLDS,
            index=SUPPORTED_THRESHOLDS.index(DEFAULT_THRESHOLD),
            help="An OD route is treated as divertible once it touches at least "
                 "this many DFC stations.",
        )

        st.divider()
        st.subheader("Input data")

        od_file = st.file_uploader("OD traffic (Excel)", type=["xlsx", "xls"])
        dfc_file = st.file_uploader("DFC station list (Excel)", type=["xlsx", "xls"])

        with st.expander("Required columns"):
            st.caption("**OD traffic**")
            st.write("\n".join(f"- {c}" for c in OD_COLUMNS))
            st.caption("**DFC station list**")
            st.write("\n".join(f"- {c}" for c in DFC_STATION_COLUMNS))
            st.caption(
                "Exports that use `FROMSTTN` / `TOSTTN` style headers are mapped "
                "automatically. Tonnage is reported in whatever unit the workbook "
                "uses — nothing is converted."
            )
            st.download_button(
                "Blank OD template",
                data=_blank_template(OD_COLUMNS),
                file_name="EWDFC_OD_template.xlsx",
                mime=XLSX_MIME,
                width="stretch",
            )
            st.download_button(
                "Blank DFC station template",
                data=_blank_template(DFC_STATION_COLUMNS),
                file_name="EWDFC_DFC_stations_template.xlsx",
                mime=XLSX_MIME,
                width="stretch",
            )

    return threshold, od_file, dfc_file


def render_export(excel_data, threshold):
    """Export control, shown only once a run has produced results."""
    with st.sidebar:
        st.divider()
        st.subheader("Export")
        st.download_button(
            "Download full report (Excel)",
            data=excel_data,
            file_name=f"EWDFC_Traffic_Report_T{threshold}.xlsx",
            mime=XLSX_MIME,
            width="stretch",
        )
