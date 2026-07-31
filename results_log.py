"""
results_log.py — append test measurements to a CSV for traceability.

One row per measurement (long format), so runs are easy to filter/compare in
a spreadsheet — including side-by-side against manual cursor readings during
validation. Header is written once when the file is created.
"""

import csv
import os
from datetime import datetime

FIELDS = [
    "timestamp", "run_id", "measurement", "value_ns",
    "limit_min_ns", "limit_max_ns", "status", "note",
]


class ResultLogger:
    def __init__(self, path: str = "results/measurements.csv"):
        self.path = path
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        self.run_id = datetime.now().strftime("%Y%m%d-%H%M%S")
        if not os.path.exists(path) or os.path.getsize(path) == 0:
            with open(path, "w", newline="") as f:
                csv.writer(f).writerow(FIELDS)

    def log(self, measurement, value_ns, status="",
            limit_min_ns=None, limit_max_ns=None, note=""):
        row = [
            datetime.now().isoformat(timespec="seconds"),
            self.run_id,
            measurement,
            "" if value_ns is None else f"{value_ns:.3f}",
            "" if limit_min_ns is None else limit_min_ns,
            "" if limit_max_ns is None else limit_max_ns,
            status,
            note,
        ]
        with open(self.path, "a", newline="") as f:
            csv.writer(f).writerow(row)
