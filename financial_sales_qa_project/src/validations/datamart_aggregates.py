"""
Stage 4 — Data Mart Aggregate Validation
------------------------------------------
Validates dm_sales_daily_summary, dm_sales_channel_summary,
dm_sales_region_summary, dm_sales_product_summary and
dm_executive_sales_summary by independently recomputing each from
dm_sales_transaction (mirroring pipeline/transform_datamart.sql's own
aggregation logic: WHERE transaction_status='ACTIVE', GROUP BY the
relevant dimensions) and comparing to what's actually stored.

Why this matters: Stage 3 found several ACTIVE dm_sales_transaction rows
were corrupted (region, product type, net amount) by post-hoc UPDATEs.
If the summary tables were never re-aggregated after those updates, they
will now be stale relative to the fact table - this stage is designed to
surface exactly that class of defect, in addition to any pure aggregation
math bugs.

Run standalone from the project root:
    python src/validations/datamart_aggregates.py
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # src/

import pandas as pd

from db_utils import run_query_df
from compare_utils import compare_aggregates
from defect_logger import DefectLogger

STAGE = "Stage4_Datamart_Aggregates"
logger = DefectLogger(STAGE)

EVIDENCE_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
                             "reports", "evidence")
os.makedirs(EVIDENCE_DIR, exist_ok=True)


def save_evidence(df: pd.DataFrame, name: str) -> str:
    if df is None or df.empty:
        return ""
    path = os.path.join(EVIDENCE_DIR, f"{name}.csv")
    df.to_csv(path, index=False)
    return path


def report_aggregate_result(check_prefix, key_cols, value_cols, missing, extra, mismatches):
    ev_missing = save_evidence(missing, f"{check_prefix}_stale_missing_groups")
    logger.record(
        STAGE, f"{check_prefix}: no stale/missing aggregate groups", status=(len(missing) == 0),
        severity="High", expected="every group in fact table has a summary row",
        actual=f"{len(missing)} groups missing from summary table",
        details=f"key_cols={key_cols}  evidence={ev_missing}  "
                f"(a miss here usually means the summary wasn't refreshed after the fact table changed)"
    )

    ev_extra = save_evidence(extra, f"{check_prefix}_phantom_groups")
    logger.record(
        STAGE, f"{check_prefix}: no phantom aggregate groups", status=(len(extra) == 0),
        severity="Medium", expected="0 phantom groups", actual=f"{len(extra)} phantom groups",
        details=f"evidence={ev_extra}"
    )

    ev_mismatch = save_evidence(mismatches, f"{check_prefix}_value_mismatches")
    logger.record(
        STAGE, f"{check_prefix}: aggregate values match recomputed truth", status=(len(mismatches) == 0),
        severity="High", expected="0 value mismatches", actual=f"{len(mismatches)} mismatches",
        details=f"value_cols={value_cols}  evidence={ev_mismatch}"
    )


def get_active_transactions() -> pd.DataFrame:
    df = run_query_df("datamart", "SELECT * FROM dm_sales_transaction WHERE transaction_status='ACTIVE'")
    df["sale_date"] = df["sale_date"].astype(str)
    return df


# ---------------------------------------------------------------------------
# 1. Daily summary
# ---------------------------------------------------------------------------
def validate_daily_summary(txn: pd.DataFrame):
    grp = txn.groupby("sale_date").agg(
        transaction_count=("sales_transaction_id", "count"),
        total_gross_sales_amount=("gross_sales_amount", "sum"),
        total_net_sales_amount=("net_sales_amount", "sum"),
    ).reset_index()
    ins = txn[txn["standard_product_type"] == "INSURANCE"].groupby("sale_date")["net_sales_amount"].sum()
    mf = txn[txn["standard_product_type"] == "MUTUAL_FUND"].groupby("sale_date")["net_sales_amount"].sum()
    grp["total_insurance_premium"] = grp["sale_date"].map(ins).fillna(0)
    grp["total_mutual_fund_sales"] = grp["sale_date"].map(mf).fillna(0)
    grp["avg_ticket_size"] = (grp["total_net_sales_amount"] / grp["transaction_count"]).round(2)

    actual = run_query_df("datamart", "SELECT * FROM dm_sales_daily_summary")
    actual["sale_date"] = actual["sale_date"].astype(str)

    value_cols = ["transaction_count", "total_gross_sales_amount", "total_net_sales_amount",
                  "total_insurance_premium", "total_mutual_fund_sales", "avg_ticket_size"]
    missing, extra, mism = compare_aggregates(grp, actual, ["sale_date"], value_cols)
    report_aggregate_result("DailySummary", ["sale_date"], value_cols, missing, extra, mism)


# ---------------------------------------------------------------------------
# 2. Channel summary
# ---------------------------------------------------------------------------
def validate_channel_summary(txn: pd.DataFrame):
    grp = txn.groupby(["sale_date", "source_channel", "standard_product_type"]).agg(
        transaction_count=("sales_transaction_id", "count"),
        total_gross_sales_amount=("gross_sales_amount", "sum"),
        total_net_sales_amount=("net_sales_amount", "sum"),
    ).reset_index()
    grp["avg_ticket_size"] = (grp["total_net_sales_amount"] / grp["transaction_count"]).round(2)

    actual = run_query_df("datamart", "SELECT * FROM dm_sales_channel_summary")
    actual["sale_date"] = actual["sale_date"].astype(str)

    key_cols = ["sale_date", "source_channel", "standard_product_type"]
    value_cols = ["transaction_count", "total_gross_sales_amount", "total_net_sales_amount", "avg_ticket_size"]
    missing, extra, mism = compare_aggregates(grp, actual, key_cols, value_cols)
    report_aggregate_result("ChannelSummary", key_cols, value_cols, missing, extra, mism)


# ---------------------------------------------------------------------------
# 3. Region summary
# ---------------------------------------------------------------------------
def validate_region_summary(txn: pd.DataFrame):
    grp = txn.groupby(["sale_date", "region_name", "standard_product_type"]).agg(
        transaction_count=("sales_transaction_id", "count"),
        total_gross_sales_amount=("gross_sales_amount", "sum"),
        total_net_sales_amount=("net_sales_amount", "sum"),
    ).reset_index()
    grp["avg_ticket_size"] = (grp["total_net_sales_amount"] / grp["transaction_count"]).round(2)

    actual = run_query_df("datamart", "SELECT * FROM dm_sales_region_summary")
    actual["sale_date"] = actual["sale_date"].astype(str)

    key_cols = ["sale_date", "region_name", "standard_product_type"]
    value_cols = ["transaction_count", "total_gross_sales_amount", "total_net_sales_amount", "avg_ticket_size"]
    missing, extra, mism = compare_aggregates(grp, actual, key_cols, value_cols)
    report_aggregate_result("RegionSummary", key_cols, value_cols, missing, extra, mism)


# ---------------------------------------------------------------------------
# 4. Product summary
# ---------------------------------------------------------------------------
def validate_product_summary(txn: pd.DataFrame):
    grp = txn.groupby(["sale_date", "product_code", "standard_product_name",
                        "standard_product_type", "product_category"]).agg(
        transaction_count=("sales_transaction_id", "count"),
        total_gross_sales_amount=("gross_sales_amount", "sum"),
        total_net_sales_amount=("net_sales_amount", "sum"),
    ).reset_index()
    grp["avg_ticket_size"] = (grp["total_net_sales_amount"] / grp["transaction_count"]).round(2)

    actual = run_query_df("datamart", "SELECT * FROM dm_sales_product_summary")
    actual["sale_date"] = actual["sale_date"].astype(str)

    key_cols = ["sale_date", "product_code", "standard_product_name", "standard_product_type", "product_category"]
    value_cols = ["transaction_count", "total_gross_sales_amount", "total_net_sales_amount", "avg_ticket_size"]
    missing, extra, mism = compare_aggregates(grp, actual, key_cols, value_cols)
    report_aggregate_result("ProductSummary", key_cols, value_cols, missing, extra, mism)


# ---------------------------------------------------------------------------
# 5. Executive summary  (should be a straight pass-through of daily summary)
# ---------------------------------------------------------------------------
def validate_executive_summary():
    daily = run_query_df("datamart", "SELECT * FROM dm_sales_daily_summary")
    daily["sale_date"] = daily["sale_date"].astype(str)
    exec_df = run_query_df("datamart", "SELECT * FROM dm_executive_sales_summary")
    exec_df["sale_date"] = exec_df["sale_date"].astype(str)

    expected = daily.rename(columns={"transaction_count": "total_transactions"})
    value_cols = ["total_transactions", "total_insurance_premium", "total_mutual_fund_sales",
                  "total_net_sales_amount", "avg_ticket_size"]
    missing, extra, mism = compare_aggregates(expected, exec_df, ["sale_date"], value_cols)
    report_aggregate_result("ExecutiveSummary_vs_DailySummary", ["sale_date"], value_cols, missing, extra, mism)


def main():
    print("=" * 70)
    print("STAGE 4: DATA MART AGGREGATE VALIDATION")
    print("=" * 70)

    txn = get_active_transactions()
    print(f"\nLoaded {len(txn)} ACTIVE transactions from dm_sales_transaction as the recomputation base.\n")

    print("-- Daily summary --")
    validate_daily_summary(txn)

    print("\n-- Channel summary --")
    validate_channel_summary(txn)

    print("\n-- Region summary --")
    validate_region_summary(txn)

    print("\n-- Product summary --")
    validate_product_summary(txn)

    print("\n-- Executive summary (vs daily summary pass-through) --")
    validate_executive_summary()

    logger.print_summary()
    out = logger.export_csv()
    print(f"\nDetailed results written to: {out}")
    print(f"Evidence CSVs written to: {EVIDENCE_DIR}")


if __name__ == "__main__":
    main()
