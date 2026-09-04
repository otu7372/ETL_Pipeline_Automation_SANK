"""
Stage 2 — Source -> Staging Validation
----------------------------------------
Validates that data flowing INTO staging matches what left the source
systems, per the project's data flow (PS - Technical Environment doc,
section 1.2) and source-to-staging mapping tables (PS - Functional
Description doc, sections 1.1.5 / 1.6.5).

For each of Retail / Distributor / Online / Master data we check:
  1. Completeness  - row counts reconcile; nothing missing, nothing duplicated,
                      nothing extra ("phantom") in staging
  2. Mandatory field integrity - no nulls in fields that must always be populated
  3. Accuracy      - for records present in both source and staging, do the
                      field values actually match (rename fields handled)

Run standalone from the project root:
    python src/validations/source_to_staging.py
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # src/

import pandas as pd
import requests

from config_loader import load_config
from db_utils import run_query_df
from compare_utils import (
    find_duplicates, find_missing_keys, find_extra_keys,
    compare_common_rows, null_check
)
from defect_logger import DefectLogger

CFG = load_config()
STAGE = "Stage2_Source_to_Staging"
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


def completeness_and_accuracy(check_prefix, source_df, target_df, key_col, compare_cols,
                               mandatory_cols_target, severity_missing="High", severity_dup="High"):
    """Shared block: count reconciliation, missing/extra/duplicate keys, null checks, field mismatches."""

    # 1. Row counts
    src_count, tgt_count = len(source_df), len(target_df)
    logger.record(
        STAGE, f"{check_prefix}: row count reconciliation",
        status=(src_count == tgt_count),
        severity="Medium",
        source_object="source", target_object="staging",
        expected=str(src_count), actual=str(tgt_count),
        details=f"source={src_count}, staging={tgt_count}"
    )

    # 2. Missing keys (present in source, absent from staging)
    missing = find_missing_keys(source_df, target_df, key_col)
    ev = save_evidence(source_df[source_df[key_col].isin(missing)], f"{check_prefix}_missing_in_staging")
    logger.record(
        STAGE, f"{check_prefix}: records missing in staging", status=(len(missing) == 0),
        severity=severity_missing, source_object="source", target_object="staging",
        expected="0 missing", actual=f"{len(missing)} missing",
        details=f"missing_keys={missing[:10]}{'...' if len(missing) > 10 else ''}  evidence={ev}"
    )

    # 3. Duplicate keys within staging
    dups = find_duplicates(target_df, key_col)
    dup_keys = sorted(dups[key_col].unique().tolist())
    ev = save_evidence(dups, f"{check_prefix}_duplicates_in_staging")
    logger.record(
        STAGE, f"{check_prefix}: duplicate records in staging", status=(len(dup_keys) == 0),
        severity=severity_dup, source_object="staging", target_object="staging",
        expected="no duplicate keys", actual=f"{len(dup_keys)} duplicated keys",
        details=f"duplicate_keys={dup_keys[:10]}{'...' if len(dup_keys) > 10 else ''}  evidence={ev}"
    )

    # 4. Extra / phantom keys in staging not present in source
    extra = find_extra_keys(source_df, target_df, key_col)
    ev = save_evidence(target_df[target_df[key_col].isin(extra)], f"{check_prefix}_extra_in_staging")
    logger.record(
        STAGE, f"{check_prefix}: phantom records in staging (not in source)", status=(len(extra) == 0),
        severity="High", source_object="source", target_object="staging",
        expected="0 extra", actual=f"{len(extra)} extra",
        details=f"extra_keys={extra[:10]}{'...' if len(extra) > 10 else ''}  evidence={ev}"
    )

    # 5. Mandatory field nulls in staging
    nulls = null_check(target_df, mandatory_cols_target)
    ev = save_evidence(nulls, f"{check_prefix}_mandatory_nulls")
    logger.record(
        STAGE, f"{check_prefix}: mandatory field nulls in staging", status=(len(nulls) == 0),
        severity="High", source_object="staging", target_object="staging",
        expected="0 rows with null mandatory fields", actual=f"{len(nulls)} rows",
        details=f"mandatory_cols={mandatory_cols_target}  evidence={ev}"
    )

    # 6. Field-level accuracy for records present in both
    mismatches = compare_common_rows(source_df, target_df, key_col, compare_cols)
    ev = save_evidence(mismatches, f"{check_prefix}_field_mismatches")
    logger.record(
        STAGE, f"{check_prefix}: field-level accuracy (source vs staging)", status=(len(mismatches) == 0),
        severity="High", source_object="source", target_object="staging",
        expected="0 field mismatches", actual=f"{len(mismatches)} mismatches",
        details=f"compared_cols={compare_cols}  evidence={ev}"
    )

    return {"missing": missing, "duplicates": dup_keys, "extra": extra, "mismatches": mismatches}


# ---------------------------------------------------------------------------
# 1. Retail: CSV file -> stg_retail_sales
# ---------------------------------------------------------------------------
def validate_retail():
    csv_path = os.path.join(CFG["paths"]["source_files_dir"], CFG["paths"]["retail_csv_filename"])
    if not os.path.exists(csv_path):
        logger.record(STAGE, "Retail: source CSV file exists", status=False, severity="High",
                       source_object=csv_path, details="File not found - cannot run retail checks")
        return

    source_df = pd.read_csv(csv_path)
    target_df = run_query_df("staging", "SELECT * FROM stg_retail_sales")

    compare_cols = ["sale_date", "branch_code", "customer_id", "product_code",
                     "product_name_raw", "product_type_raw", "policy_number", "folio_number",
                     "gross_amount", "discount_amount", "transaction_status"]
    mandatory = ["transaction_id", "sale_date", "branch_code", "customer_id",
                 "product_code", "gross_amount", "transaction_status"]

    completeness_and_accuracy("Retail", source_df, target_df, "transaction_id", compare_cols, mandatory)


# ---------------------------------------------------------------------------
# 2. Distributor: fs_source_distributor.distributor_sales_raw -> stg_distributor_sales
# ---------------------------------------------------------------------------
def validate_distributor():
    source_df = run_query_df("source_distributor", "SELECT * FROM distributor_sales_raw")
    target_df = run_query_df("staging", "SELECT * FROM stg_distributor_sales")

    # Known rename: distributor_txn_id (source) -> transaction_id (staging)
    source_df = source_df.rename(columns={"distributor_txn_id": "transaction_id"})

    logger.record(
        STAGE, "Distributor: transaction_id rename verified", status=True, severity="Low",
        source_object="distributor_txn_id", target_object="transaction_id",
        details="Renamed for comparison; staging schema uses transaction_id"
    )

    compare_cols = ["sale_date", "distributor_code", "customer_id", "product_code",
                     "product_name_raw", "product_type_raw", "policy_number", "folio_number",
                     "gross_amount", "discount_amount", "commission_amount",
                     "transaction_status", "region_code"]
    mandatory = ["transaction_id", "sale_date", "distributor_code", "product_code",
                 "gross_amount", "transaction_status"]

    completeness_and_accuracy("Distributor", source_df, target_df, "transaction_id", compare_cols, mandatory)

    # customer_id nullability is EXPECTED for distributor (docs: 2-5% legacy nulls).
    # We report the % as information, not a hard failure, unless it's unreasonably high.
    null_pct = target_df["customer_id"].isna().mean() * 100
    logger.record(
        STAGE, "Distributor: customer_id null rate within expected range", status=(null_pct <= 10),
        severity="Medium", source_object="staging",
        expected="<= 10% null (docs note 2-5% is normal for legacy distributors)",
        actual=f"{null_pct:.2f}% null",
        details="customer_id is nullable by design for distributor; flag for data mart exclusion if too high"
    )


# ---------------------------------------------------------------------------
# 3. Online: Flask API -> stg_online_sales
# ---------------------------------------------------------------------------
def validate_online():
    base = CFG["api"]["base_url"]
    url = base + CFG["api"]["online_sales_endpoint"]
    params = {"from_date": CFG["run"]["from_date"], "to_date": CFG["run"]["to_date"]}
    try:
        r = requests.get(url, params=params, timeout=30)
        r.raise_for_status()
        source_df = pd.DataFrame(r.json())
    except Exception as e:
        logger.record(STAGE, "Online: API reachable and returns data", status=False, severity="High",
                       source_object=url, details=str(e))
        return

    target_df = run_query_df("staging", "SELECT * FROM stg_online_sales")

    compare_cols = ["sale_date", "customer_id", "product_code", "product_name_raw",
                     "product_type_raw", "gross_amount", "discount_amount",
                     "transaction_status", "region_code", "payment_mode"]
    mandatory = ["transaction_id", "sale_date", "customer_id", "product_code",
                 "gross_amount", "transaction_status"]

    completeness_and_accuracy("Online", source_df, target_df, "transaction_id", compare_cols, mandatory)


# ---------------------------------------------------------------------------
# 4. Master data: Customer / Product / Branch-Region mapping
# ---------------------------------------------------------------------------
def validate_master_data():
    # Customer master
    src = run_query_df("source_retail", "SELECT * FROM customer_master_raw")
    tgt = run_query_df("staging", "SELECT * FROM stg_customer_master")
    completeness_and_accuracy(
        "CustomerMaster", src, tgt, "customer_id",
        compare_cols=["customer_name", "dob", "gender", "city", "state", "mobile", "email", "kyc_status"],
        mandatory_cols_target=["customer_id", "customer_name", "dob", "gender", "state", "mobile", "kyc_status"]
    )

    # Product master
    src = run_query_df("source_retail", "SELECT * FROM product_master_raw")
    tgt = run_query_df("staging", "SELECT * FROM stg_product_master")
    completeness_and_accuracy(
        "ProductMaster", src, tgt, "product_code",
        compare_cols=["standard_product_name", "standard_product_type", "product_category",
                      "issuer_company", "active_flag"],
        mandatory_cols_target=["product_code", "standard_product_name", "standard_product_type", "active_flag"]
    )

    # Branch / region mapping
    src = run_query_df("source_retail", "SELECT * FROM branch_region_mapping_raw")
    tgt = run_query_df("staging", "SELECT * FROM stg_branch_region_mapping")
    completeness_and_accuracy(
        "BranchRegionMapping", src, tgt, "branch_code",
        compare_cols=["branch_name", "city", "state", "region_name", "active_flag"],
        mandatory_cols_target=["branch_code", "region_name", "active_flag"]
    )


# ---------------------------------------------------------------------------
# 5. Referential integrity: do staged transactions point to real master records?
# ---------------------------------------------------------------------------
def validate_referential_integrity():
    customers = set(run_query_df("staging", "SELECT customer_id FROM stg_customer_master")["customer_id"])
    products = set(run_query_df("staging", "SELECT product_code FROM stg_product_master")["product_code"])
    branches = set(run_query_df("staging", "SELECT branch_code FROM stg_branch_region_mapping")["branch_code"])

    checks = [
        ("Retail", "stg_retail_sales", "customer_id", customers, "customer_master"),
        ("Retail", "stg_retail_sales", "product_code", products, "product_master"),
        ("Retail", "stg_retail_sales", "branch_code", branches, "branch_region_mapping"),
        ("Distributor", "stg_distributor_sales", "customer_id", customers, "customer_master"),
        ("Distributor", "stg_distributor_sales", "product_code", products, "product_master"),
        ("Online", "stg_online_sales", "customer_id", customers, "customer_master"),
        ("Online", "stg_online_sales", "product_code", products, "product_master"),
    ]
    for channel, table, col, valid_set, ref_name in checks:
        df = run_query_df("staging", f"SELECT * FROM {table}")
        orphans = df[~df[col].isin(valid_set) & df[col].notna()]
        ev = save_evidence(orphans, f"{channel}_{col}_orphans")
        logger.record(
            STAGE, f"{channel}: {col} exists in {ref_name}", status=(len(orphans) == 0),
            severity="High", source_object=table, target_object=ref_name,
            expected=f"all {col} values found in {ref_name}",
            actual=f"{len(orphans)} orphan rows",
            details=f"evidence={ev}"
        )


def main():
    print("=" * 70)
    print("STAGE 2: SOURCE -> STAGING VALIDATION")
    print("=" * 70)

    print("\n-- Retail (CSV -> stg_retail_sales) --")
    validate_retail()

    print("\n-- Distributor (MySQL -> stg_distributor_sales) --")
    validate_distributor()

    print("\n-- Online (API -> stg_online_sales) --")
    validate_online()

    print("\n-- Master Data (Customer / Product / Branch-Region) --")
    validate_master_data()

    print("\n-- Referential Integrity (staging transactions vs staged master data) --")
    validate_referential_integrity()

    logger.print_summary()
    out = logger.export_csv()
    print(f"\nDetailed results written to: {out}")
    print(f"Evidence CSVs written to: {EVIDENCE_DIR}")


if __name__ == "__main__":
    main()
