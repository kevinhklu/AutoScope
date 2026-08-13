"""
results_log.py — append test measurements to a CSV for traceability.

One row per measurement (long format). New files get a header row; existing
files are never overwritten — rows are always appended.
"""

import csv
import os
from datetime import datetime

RESULTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "results")

FIELDS = [
    "timestamp", "run_id", "measurement", "value", "units",
    "limit_min_ns", "limit_max_ns", "status", "note",
]


def resolve_log_path(filename: str) -> str:
    """
    Resolve a user-entered log name to a path under results/.

    Adds .csv when no extension is given. Only the basename is used so paths
    cannot escape the results folder. Creates results/ if needed.
    """
    name = filename.strip()
    if not name:
        raise ValueError("Enter a CSV log file name (e.g. measurements.csv).")
    if name.lower().endswith(".xlsx"):
        raise ValueError(
            "Use a .csv file (Excel opens CSV). True .xlsx append is not supported."
        )
    if not name.lower().endswith(".csv"):
        name += ".csv"
    os.makedirs(RESULTS_DIR, exist_ok=True)
    return os.path.join(RESULTS_DIR, os.path.basename(name))


class ResultLogger:
    def __init__(self, path: str):
        self.path = path
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        self.run_id = datetime.now().strftime("%Y%m%d-%H%M%S")
        if not os.path.exists(path) or os.path.getsize(path) == 0:
            with open(path, "w", newline="") as f:
                csv.writer(f).writerow(FIELDS)

    def log(self, measurement, value, *, units="", status="",
            limit_min_ns=None, limit_max_ns=None, note=""):
        row = [
            datetime.now().isoformat(timespec="seconds"),
            self.run_id,
            measurement,
            "" if value is None else f"{value:.6g}",
            units,
            "" if limit_min_ns is None else limit_min_ns,
            "" if limit_max_ns is None else limit_max_ns,
            status,
            note,
        ]
        with open(self.path, "a", newline="") as f:
            csv.writer(f).writerow(row)
