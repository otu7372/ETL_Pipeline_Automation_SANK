"""
defect_logger.py
-----------------
A single, shared place to record every check result across all stages
(Stage 2 through Stage 6). Each validation script imports DefectLogger,
records PASS/FAIL rows against it, and at the end of a run we export it
to CSV (always) — Stage 6 later turns the accumulated log into the
final Excel workbook + defect log deliverable.

Design goal: every stage's script should be able to run standalone
(for fast iteration) AND contribute to one combined report.
"""

import os
import csv
import datetime

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPORTS_DIR = os.path.join(PROJECT_ROOT, "reports")
LOGS_DIR = os.path.join(PROJECT_ROOT, "logs")

FIELDS = [
    "timestamp", "stage", "check_name", "severity", "status",
    "source_object", "target_object", "expected", "actual", "details"
]


class DefectLogger:
    def __init__(self, run_name: str):
        self.run_name = run_name
        self.rows = []
        os.makedirs(REPORTS_DIR, exist_ok=True)
        os.makedirs(LOGS_DIR, exist_ok=True)

    def record(self, stage, check_name, status, severity="Medium",
               source_object="", target_object="", expected="", actual="", details=""):
        """
        status: True (PASS) or False (FAIL)
        severity: High / Medium / Low  (only meaningful for FAIL rows)
        """
        row = {
            "timestamp": datetime.datetime.now().isoformat(timespec="seconds"),
            "stage": stage,
            "check_name": check_name,
            "severity": severity if not status else "-",
            "status": "PASS" if status else "FAIL",
            "source_object": source_object,
            "target_object": target_object,
            "expected": expected,
            "actual": actual,
            "details": details,
        }
        self.rows.append(row)
        marker = "PASS" if status else "FAIL"
        print(f"[{marker}] ({stage}) {check_name} :: {details}")

    def summary(self):
        total = len(self.rows)
        passed = sum(1 for r in self.rows if r["status"] == "PASS")
        failed = total - passed
        high = sum(1 for r in self.rows if r["status"] == "FAIL" and r["severity"] == "High")
        return {"total": total, "passed": passed, "failed": failed, "high_severity_failures": high}

    def export_csv(self, filename: str = None) -> str:
        if filename is None:
            ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
            filename = f"{self.run_name}_{ts}.csv"
        path = os.path.join(REPORTS_DIR, filename)
        with open(path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=FIELDS)
            writer.writeheader()
            writer.writerows(self.rows)
        return path

    def print_summary(self):
        s = self.summary()
        print("\n" + "=" * 70)
        print(f"SUMMARY [{self.run_name}]: {s['passed']}/{s['total']} passed, "
              f"{s['failed']} failed ({s['high_severity_failures']} high severity)")
        print("=" * 70)
