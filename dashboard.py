#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Franchise Links Dashboard - Streamlit App."""

import glob
import html
import os
import re
from datetime import datetime
from urllib.parse import urlparse

import pandas as pd
import streamlit as st


DATA_DIR = "data"
FRANCHISE_REPORT_PATTERN = os.path.join(DATA_DIR, "Franchise_Links_Report_*.csv")
DOMAIN_HEALTH_REPORT_PATTERN = os.path.join(DATA_DIR, "Domain_Health_*.csv")

STATUS_REDIRECT_MAIN = "REDIRECT_MAIN"
STATUS_REDIRECT_OTHER = "REDIRECT_OTHER"
OK_STATUSES = {"OK"}
ATTENTION_STATUSES = {
    "BRAND_MISMATCH",
    "MAINTENANCE",
    "EMPTY_PAGE",
    "PARKED",
    "BOT_BLOCKED",
    "NOT_FOUND",
    "FORBIDDEN",
    "TIMEOUT",
    "CONNECTION_ERROR",
    "REQUEST_ERROR",
    "BROWSER_ERROR",
    "UNHANDLED_ERROR",
}


st.set_page_config(
    page_title="Lingua Learn Scan Dashboard",
    page_icon="LL",
    layout="wide",
    initial_sidebar_state="expanded",
)


def inject_styles() -> None:
    st.markdown(
        """
        <style>
        .block-container {padding-top: 2rem; padding-bottom: 2rem;}
        [data-testid="stMetric"] {
            background: #f7f8fa;
            border: 1px solid #eceef2;
            border-radius: 8px;
            padding: 14px 16px;
        }
        [data-testid="stMetricLabel"] p {
            color: #667085;
            font-size: 12px;
        }
        [data-testid="stMetricValue"] {
            color: #101828;
            font-size: 24px;
        }
        .section-label {
            color: #667085;
            font-size: 11px;
            font-weight: 600;
            letter-spacing: .08em;
            margin: .5rem 0 .7rem;
            text-transform: uppercase;
        }
        .badge {
            border-radius: 999px;
            display: inline-block;
            font-size: 11px;
            font-weight: 600;
            line-height: 1;
            padding: 5px 9px;
            white-space: nowrap;
        }
        .badge-green {background: #e8f5e9; color: #276738;}
        .badge-blue {background: #e6f1fb; color: #185fa5;}
        .badge-amber {background: #faeeda; color: #854f0b;}
        .badge-red {background: #fcebeb; color: #a32d2d;}
        .badge-gray {background: #f1f2f4; color: #475467;}
        table {
            border-collapse: collapse;
            font-size: 13px;
            margin-bottom: 1.4rem;
            width: 100%;
        }
        th {
            background: #f7f8fa;
            border-bottom: 1px solid #eceef2;
            color: #667085;
            font-size: 11px;
            font-weight: 600;
            letter-spacing: .06em;
            padding: 10px 12px;
            text-align: left;
            text-transform: uppercase;
        }
        td {
            border-bottom: 1px solid #eceef2;
            color: #101828;
            padding: 9px 12px;
            vertical-align: top;
        }
        tr:last-child td {border-bottom: 0;}
        .domain {font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;}
        </style>
        """,
        unsafe_allow_html=True,
    )


def get_franchise_report_files() -> list[str]:
    report_files = glob.glob(FRANCHISE_REPORT_PATTERN)
    report_files.sort(reverse=True)
    return report_files


def get_domain_health_report_files() -> list[str]:
    report_files = glob.glob(DOMAIN_HEALTH_REPORT_PATTERN)
    report_files.sort(reverse=True)
    return report_files


def extract_report_timestamp(report_path: str) -> str:
    match = re.search(r"(\d{8}_\d{6})", os.path.basename(report_path))
    if not match:
        return "Unknown"

    date_str = match.group(1)
    try:
        dt = datetime.strptime(date_str, "%Y%m%d_%H%M%S")
    except ValueError:
        return date_str
    return dt.strftime("%-d %b %Y, %H:%M")


def load_report_csv(report_path: str) -> pd.DataFrame:
    return pd.read_csv(report_path)


def normalize_status(value: object) -> str:
    if pd.isna(value):
        return "UNKNOWN"
    return str(value).strip().upper()


def domain_from_url(url: object) -> str:
    if pd.isna(url) or str(url).strip() in {"", "#"}:
        return "-"
    parsed = urlparse(str(url))
    return parsed.netloc or str(url).replace("https://", "").replace("http://", "").split("/")[0]


def classify_badge(status: str) -> str:
    if status in OK_STATUSES:
        return "green"
    if status == STATUS_REDIRECT_OTHER:
        return "blue"
    if status in {STATUS_REDIRECT_MAIN, "COMING_SOON", "MAINTENANCE", "EMPTY_PAGE"}:
        return "amber"
    if status == "UNKNOWN":
        return "gray"
    return "red"


def humanize_status(status: str) -> str:
    return status.replace("_", " ").title()


def enrich_results(report_df: pd.DataFrame) -> pd.DataFrame:
    enriched = report_df.copy()
    if "status" not in enriched.columns:
        enriched["status"] = "UNKNOWN"
    if "url" not in enriched.columns:
        enriched["url"] = ""
    if "country" not in enriched.columns:
        enriched["country"] = "Unknown"
    if "note" not in enriched.columns:
        enriched["note"] = ""

    enriched["status"] = enriched["status"].map(normalize_status)
    enriched["domain"] = enriched["url"].map(domain_from_url)
    return enriched


def compute_summary(report_df: pd.DataFrame) -> dict[str, int]:
    statuses = report_df["status"] if "status" in report_df.columns else pd.Series(dtype=str)
    status_counts = statuses.map(normalize_status).value_counts().to_dict()
    total = len(report_df)
    coming_soon = status_counts.get("COMING_SOON", 0)
    redirect_main = status_counts.get(STATUS_REDIRECT_MAIN, 0)
    redirect_other = status_counts.get(STATUS_REDIRECT_OTHER, 0)
    ok = sum(status_counts.get(status, 0) for status in OK_STATUSES)

    known_non_issue = OK_STATUSES | {
        STATUS_REDIRECT_MAIN,
        STATUS_REDIRECT_OTHER,
        "COMING_SOON",
    }
    issues = sum(
        count
        for status, count in status_counts.items()
        if status in ATTENTION_STATUSES
        or status.startswith("HTTP_")
        or status.startswith("CLIENT_ERROR_")
        or status.startswith("SERVER_ERROR_")
        or status not in known_non_issue
    )

    return {
        "total": total,
        "live": total - coming_soon,
        "coming_soon": coming_soon,
        "ok": ok,
        "redirect_main": redirect_main,
        "redirect_other": redirect_other,
        "issues": issues,
    }


def status_badge(status: str) -> str:
    color = classify_badge(status)
    return f'<span class="badge badge-{color}">{humanize_status(status)}</span>'


def render_html_table(
    table_df: pd.DataFrame,
    columns: list[tuple[str, str]],
    limit: int = 20,
) -> None:
    if table_df.empty:
        st.caption("No matching rows in this scan.")
        return

    rows = []
    for _, row in table_df.head(limit).iterrows():
        cells = []
        for source, label in columns:
            value = row.get(source, "")
            if source == "status":
                value = status_badge(normalize_status(value))
            elif source == "domain":
                value = f'<span class="domain">{html.escape(str(value))}</span>'
            else:
                value = "" if pd.isna(value) else html.escape(str(value))
            cells.append(f"<td>{value}</td>")
        rows.append("<tr>" + "".join(cells) + "</tr>")

    headers = "".join(f"<th>{label}</th>" for _, label in columns)
    st.markdown(
        f"""
        <table>
            <thead><tr>{headers}</tr></thead>
            <tbody>{''.join(rows)}</tbody>
        </table>
        """,
        unsafe_allow_html=True,
    )


def section_label(text: str) -> None:
    st.markdown(f'<div class="section-label">{text}</div>', unsafe_allow_html=True)


def render_domain_health_placeholder() -> None:
    section_label("Domain overview")
    st.info(
        "The current scanner produces franchise-link CSV reports. "
        "To populate SSL expiry and DNS/HTTP domain-health panels like the HTML mockup, "
        "add a domain-health scan that writes `data/Domain_Health_YYYYMMDD_HHMMSS.csv`."
    )


def render_domain_health(report_path: str) -> None:
    domain_health_df = load_report_csv(report_path)
    section_label(f"Domain overview - {extract_report_timestamp(report_path)}")

    online = (
        len(domain_health_df[domain_health_df.get("status", "") == "ONLINE"])
        if "status" in domain_health_df.columns
        else 0
    )
    offline = (
        len(domain_health_df[domain_health_df.get("status", "") == "OFFLINE"])
        if "status" in domain_health_df.columns
        else 0
    )
    ssl_expiring = (
        len(domain_health_df[
            pd.to_numeric(domain_health_df.get("ssl_days_left"), errors="coerce") < 60
        ])
        if "ssl_days_left" in domain_health_df.columns
        else 0
    )
    skipped = (
        len(domain_health_df[domain_health_df.get("domain", "") == ""])
        if "domain" in domain_health_df.columns
        else 0
    )

    col1, col2, col3, col4 = st.columns(4)
    col1.metric("Online", online)
    col2.metric("Offline / unreachable", offline)
    col3.metric("SSL expiring < 60 d", ssl_expiring)
    col4.metric("Skipped (no domain)", skipped)


def main() -> None:
    inject_styles()

    st.title("Lingua Learn Scan Dashboard")

    franchise_report_files = get_franchise_report_files()
    domain_health_report_files = get_domain_health_report_files()

    with st.sidebar:
        st.header("Scans")
        if franchise_report_files:
            selected_report_path = st.selectbox(
                "Franchise scan",
                franchise_report_files,
                format_func=extract_report_timestamp,
            )
        else:
            selected_report_path = None
        st.caption("Daily reports are expected in `data/`.")

    if domain_health_report_files:
        render_domain_health(domain_health_report_files[0])
    else:
        render_domain_health_placeholder()

    st.divider()

    if not selected_report_path:
        section_label("Franchise link checker")
        st.warning("No scan results found yet.")
        st.code("python main.py --use-browser", language="bash")
        return

    report_df = enrich_results(load_report_csv(selected_report_path))
    scan_date = extract_report_timestamp(selected_report_path)
    summary = compute_summary(report_df)

    section_label(f"Franchise link checker - {summary['total']} entries scanned - {scan_date}")
    col1, col2, col3, col4 = st.columns(4)
    col1.metric("OK", summary["ok"])
    col2.metric("Redirect (local)", summary["redirect_other"])
    col3.metric("Redirect -> .com", summary["redirect_main"])
    col4.metric("Issues", summary["issues"])

    csv_bytes = report_df.to_csv(index=False).encode("utf-8")
    st.download_button(
        "Download CSV",
        data=csv_bytes,
        file_name=os.path.basename(selected_report_path),
        mime="text/csv",
    )

    issue_mask = ~report_df["status"].isin([
        *OK_STATUSES,
        STATUS_REDIRECT_MAIN,
        STATUS_REDIRECT_OTHER,
        "COMING_SOON",
    ])
    issue_report_df = report_df[issue_mask].copy()
    primary_redirect_df = report_df[report_df["status"] == STATUS_REDIRECT_MAIN].copy()

    section_label("Franchise issues - sites needing attention")
    render_html_table(
        issue_report_df,
        [
            ("country", "Country"),
            ("domain", "Domain"),
            ("status", "Status"),
            ("note", "Note"),
        ],
    )

    section_label("Inactive franchises - local domain redirecting to lingua-learn.com")
    render_html_table(
        primary_redirect_df,
        [
            ("country", "Country"),
            ("domain", "Domain"),
            ("status", "Status"),
        ],
    )

    section_label("Browse results")
    all_statuses = sorted(report_df["status"].dropna().unique())
    status_filter = st.multiselect("Status", all_statuses, default=all_statuses)
    filtered_report_df = report_df[report_df["status"].isin(status_filter)]

    all_countries = sorted(filtered_report_df["country"].dropna().unique())
    country_filter = st.multiselect("Country", all_countries, default=all_countries)
    filtered_report_df = filtered_report_df[filtered_report_df["country"].isin(country_filter)]

    st.dataframe(
        filtered_report_df[["country", "domain", "url", "status", "code", "note"]],
        use_container_width=True,
        hide_index=True,
        column_config={
            "country": st.column_config.TextColumn("Country", width="small"),
            "domain": st.column_config.TextColumn("Domain", width="medium"),
            "url": st.column_config.LinkColumn("URL", width="medium"),
            "status": st.column_config.TextColumn("Status", width="small"),
            "code": st.column_config.NumberColumn("Code", width="small"),
            "note": st.column_config.TextColumn("Note", width="large"),
        },
    )

    section_label("Scan history")
    history_rows = []
    for report_path in franchise_report_files[:10]:
        historical_report_df = enrich_results(load_report_csv(report_path))
        historical_summary = compute_summary(historical_report_df)
        history_rows.append(
            {
                "Date": extract_report_timestamp(report_path),
                "Entries": historical_summary["total"],
                "OK": historical_summary["ok"],
                "Redirect -> .com": historical_summary["redirect_main"],
                "Redirect local": historical_summary["redirect_other"],
                "Issues": historical_summary["issues"],
            }
        )
    st.dataframe(pd.DataFrame(history_rows), use_container_width=True, hide_index=True)


if __name__ == "__main__":
    main()
