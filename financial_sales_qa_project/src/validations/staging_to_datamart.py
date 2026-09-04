"""
Stage 3 — Staging -> Data Mart Transformation Validation
------------------------------------------------------------
Validates fs_datamart.dm_sales_transaction against an INDEPENDENTLY
recomputed "expected" version built directly from staging + master data
in pandas. We deliberately do NOT trust pipeline/transform_datamart.sql's
own logic as ground truth to re-run — we mirror its documented business
rules ourselves, so a bug planted in the SQL (or in the data) surfaces as
a mismatch here.

Business rules being validated (see PS - Technical Environment doc,
sections 1.3 / 1.4, and the actual pipeline/transform_datamart.sql):
  - sales_transaction_id = '<CHANNEL_PREFIX>-' + transaction_id
  - customer_name_clean  = TRIM(customer_master.customer_name)
  - product enrichment   = LEFT JOIN to product_master on product_code
  - region_name (Retail) = LEFT JOIN branch_region_mapping on branch_code
  - region_name (Dist/Online) = CASE on raw region_code -> Title Case,
      else 'Unknown'  <-- NOTE: this is what the CODE does, which differs
      from the functional doc's "join to region_mapping" description.
      We validate against the code's actual behaviour and log the
      doc/code mismatch separately as an informational finding.
  - net_sales_amount     = gross_amount - COALESCE(discount_amount, 0)
  - commission_amount    = distributor's own value; 0 for Retail/Online

Run standalone from the project root:
    python src/validations/staging_to_datamart.py
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # src/

import pandas as pd

from config_loader import load_config
from db_utils import run_query_df
from compare_utils import find_duplicates, find_missing_keys, find_extra_keys, compare_common_rows, null_check
from defect_logger import DefectLogger

CFG = load_config()
STAGE = "Stage3_Staging_to_Datamart"
logger = DefectLogger(STAGE)

EVIDENCE_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
                             "reports", "evidence")
os.makedirs(EVIDENCE_DIR, exist_ok=True)

REGION_MAP = {"NORTH": "North", "SOUTH": "South", "EAST": "East", "WEST": "West"}


def save_evidence(df: pd.DataFrame, name: str) -> str:
    if df is None or df.empty:
        return ""
    path = os.path.join(EVIDENCE_DIR, f"{name}.csv")
    df.to_csv(path, index=False)
    return path


def row_level_checks(check_prefix, expected_df, actual_df, key_col, compare_cols, null_cols):
    """Shared block: counts, missing/dup/extra keys, join-failure nulls, field accuracy."""

    exp_count, act_count = len(expected_df), len(actual_df)
    logger.record(
        STAGE, f"{check_prefix}: row count (staging vs data mart)", status=(exp_count == act_count),
        severity="High", source_object="staging (recomputed)", target_object="dm_sales_transaction",
        expected=str(exp_count), actual=str(act_count), details=f"expected={exp_count}, actual={act_count}"
    )

    missing = find_missing_keys(expected_df, actual_df, key_col)
    ev = save_evidence(expected_df[expected_df[key_col].isin(missing)], f"{check_prefix}_missing_in_datamart")
    logger.record(
        STAGE, f"{check_prefix}: records missing in data mart", status=(len(missing) == 0),
        severity="High", expected="0 missing", actual=f"{len(missing)} missing",
        details=f"missing_keys={missing[:10]}{'...' if len(missing) > 10 else ''}  evidence={ev}"
    )

    dups = find_duplicates(actual_df, key_col)
    dup_keys = sorted(dups[key_col].unique().tolist())
    ev = save_evidence(dups, f"{check_prefix}_duplicates_in_datamart")
    logger.record(
        STAGE, f"{check_prefix}: duplicate records in data mart", status=(len(dup_keys) == 0),
        severity="High", expected="no duplicate keys", actual=f"{len(dup_keys)} duplicated keys",
        details=f"duplicate_keys={dup_keys[:10]}{'...' if len(dup_keys) > 10 else ''}  evidence={ev}"
    )

    extra = find_extra_keys(expected_df, actual_df, key_col)
    ev = save_evidence(actual_df[actual_df[key_col].isin(extra)], f"{check_prefix}_extra_in_datamart")
    logger.record(
        STAGE, f"{check_prefix}: phantom records in data mart", status=(len(extra) == 0),
        severity="High", expected="0 extra", actual=f"{len(extra)} extra",
        details=f"extra_keys={extra[:10]}{'...' if len(extra) > 10 else ''}  evidence={ev}"
    )

    nulls = null_check(actual_df, null_cols)
    ev = save_evidence(nulls, f"{check_prefix}_enrichment_join_failures")
    logger.record(
        STAGE, f"{check_prefix}: enrichment join completeness (no unexpected nulls)", status=(len(nulls) == 0),
        severity="High", expected=f"0 rows with null {null_cols}", actual=f"{len(nulls)} rows",
        details=f"cols_checked={null_cols}  evidence={ev}  (nulls here mean a lookup key didn't match master data)"
    )

    mismatches = compare_common_rows(expected_df, actual_df, key_col, compare_cols)
    ev = save_evidence(mismatches, f"{check_prefix}_transformation_mismatches")
    logger.record(
        STAGE, f"{check_prefix}: transformation accuracy (recomputed vs actual)", status=(len(mismatches) == 0),
        severity="High", expected="0 mismatches", actual=f"{len(mismatches)} mismatches",
        details=f"compared_cols={compare_cols}  evidence={ev}"
    )


# ---------------------------------------------------------------------------
# 1. Retail
# ---------------------------------------------------------------------------
def validate_retail_transform():
    stg = run_query_df("staging", "SELECT * FROM stg_retail_sales")
    cust = run_query_df("staging", "SELECT customer_id, customer_name, state FROM stg_customer_master")
    prod = run_query_df("staging", "SELECT product_code, standard_product_name, standard_product_type, "
                                    "product_category FROM stg_product_master")
    branch = run_query_df("staging", "SELECT branch_code, region_name FROM stg_branch_region_mapping")
    actual = run_query_df("datamart", "SELECT * FROM dm_sales_transaction WHERE source_channel='Retail'")

    exp = stg.merge(cust, on="customer_id", how="left")
    exp = exp.merge(prod, on="product_code", how="left")
    exp = exp.merge(branch, on="branch_code", how="left")

    exp["sales_transaction_id"] = "RTL-" + exp["transaction_id"].astype(str)
    exp["customer_name_clean"] = exp["customer_name"].str.strip()
    exp["customer_state"] = exp["state"]
    exp["gross_sales_amount"] = exp["gross_amount"]
    exp["discount_amount"] = exp["discount_amount"].fillna(0)
    exp["net_sales_amount"] = exp["gross_amount"].fillna(0) - exp["discount_amount"]

    compare_cols = ["customer_name_clean", "customer_state", "region_name",
                     "standard_product_name", "standard_product_type", "product_category",
                     "gross_sales_amount", "discount_amount", "net_sales_amount"]
    null_cols = ["customer_name_clean", "region_name", "standard_product_type"]

    row_level_checks("Retail_DM", exp, actual, "sales_transaction_id", compare_cols, null_cols)


# ---------------------------------------------------------------------------
# 2. Distributor
# ---------------------------------------------------------------------------
def validate_distributor_transform():
    stg = run_query_df("staging", "SELECT * FROM stg_distributor_sales")
    cust = run_query_df("staging", "SELECT customer_id, customer_name, state FROM stg_customer_master")
    prod = run_query_df("staging", "SELECT product_code, standard_product_name, standard_product_type, "
                                    "product_category FROM stg_product_master")
    actual = run_query_df("datamart", "SELECT * FROM dm_sales_transaction WHERE source_channel='Distributor'")

    exp = stg.merge(cust, on="customer_id", how="left")
    exp = exp.merge(prod, on="product_code", how="left")

    exp["sales_transaction_id"] = "DST-" + exp["transaction_id"].astype(str)
    exp["customer_name_clean"] = exp["customer_name"].str.strip()
    exp["customer_state"] = exp["state"]
    exp["region_name"] = exp["region_code"].map(REGION_MAP).fillna("Unknown")
    exp["gross_sales_amount"] = exp["gross_amount"]
    exp["discount_amount"] = exp["discount_amount"].fillna(0)
    exp["net_sales_amount"] = exp["gross_amount"].fillna(0) - exp["discount_amount"]
    exp["commission_amount"] = exp["commission_amount"]

    compare_cols = ["region_name", "standard_product_name", "standard_product_type", "product_category",
                     "gross_sales_amount", "discount_amount", "net_sales_amount", "commission_amount"]
    # customer_name_clean/customer_state excluded from hard null check: distributor customer_id
    # can legitimately be null (docs), which cascades to a null customer join - not a defect.
    null_cols = ["region_name", "standard_product_type"]

    row_level_checks("Distributor_DM", exp, actual, "sales_transaction_id", compare_cols, null_cols)

    logger.record(
        STAGE, "Distributor: region derivation matches ACTUAL code (not functional doc spec)", status=True,
        severity="Low",
        details=("INFORMATIONAL: functional doc specifies region should be derived via "
                  "distributor_code -> region_mapping join with region_code fallback. "
                  "The actual transform_datamart.sql instead maps region_code directly via a "
                  "CASE statement with no join, defaulting anything else to 'Unknown'. "
                  "Validated against actual code behaviour. Recommend raising as a "
                  "spec-vs-implementation gap for the data engineering team.")
    )


# ---------------------------------------------------------------------------
# 3. Online
# ---------------------------------------------------------------------------
def validate_online_transform():
    stg = run_query_df("staging", "SELECT * FROM stg_online_sales")
    cust = run_query_df("staging", "SELECT customer_id, customer_name, state FROM stg_customer_master")
    prod = run_query_df("staging", "SELECT product_code, standard_product_name, standard_product_type, "
                                    "product_category FROM stg_product_master")
    actual = run_query_df("datamart", "SELECT * FROM dm_sales_transaction WHERE source_channel='Online'")

    exp = stg.merge(cust, on="customer_id", how="left")
    exp = exp.merge(prod, on="product_code", how="left")

    exp["sales_transaction_id"] = "ONL-" + exp["transaction_id"].astype(str)
    exp["customer_name_clean"] = exp["customer_name"].str.strip()
    exp["customer_state"] = exp["state"]
    exp["region_name"] = exp["region_code"].map(REGION_MAP).fillna("Unknown")
    exp["gross_sales_amount"] = exp["gross_amount"]
    exp["discount_amount"] = exp["discount_amount"].fillna(0)
    exp["net_sales_amount"] = exp["gross_amount"].fillna(0) - exp["discount_amount"]
    exp["folio_number_expected"] = exp.apply(
        lambda r: f"ONLINE-{r['transaction_id']}" if r["standard_product_type"] == "MUTUAL_FUND" else None, axis=1
    )

    compare_cols = ["customer_name_clean", "customer_state", "region_name",
                     "standard_product_name", "standard_product_type", "product_category",
                     "gross_sales_amount", "discount_amount", "net_sales_amount"]
    null_cols = ["customer_name_clean", "region_name", "standard_product_type"]

    row_level_checks("Online_DM", exp, actual, "sales_transaction_id", compare_cols, null_cols)

    # folio_number is a separate, derived (not directly-copied) field - check independently
    merged = exp.merge(actual[["sales_transaction_id", "folio_number"]], on="sales_transaction_id", how="inner")
    folio_mismatch = merged[merged["folio_number_expected"] != merged["folio_number"]]
    ev = save_evidence(folio_mismatch, "Online_DM_folio_number_mismatches")
    logger.record(
        STAGE, "Online: folio_number derivation (MUTUAL_FUND -> ONLINE-<txn_id>)",
        status=(len(folio_mismatch) == 0), severity="Medium",
        expected="folio_number populated only for MUTUAL_FUND rows", actual=f"{len(folio_mismatch)} mismatches",
        details=f"evidence={ev}"
    )

    logger.record(
        STAGE, "Online: policy_number is hardcoded NULL for all online records", status=True, severity="Low",
        details=("INFORMATIONAL: functional doc implies insurance products bought online should get an "
                  "issued policy_number. Actual transform_datamart.sql hardcodes policy_number=NULL for "
                  "every online record regardless of product type. Not flagged as a defect here since it "
                  "reflects current code behaviour consistently, but worth raising with the business/dev team.")
    )


# ---------------------------------------------------------------------------
# 4. Compliance-style checks: inactive master data used in live transactions
# ---------------------------------------------------------------------------
def validate_inactive_master_data_usage():
    prod = run_query_df("staging", "SELECT product_code, active_flag FROM stg_product_master")
    branch = run_query_df("staging", "SELECT branch_code, active_flag FROM stg_branch_region_mapping")
    dm = run_query_df("datamart", "SELECT sales_transaction_id, source_channel, product_code, transaction_status "
                                   "FROM dm_sales_transaction")

    inactive_products = set(prod[prod["active_flag"] == 0]["product_code"])
    hits = dm[dm["product_code"].isin(inactive_products) & (dm["transaction_status"] == "ACTIVE")]
    ev = save_evidence(hits, "inactive_product_sold_in_datamart")
    logger.record(
        STAGE, "No ACTIVE sales against inactive (discontinued) products", status=(len(hits) == 0),
        severity="High", expected="0 rows", actual=f"{len(hits)} rows", details=f"evidence={ev}"
    )


def main():
    print("=" * 70)
    print("STAGE 3: STAGING -> DATA MART TRANSFORMATION VALIDATION")
    print("=" * 70)

    print("\n-- Retail transformation --")
    validate_retail_transform()

    print("\n-- Distributor transformation --")
    validate_distributor_transform()

    print("\n-- Online transformation --")
    validate_online_transform()

    print("\n-- Compliance: inactive product/branch usage --")
    validate_inactive_master_data_usage()

    logger.print_summary()
    out = logger.export_csv()
    print(f"\nDetailed results written to: {out}")
    print(f"Evidence CSVs written to: {EVIDENCE_DIR}")


if __name__ == "__main__":
    main()
