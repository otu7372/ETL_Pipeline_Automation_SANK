"""
Stage 5 — Reporting Layer Validation
----------------------------------------
Two layers of validation, matching the data flow's last two hops
(PS - Detailed Context doc, section 1.4 "Reporting Layer"):

  A. fs_reporting views vs fs_datamart tables
     Per database/sql/04_create_reporting_views.sql, every view SHOULD be
     a plain pass-through of its data mart table. If a view has been
     redefined (as vw_executive_dashboard was, per the defect pack) this
     comparison catches it directly.

  B. Live Flask reporting app vs the views/data mart it's supposed to reflect
     We scrape the actual rendered HTML (the same thing a business user
     sees) and compare it two ways:
       - against the SQL the reporting app itself runs (proves the
         template/rendering layer isn't introducing its own bugs)
       - against the true, independently-recomputed grand total straight
         from dm_sales_transaction across ALL channels (proves the
         business-facing number is actually complete/correct, not just
         "matches its own possibly-buggy view")

Run standalone from the project root:
    python src/validations/reporting_validation.py
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # src/

import pandas as pd
import requests
from bs4 import BeautifulSoup

from config_loader import load_config
from db_utils import run_query_df
from compare_utils import compare_aggregates
from defect_logger import DefectLogger

CFG = load_config()
STAGE = "Stage5_Reporting_Layer"
logger = DefectLogger(STAGE)

EVIDENCE_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
                             "reports", "evidence")
os.makedirs(EVIDENCE_DIR, exist_ok=True)

FROM_DATE = CFG["run"]["from_date"]
TO_DATE = CFG["run"]["to_date"]


def save_evidence(df: pd.DataFrame, name: str) -> str:
    if df is None or df.empty:
        return ""
    path = os.path.join(EVIDENCE_DIR, f"{name}.csv")
    df.to_csv(path, index=False)
    return path


def report_aggregate_result(check_prefix, key_cols, value_cols, missing, extra, mismatches, severity="High"):
    ev1 = save_evidence(missing, f"{check_prefix}_missing_groups")
    logger.record(STAGE, f"{check_prefix}: no missing groups", status=(len(missing) == 0), severity=severity,
                  actual=f"{len(missing)} missing", details=f"evidence={ev1}")
    ev2 = save_evidence(extra, f"{check_prefix}_extra_groups")
    logger.record(STAGE, f"{check_prefix}: no extra/phantom groups", status=(len(extra) == 0), severity="Medium",
                  actual=f"{len(extra)} extra", details=f"evidence={ev2}")
    ev3 = save_evidence(mismatches, f"{check_prefix}_value_mismatches")
    logger.record(STAGE, f"{check_prefix}: values reconcile", status=(len(mismatches) == 0), severity=severity,
                  actual=f"{len(mismatches)} mismatches", details=f"value_cols={value_cols}  evidence={ev3}")


# ---------------------------------------------------------------------------
# A. Views vs Data Mart
# ---------------------------------------------------------------------------
def validate_views_vs_datamart():
    # Executive dashboard view vs its data mart table
    dm = run_query_df("datamart", "SELECT * FROM dm_executive_sales_summary")
    vw = run_query_df("reporting", "SELECT * FROM vw_executive_dashboard")
    for d in (dm, vw):
        d["sale_date"] = d["sale_date"].astype(str)
    value_cols = ["total_transactions", "total_insurance_premium", "total_mutual_fund_sales",
                  "total_net_sales_amount", "avg_ticket_size"]
    missing, extra, mism = compare_aggregates(dm, vw, ["sale_date"], value_cols)
    report_aggregate_result("vw_executive_dashboard_vs_datamart", ["sale_date"], value_cols, missing, extra, mism)

    # Channel / Region / Product / Daily-trend views: expected to be exact pass-throughs
    checks = [
        ("vw_channel_performance", "dm_sales_channel_summary",
         ["sale_date", "source_channel", "standard_product_type"],
         ["transaction_count", "total_gross_sales_amount", "total_net_sales_amount", "avg_ticket_size"]),
        ("vw_region_performance", "dm_sales_region_summary",
         ["sale_date", "region_name", "standard_product_type"],
         ["transaction_count", "total_gross_sales_amount", "total_net_sales_amount", "avg_ticket_size"]),
        ("vw_product_performance", "dm_sales_product_summary",
         ["sale_date", "product_code", "standard_product_name", "standard_product_type", "product_category"],
         ["transaction_count", "total_gross_sales_amount", "total_net_sales_amount", "avg_ticket_size"]),
        ("vw_daily_sales_trend", "dm_sales_daily_summary",
         ["sale_date"],
         ["transaction_count", "total_gross_sales_amount", "total_net_sales_amount",
          "total_insurance_premium", "total_mutual_fund_sales", "avg_ticket_size"]),
    ]
    for view_name, dm_table, key_cols, value_cols in checks:
        dm_df = run_query_df("datamart", f"SELECT * FROM {dm_table}")
        vw_df = run_query_df("reporting", f"SELECT * FROM {view_name}")
        dm_df["sale_date"] = dm_df["sale_date"].astype(str)
        vw_df["sale_date"] = vw_df["sale_date"].astype(str)
        missing, extra, mism = compare_aggregates(dm_df, vw_df, key_cols, value_cols)
        report_aggregate_result(f"{view_name}_vs_{dm_table}", key_cols, value_cols, missing, extra, mism)


# ---------------------------------------------------------------------------
# B1. Live Executive Dashboard page vs the SQL the app itself runs
# ---------------------------------------------------------------------------
def scrape_executive_dashboard():
    base = CFG["reporting_app"]["base_url"]
    url = base + CFG["reporting_app"]["endpoints"]["executive_dashboard"]
    r = requests.get(url, params={"from_date": FROM_DATE, "to_date": TO_DATE}, timeout=20)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")

    label_map = {
        "Total Sales": "total_net_sales_amount",
        "Insurance Premium": "total_insurance_premium",
        "Mutual Fund Sales": "total_mutual_fund_sales",
        "Transactions": "total_transactions",
        "Average Ticket": "avg_ticket_size",
    }
    scraped = {}
    for card in soup.select(".metrics > div"):
        label = card.find("label")
        span = card.find("span")
        if label is None or span is None:
            continue
        key = label_map.get(label.get_text(strip=True))
        if key:
            try:
                scraped[key] = float(span.get_text(strip=True))
            except ValueError:
                scraped[key] = None
    return scraped


def query_app_style_executive_totals():
    """Reproduce exactly the SQL reporting_app/app.py runs for the exec dashboard cards."""
    sql = """
        SELECT
            SUM(total_transactions) total_transactions,
            SUM(total_insurance_premium) total_insurance_premium,
            SUM(total_mutual_fund_sales) total_mutual_fund_sales,
            SUM(total_net_sales_amount) total_net_sales_amount,
            ROUND(SUM(total_net_sales_amount) / NULLIF(SUM(total_transactions),0), 2) avg_ticket_size
        FROM vw_executive_dashboard
        WHERE sale_date BETWEEN %(from_date)s AND %(to_date)s
    """
    df = run_query_df("reporting", sql, params={"from_date": FROM_DATE, "to_date": TO_DATE})
    return df.iloc[0].to_dict() if not df.empty else {}


def compute_true_grand_totals():
    """Ground truth: recompute straight from dm_sales_transaction, ALL channels, ACTIVE only."""
    sql = f"""
        SELECT
            COUNT(*) total_transactions,
            SUM(CASE WHEN standard_product_type='INSURANCE' THEN net_sales_amount ELSE 0 END) total_insurance_premium,
            SUM(CASE WHEN standard_product_type='MUTUAL_FUND' THEN net_sales_amount ELSE 0 END) total_mutual_fund_sales,
            SUM(net_sales_amount) total_net_sales_amount,
            ROUND(SUM(net_sales_amount) / NULLIF(COUNT(*),0), 2) avg_ticket_size
        FROM dm_sales_transaction
        WHERE transaction_status='ACTIVE' AND sale_date BETWEEN '{FROM_DATE}' AND '{TO_DATE}'
    """
    df = run_query_df("datamart", sql)
    return df.iloc[0].to_dict() if not df.empty else {}


def validate_executive_dashboard_page():
    scraped = scrape_executive_dashboard()
    app_sql_totals = query_app_style_executive_totals()
    true_totals = compute_true_grand_totals()

    cols = ["total_transactions", "total_insurance_premium", "total_mutual_fund_sales",
            "total_net_sales_amount", "avg_ticket_size"]

    # B1: does the rendered page match the SQL the app itself ran? (template/rendering bug check)
    render_mismatches = []
    for c in cols:
        expected = round(float(app_sql_totals.get(c) or 0), 2)
        actual = round(float(scraped.get(c) or 0), 2) if scraped.get(c) is not None else None
        if actual is None or abs(expected - actual) > 0.01:
            render_mismatches.append({"metric": c, "app_sql_value": expected, "rendered_page_value": actual})
    ev = save_evidence(pd.DataFrame(render_mismatches), "executive_dashboard_render_mismatches")
    logger.record(
        STAGE, "Executive dashboard: rendered page matches its own backing SQL", status=(len(render_mismatches) == 0),
        severity="Medium", expected="page values == app's own query results",
        actual=f"{len(render_mismatches)} mismatches", details=f"evidence={ev}"
    )

    # B2: does the dashboard show the TRUE business total across all channels? (the real bug we're hunting)
    business_mismatches = []
    for c in cols:
        expected = round(float(true_totals.get(c) or 0), 2)
        actual = round(float(scraped.get(c) or 0), 2) if scraped.get(c) is not None else None
        if actual is None or abs(expected - actual) > 0.01:
            business_mismatches.append({
                "metric": c, "true_value_all_channels": expected, "dashboard_shows": actual,
                "difference": None if actual is None else round(actual - expected, 2)
            })
    ev = save_evidence(pd.DataFrame(business_mismatches), "executive_dashboard_business_totals_mismatches")
    logger.record(
        STAGE, "Executive dashboard: totals include ALL channels (matches true grand total)",
        status=(len(business_mismatches) == 0), severity="High",
        expected="dashboard total == recomputed total from dm_sales_transaction, all channels",
        actual=f"{len(business_mismatches)} metric(s) understated/overstated",
        details=f"evidence={ev}  scraped={scraped}"
    )


# ---------------------------------------------------------------------------
# B2. Table-based report pages vs their views
# ---------------------------------------------------------------------------
def scrape_table_page(endpoint_key):
    base = CFG["reporting_app"]["base_url"]
    url = base + CFG["reporting_app"]["endpoints"][endpoint_key]
    r = requests.get(url, params={"from_date": FROM_DATE, "to_date": TO_DATE}, timeout=20)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")
    table = soup.find("table", id="report-table")
    if table is None:
        return pd.DataFrame()

    header_cells = table.find("thead").find_all("th") if table.find("thead") else []
    headers = [th.get_text(strip=True).lower().replace(" ", "_") for th in header_cells]
    if not headers:
        return pd.DataFrame()

    rows = []
    body = table.find("tbody")
    if body is not None:
        for tr in body.find_all("tr"):
            cells = [td.get_text(strip=True) for td in tr.find_all("td")]
            if cells:
                rows.append(cells)

    df = pd.DataFrame(rows, columns=headers)
    # Numeric columns come through as text from HTML; coerce anything that looks numeric.
    for col in df.columns:
        coerced = pd.to_numeric(df[col], errors="coerce")
        if coerced.notna().all():
            df[col] = coerced
    return df


def validate_table_pages():
    page_checks = [
        ("channel_performance", "vw_channel_performance",
         ["sale_date", "source_channel", "standard_product_type"], "transaction_count", "total_net_sales_amount"),
        ("region_performance", "vw_region_performance",
         ["sale_date", "region_name", "standard_product_type"], "transaction_count", "total_net_sales_amount"),
        ("product_performance", "vw_product_performance",
         ["sale_date", "product_code", "standard_product_name", "standard_product_type"],
         "transaction_count", "total_net_sales_amount"),
    ]
    # NOTE: channel/region/product report pages group by dimension only (not sale_date) per
    # reporting_app/app.py - so we compare grand totals per dimension, not per-date rows.
    for endpoint_key, view_name, dim_cols, count_col, amount_col in page_checks:
        try:
            scraped = scrape_table_page(endpoint_key)
        except Exception as e:
            logger.record(STAGE, f"{endpoint_key} page: scrape succeeded", status=False, severity="High",
                           details=str(e))
            continue

        group_dims = [c for c in dim_cols if c != "sale_date"]
        vw_df = run_query_df("reporting", f"SELECT * FROM {view_name} "
                                           f"WHERE sale_date BETWEEN '{FROM_DATE}' AND '{TO_DATE}'")
        expected = vw_df.groupby(group_dims).agg(
            transaction_count=(count_col, "sum"),
            total_net_sales_amount=(amount_col, "sum"),
        ).reset_index()

        if scraped.empty:
            logger.record(STAGE, f"{endpoint_key} page: rows rendered", status=False, severity="High",
                           actual="0 rows scraped from page")
            continue

        missing, extra, mism = compare_aggregates(
            expected, scraped, group_dims, ["transaction_count", "total_net_sales_amount"]
        )
        report_aggregate_result(f"{endpoint_key}_page_vs_view", group_dims,
                                 ["transaction_count", "total_net_sales_amount"], missing, extra, mism)


def main():
    print("=" * 70)
    print("STAGE 5: REPORTING LAYER VALIDATION")
    print("=" * 70)

    print("\n-- A. fs_reporting views vs fs_datamart tables --")
    validate_views_vs_datamart()

    print("\n-- B1. Live Executive Dashboard page --")
    validate_executive_dashboard_page()

    print("\n-- B2. Live table-based report pages --")
    validate_table_pages()

    logger.print_summary()
    out = logger.export_csv()
    print(f"\nDetailed results written to: {out}")
    print(f"Evidence CSVs written to: {EVIDENCE_DIR}")


if __name__ == "__main__":
    main()
