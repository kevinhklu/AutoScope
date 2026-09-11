"""
results_log.py — append test measurements to an Excel (.xlsx) workbook.

Each CAPTURE (one Acquire or one I2C run) is written as a group of rows — one
row per measurement — sharing a single MERGED capture-number cell and a single
merged timestamp cell. New files get a header row; existing files are appended
to (load-modify-save), so the file must not be open in Excel during a run.
"""

import os
import subprocess
import sys
from datetime import datetime

from openpyxl import Workbook, load_workbook
from openpyxl.styles import Alignment

RESULTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "results")

HEADERS = ["Capture", "Measurement", "Value", "Units",
           "Limit min (ns)", "Limit max (ns)", "Status", "Note", "Timestamp"]
_CAPTURE_COL = 1
_TIMESTAMP_COL = len(HEADERS)   # last column


def resolve_log_path(filename: str) -> str:
    """
    Resolve a user-entered log name to an .xlsx path under results/.

    Any extension is normalized to .xlsx (merged cells need Excel, not CSV).
    Only the basename is used so paths cannot escape the results folder.
    """
    name = filename.strip()
    if not name:
        raise ValueError("Enter a log file name (e.g. measurements.xlsx).")
    base = os.path.splitext(os.path.basename(name))[0]
    os.makedirs(RESULTS_DIR, exist_ok=True)
    return os.path.join(RESULTS_DIR, base + ".xlsx")


def _cell(x):
    """Numbers pass through (Excel stores them numeric); None -> blank."""
    if x is None:
        return ""
    if isinstance(x, float):
        return float(f"{x:.6g}")
    return x


class ResultLogger:
    def __init__(self, path: str):
        self.path = path
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        self.run_id = datetime.now().strftime("%Y%m%d-%H%M%S")

    def open_in_excel(self):
        """Create the workbook if needed, then open it in the default Excel app."""
        if not os.path.exists(self.path) or os.path.getsize(self.path) == 0:
            wb = Workbook()
            ws = wb.active
            ws.title = "measurements"
            ws.append(HEADERS)
            wb.save(self.path)
        if os.name == "nt":
            os.startfile(self.path)  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.Popen(["open", self.path])
        else:
            subprocess.Popen(["xdg-open", self.path])
        return self.path

    def log(self, measurement, value, *, units="", status="",
            limit_min_ns=None, limit_max_ns=None, note=""):
        """Log a single measurement as a one-row capture."""
        return self.log_capture([{
            "measurement": measurement, "value": value, "units": units,
            "status": status, "limit_min_ns": limit_min_ns,
            "limit_max_ns": limit_max_ns, "note": note,
        }])

    def remove_capture(self, capture_number=None):
        """Delete every row belonging to a capture number. If omitted, delete the latest one."""
        if not os.path.exists(self.path) or os.path.getsize(self.path) == 0:
            raise FileNotFoundError(f"No log file exists at {self.path}.")
        wb = load_workbook(self.path)
        ws = wb.active
        if capture_number is None:
            candidates = []
            for row in ws.iter_rows(min_row=2, min_col=_CAPTURE_COL,
                                    max_col=_CAPTURE_COL, values_only=True):
                v = row[0]
                if isinstance(v, int):
                    candidates.append(v)
                elif isinstance(v, str) and v.isdigit():
                    candidates.append(int(v))
            if not candidates:
                return None
            capture_number = max(candidates)
        else:
            capture_number = int(capture_number)

        excluded_rows = set()
        for merged in ws.merged_cells.ranges:
            min_col, min_row, max_col, max_row = merged.min_col, merged.min_row, merged.max_col, merged.max_row
            if min_col == _CAPTURE_COL and max_col == _CAPTURE_COL:
                start_val = ws.cell(min_row, min_col).value
                if start_val == capture_number or (
                    isinstance(start_val, str) and start_val.isdigit() and int(start_val) == capture_number
                ):
                    for row_idx in range(min_row, max_row + 1):
                        excluded_rows.add(row_idx)

        for row_idx in range(2, ws.max_row + 1):
            cell_val = ws.cell(row_idx, _CAPTURE_COL).value
            if cell_val == capture_number or (
                isinstance(cell_val, str) and cell_val.isdigit() and int(cell_val) == capture_number
            ):
                excluded_rows.add(row_idx)

        if not excluded_rows:
            return None

        remaining_rows = []
        for row_idx in range(1, ws.max_row + 1):
            if row_idx in excluded_rows:
                continue
            remaining_rows.append([
                ws.cell(row_idx, col_idx).value for col_idx in range(1, ws.max_column + 1)
            ])

        new_wb = Workbook()
        new_ws = new_wb.active
        new_ws.title = "measurements"
        if not remaining_rows:
            new_ws.append(HEADERS)
        else:
            new_ws.append(list(HEADERS))
            for row in remaining_rows[1:]:
                new_ws.append(row)
        new_wb.save(self.path)
        return capture_number

    def log_capture(self, rows):
        """
        Append one capture (list of measurement dicts) as a group of rows.

        The capture number and timestamp each go in ONE cell merged down the
        group's rows; each measurement gets its own row. A single-measurement
        capture is one row (nothing to merge).
        """
        rows = [r for r in rows if r]
        if not rows:
            return None
        ts = datetime.now().isoformat(timespec="seconds")

        if os.path.exists(self.path) and os.path.getsize(self.path) > 0:
            wb = load_workbook(self.path)
            ws = wb.active
        else:
            wb = Workbook()
            ws = wb.active
            ws.title = "measurements"
            ws.append(HEADERS)

        capture = self._next_capture(ws)
        start = ws.max_row + 1
        for r in rows:
            ws.append([
                capture,
                r.get("measurement", ""),
                _cell(r.get("value")),
                r.get("units", ""),
                _cell(r.get("limit_min_ns")),
                _cell(r.get("limit_max_ns")),
                r.get("status", ""),
                r.get("note", ""),
                ts,
            ])
        end = ws.max_row
        if end > start:   # merge the capture number + timestamp across the group
            for col in (_CAPTURE_COL, _TIMESTAMP_COL):
                ws.merge_cells(start_row=start, start_column=col,
                               end_row=end, end_column=col)
                ws.cell(row=start, column=col).alignment = Alignment(
                    vertical="center")

        try:
            wb.save(self.path)
        except PermissionError as e:
            raise PermissionError(
                f"Could not write {self.path} — is it open in Excel? "
                "Close it and run again."
            ) from e
        return capture

    @staticmethod
    def _next_capture(ws) -> int:
        """Next capture number = max existing (top cell of each merge) + 1."""
        max_cap = 0
        for (v,) in ws.iter_rows(min_row=2, min_col=_CAPTURE_COL,
                                 max_col=_CAPTURE_COL, values_only=True):
            if isinstance(v, int):
                max_cap = max(max_cap, v)
            elif isinstance(v, str) and v.isdigit():
                max_cap = max(max_cap, int(v))
        return max_cap + 1
