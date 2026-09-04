"""
Stage 6a — Run All Validation Stages
----------------------------------------
Convenience wrapper: re-runs Stage 2 through Stage 5 back-to-back as
separate processes (so one stage's failure/exit code never stops the
others from running), producing a fresh, same-run set of CSVs in
reports/ for generate_final_report.py to consolidate.

Run from the project root:
    python src/run_all_stages.py
"""

import os
import sys
import subprocess

SRC_DIR = os.path.dirname(os.path.abspath(__file__))

STAGES = [
    ("Stage 2: Source -> Staging", os.path.join(SRC_DIR, "validations", "source_to_staging.py")),
    ("Stage 3: Staging -> Data Mart", os.path.join(SRC_DIR, "validations", "staging_to_datamart.py")),
    ("Stage 4: Data Mart Aggregates", os.path.join(SRC_DIR, "validations", "datamart_aggregates.py")),
    ("Stage 5: Reporting Layer", os.path.join(SRC_DIR, "validations", "reporting_validation.py")),
]


def main():
    results = []
    for label, path in STAGES:
        print("\n" + "#" * 70)
        print(f"# RUNNING: {label}")
        print("#" * 70)
        proc = subprocess.run([sys.executable, path])
        results.append((label, proc.returncode))

    print("\n" + "=" * 70)
    print("RUN-ALL SUMMARY")
    print("=" * 70)
    for label, code in results:
        status = "COMPLETED (no failures)" if code == 0 else "COMPLETED WITH FAILURES (see stage output above)"
        print(f"  {label}: {status}")
    print("\nAll stage CSVs are in reports/. Run src/generate_final_report.py next "
          "to build the consolidated Excel workbook.")


if __name__ == "__main__":
    main()
