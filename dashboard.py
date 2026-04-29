#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Franchise Links Dashboard — Streamlit App"""

import os
import glob
import re
from datetime import datetime

import pandas as pd
import streamlit as st

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

DATA_DIR = "data"
CSV_PATTERN = os.path.join(DATA_DIR, "Franchise_Links_Report_*.csv")

st.set_page_config(
    page_title="Franchise Links Dashboard",
    page_icon="🔗",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def get_scan_files() -> list[str]:
    """Return sorted list of CSV files, newest first."""
    files = glob.glob(CSV_PATTERN)
    files.sort(reverse=True)
    return files


def extract_date_from_filename(filename: str) -> str:
    """Extract date from filename like Franchise_Links_Report_20260429_120000.csv"""
    match = re.search(r"(\d{8}_\d{6})", os.path.basename(filename))
    if match:
        date_str = match.group(1)
        try:
            dt = datetime.strptime(date_str, "%Y%m%d_%H%M%S")
            return dt.strftime("%Y-%m-%d %H:%M")
        except ValueError:
            return date_str
    return "Unknown"


def load_csv(filepath: str) -> pd.DataFrame:
    """Load CSV into DataFrame."""
    return pd.read_csv(filepath)


def compute_summary(df: pd.DataFrame) -> dict:
    """Compute summary statistics."""
    total = len(df)
    coming_soon = len(df[df["status"] == "COMING_SOON"]) if "status" in df.columns else 0
    live = total - coming_soon

    if "status" not in df.columns:
        return {"total": total, "ok": 0, "errors": 0, "redirects": 0, "coming_soon": 0}

    status_counts = df["status"].value_counts().to_dict()

    ok_statuses = {"OK"}
    redirect_statuses = {"REDIRECT_MAIN", "REDIRECT_OTHER"}
    error_statuses = {
        "NOT_FOUND", "TIMEOUT", "CONNECTION_ERROR", "SERVER_ERROR_500",
        "SERVER_ERROR_502", "SERVER_ERROR_503", "SERVER_ERROR_504",
        "BOT_BLOCKED", "PARKED", "FORBIDDEN", "UNHANDLED_ERROR",
        "HTTP_429", "BROWSER_ERROR", "REQUEST_ERROR",
    }

    ok = sum(status_counts.get(s, 0) for s in ok_statuses)
    redirects = sum(status_counts.get(s, 0) for s in redirect_statuses)
    errors = sum(status_counts.get(s, 0) for s in error_statuses)
    maintenance = status_counts.get("MAINTENANCE", 0)
    empty_page = status_counts.get("EMPTY_PAGE", 0)
    brand_mismatch = status_counts.get("BRAND_MISMATCH", 0)

    return {
        "total": total,
        "live": live,
        "coming_soon": coming_soon,
        "ok": ok,
        "redirects": redirects,
        "errors": errors,
        "maintenance": maintenance,
        "empty_page": empty_page,
        "brand_mismatch": brand_mismatch,
    }


def format_summary(summary: dict) -> str:
    """Format summary as readable text."""
    lines = [
        f"**Total Links:** {summary['total']}",
        f"**Live:** {summary['live']} | **Coming Soon:** {summary['coming_soon']}",
        f"**OK:** {summary['ok']} | **Redirects:** {summary['redirects']} | **Errors:** {summary['errors']}",
    ]
    if summary.get("maintenance", 0) > 0:
        lines.append(f"- Maintenance: {summary['maintenance']}")
    if summary.get("empty_page", 0) > 0:
        lines.append(f"- Empty Pages: {summary['empty_page']}")
    if summary.get("brand_mismatch", 0) > 0:
        lines.append(f"- Brand Mismatch: {summary['brand_mismatch']}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# UI
# ---------------------------------------------------------------------------


def main():
    st.title("🔗 Franchise Links Dashboard")
    st.markdown("---")

    files = get_scan_files()

    if not files:
        st.warning(
            "No scan results found. Ensure scans are running and saving to the `data/` folder."
        )
        st.info(
            "**Expected path:** `data/Franchise_Links_Report_YYYYMMDD_HHMMSS.csv`"
        )
        return

    # Sidebar
    st.sidebar.header("Navigation")
    selected_file = st.sidebar.selectbox(
        "Select Scan",
        files,
        format_func=lambda x: extract_date_from_filename(x),
    )

    st.sidebar.markdown("---")
    st.sidebar.markdown("### About")
    st.sidebar.markdown(
        "Daily automated scans of franchise links. "
        "Checks for redirects, errors, maintenance pages, and brand compliance."
    )

    # Load selected scan
    df = load_csv(selected_file)
    scan_date = extract_date_from_filename(selected_file)
    summary = compute_summary(df)

    # Header
    col1, col2 = st.columns([3, 1])
    with col1:
        st.subheader(f"Scan: {scan_date}")
    with col2:
        st.download_button(
            label="📥 Download CSV",
            data=open(selected_file, "rb").read(),
            file_name=os.path.basename(selected_file),
            mime="text/csv",
        )

    st.markdown("---")

    # Summary metrics
    st.subheader("📊 Summary")
    col1, col2, col3, col4 = st.columns(4)

    with col1:
        st.metric("Total", summary["total"])

    with col2:
        st.metric("Live", summary["live"])

    with col3:
        ok_delta = summary["ok"] - summary["redirects"] - summary["errors"]
        st.metric("OK", summary["ok"], delta=f"{ok_delta:+d}" if ok_delta != 0 else None)

    with col4:
        st.metric("Issues", summary["errors"] + summary["redirects"])

    # Detailed breakdown
    with st.expander("📋 Detailed Breakdown", expanded=False):
        st.markdown(format_summary(summary))

        if "status" in df.columns:
            status_df = df["status"].value_counts().reset_index()
            status_df.columns = ["Status", "Count"]
            st.dataframe(status_df, use_container_width=True, hide_index=True)

    # Status filter
    st.subheader("🔍 Browse Results")

    if "status" in df.columns:
        all_statuses = sorted(df["status"].dropna().unique())
        status_filter = st.multiselect(
            "Filter by Status",
            options=all_statuses,
            default=all_statuses,
        )
        df_filtered = df[df["status"].isin(status_filter)]
    else:
        df_filtered = df

    # Country filter
    if "country" in df.columns:
        all_countries = sorted(df_filtered["country"].dropna().unique())
        country_filter = st.multiselect(
            "Filter by Country",
            options=all_countries,
            default=all_countries,
        )
        df_filtered = df_filtered[df_filtered["country"].isin(country_filter)]

    st.dataframe(
        df_filtered,
        use_container_width=True,
        hide_index=True,
        column_config={
            "country": st.column_config.TextColumn("Country", width="small"),
            "url": st.column_config.LinkColumn("URL", width="medium"),
            "status": st.column_config.TextColumn("Status", width="small"),
            "code": st.column_config.NumberColumn("Code", width="small"),
            "note": st.column_config.TextColumn("Notes", width="large"),
        },
    )

    # Scan history
    st.markdown("---")
    st.subheader("📅 Scan History")

    history_data = []
    for f in files[:10]:  # Last 10 scans
        date_str = extract_date_from_filename(f)
        temp_df = load_csv(f)
        temp_summary = compute_summary(temp_df)
        history_data.append({
            "Date": date_str,
            "Total": temp_summary["total"],
            "OK": temp_summary["ok"],
            "Redirects": temp_summary["redirects"],
            "Errors": temp_summary["errors"],
        })

    if history_data:
        history_df = pd.DataFrame(history_data)
        st.dataframe(history_df, use_container_width=True, hide_index=True)


if __name__ == "__main__":
    main()
