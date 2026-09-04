"""
Stage 1 — Environment & Connectivity Check
-------------------------------------------
Purpose: before we write a single validation rule, confirm that:
  1. config.yaml loads and has real credentials in it
  2. every MySQL database in the pipeline is reachable and has data
  3. the online-sales API is up
  4. the Flask reporting app is up

Run from the project root:
    python src/connectivity_check.py

This does NOT validate any data correctness yet — that's Stage 2 onward.
It only proves the environment is reachable, so later failures are real
data defects and not "I can't connect" noise.
"""

import os
import sys
import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import requests
from config_loader import load_config
from db_utils import table_row_count

CFG = load_config()

# One representative table per database, just to prove connectivity + data presence.
DB_TABLE_CHECKS = {
    "source_retail": ["retail_sales_raw", "customer_master_raw", "product_master_raw", "branch_region_mapping_raw"],
    "source_distributor": ["distributor_sales_raw"],
    "source_online": ["online_sales_raw"],
    "staging": ["stg_retail_sales", "stg_distributor_sales", "stg_online_sales",
                "stg_customer_master", "stg_product_master", "stg_branch_region_mapping"],
    "datamart": ["dm_sales_transaction", "dm_sales_daily_summary", "dm_sales_channel_summary",
                 "dm_sales_region_summary", "dm_sales_product_summary", "dm_executive_sales_summary"],
    "reporting": ["vw_executive_dashboard", "vw_channel_performance", "vw_region_performance",
                  "vw_product_performance", "vw_daily_sales_trend"],
}

results = []  # list of dicts: {check, status, detail}


def log(check, status, detail=""):
    results.append({"check": check, "status": status, "detail": detail})
    marker = "PASS" if status else "FAIL"
    print(f"[{marker}] {check}  {detail}")


def check_databases():
    for db_key, tables in DB_TABLE_CHECKS.items():
        for table in tables:
            try:
                count = table_row_count(db_key, table)
                log(f"MySQL: {db_key}.{table}", True, f"row_count={count}")
            except Exception as e:
                log(f"MySQL: {db_key}.{table}", False, str(e))


def check_api():
    base = CFG["api"]["base_url"]
    health_url = base + CFG["api"]["health_endpoint"]
    try:
        r = requests.get(health_url, timeout=5)
        log("API health endpoint", r.status_code == 200, f"GET {health_url} -> {r.status_code}")
    except Exception as e:
        log("API health endpoint", False, f"GET {health_url} -> {e}")

    sales_url = base + CFG["api"]["online_sales_endpoint"]
    params = {"from_date": CFG["run"]["from_date"], "to_date": CFG["run"]["to_date"]}
    try:
        r = requests.get(sales_url, params=params, timeout=10)
        ok = r.status_code == 200
        detail = f"GET {r.url} -> {r.status_code}"
        if ok:
            try:
                data = r.json()
                detail += f", records_returned={len(data)}"
            except Exception:
                detail += ", (response was not valid JSON)"
        log("API online-sales endpoint", ok, detail)
    except Exception as e:
        log("API online-sales endpoint", False, f"GET {sales_url} -> {e}")


def check_reporting_app():
    base = CFG["reporting_app"]["base_url"]
    for name, path in CFG["reporting_app"]["endpoints"].items():
        url = base + path
        try:
            r = requests.get(url, params={
                "from_date": CFG["run"]["from_date"],
                "to_date": CFG["run"]["to_date"]
            }, timeout=10)
            log(f"Reporting app: {name}", r.status_code == 200, f"GET {url} -> {r.status_code}")
        except Exception as e:
            log(f"Reporting app: {name}", False, f"GET {url} -> {e}")


def write_log_file():
    os.makedirs(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "logs"), exist_ok=True)
    log_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "logs")
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    log_path = os.path.join(log_dir, f"stage1_connectivity_{ts}.log")
    with open(log_path, "w") as f:
        for r in results:
            f.write(f"[{'PASS' if r['status'] else 'FAIL'}] {r['check']}  {r['detail']}\n")
    print(f"\nLog written to: {log_path}")


def main():
    print("=" * 70)
    print("STAGE 1: ENVIRONMENT & CONNECTIVITY CHECK")
    print("=" * 70)

    print("\n-- MySQL databases --")
    check_databases()

    print("\n-- Online Sales API --")
    check_api()

    print("\n-- Flask Reporting App --")
    check_reporting_app()

    total = len(results)
    passed = sum(1 for r in results if r["status"])
    print("\n" + "=" * 70)
    print(f"SUMMARY: {passed}/{total} checks passed")
    print("=" * 70)

    write_log_file()

    if passed != total:
        sys.exit(1)


if __name__ == "__main__":
    main()
