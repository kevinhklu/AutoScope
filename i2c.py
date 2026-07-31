"""
i2c.py — capture and sanity-check a real I2C transaction (no DPOEMBD decode).

This unit has NO serial-bus decode option, so we trigger on SCL activity to
land on an actual transaction, then read levels straight off the raw waveform.
This step just proves we can reliably CATCH traffic and that both lines are
on-screen and DC-coupled — timing (tHD;DAT / tSU;DAT) comes next.

Bench machine only (needs NI-VISA + scope). Configure via env vars:
    set AUTOSCOPE_RESOURCE=USB0::0x0699::0x0456::C013718::INSTR
    set AUTOSCOPE_SCL=CH1
    set AUTOSCOPE_SDA=CH2
    set AUTOSCOPE_VDD=1.8
Run:  py i2c.py
"""

import os
import time

from scope_interface import Scope, NoBusActivity
from measurements import read_measurement


# --- raw-waveform edge math (pure functions, unit-testable without a scope) ---

def find_crossings(times, volts, level):
    """
    Interpolated times where `volts` crosses `level`, each tagged 'rise'/'fall'.
    Linear interpolation between the two straddling samples places the crossing
    sub-sample, so timing resolution beats the raw sample interval.
    """
    out = []
    for i in range(1, len(volts)):
        a, b = volts[i - 1], volts[i]
        if a < level <= b:                                   # rising through level
            frac = (level - a) / (b - a)
            out.append((times[i - 1] + frac * (times[i] - times[i - 1]), "rise"))
        elif a > level >= b:                                 # falling through level
            frac = (a - level) / (a - b)
            out.append((times[i - 1] + frac * (times[i] - times[i - 1]), "fall"))
    return out


def hold_times(scl_t, scl_v, sda_t, sda_v, vdd):
    """
    tHD;DAT per data bit: from the SCL 30% FALLING edge to the moment SDA next
    starts changing. The SDA endpoint is the FIRST crossing of *either* 30% or
    70% after the SCL edge — which automatically selects 70% for a falling SDA
    and 30% for a rising SDA (i.e. the threshold nearest the level it's leaving).
    Only SDA changes before the next SCL falling edge count, so an unchanged bit
    isn't mis-paired with a far-away transition.

    NOTE (confirm with your team): this encodes "30% or 70% = whichever the
    edge reaches first". If your convention is the opposite (the far threshold),
    swap to the LAST crossing before the next SCL fall instead of the first.
    """
    lo, hi = 0.30 * vdd, 0.70 * vdd
    scl_falls = sorted(t for t, d in find_crossings(scl_t, scl_v, lo) if d == "fall")
    sda_events = sorted(
        t for t, _ in find_crossings(sda_t, sda_v, lo) + find_crossings(sda_t, sda_v, hi)
    )
    results = []
    for i, tf in enumerate(scl_falls):
        window_end = scl_falls[i + 1] if i + 1 < len(scl_falls) else float("inf")
        nxt = next((t for t in sda_events if tf < t < window_end), None)
        if nxt is not None:
            results.append(nxt - tf)
    return results


def setup_times(scl_t, scl_v, sda_t, sda_v, vdd):
    """
    tSU;DAT per data bit: from the 70% point on a SDA RISING edge (data 0->1,
    at settled-high) to the subsequent SCL 30% rising edge. The SDA rising edge
    must sit in the SCL-low window before that clock edge (after the previous
    SCL falling edge) so it belongs to this bit's data.

    Only SDA *rising* edges count (per the team's definition) — a falling SDA
    edge crosses 70% at the START of its fall, which is not a valid-data point,
    so it is intentionally excluded rather than producing an inflated tSU.
    """
    lo, hi = 0.30 * vdd, 0.70 * vdd
    scl_rises = sorted(t for t, d in find_crossings(scl_t, scl_v, lo) if d == "rise")
    scl_falls = sorted(t for t, d in find_crossings(scl_t, scl_v, lo) if d == "fall")
    sda_70_rise = sorted(t for t, d in find_crossings(sda_t, sda_v, hi) if d == "rise")
    results = []
    for tr in scl_rises:
        prev_fall = max((t for t in scl_falls if t < tr), default=float("-inf"))
        cand = [t for t in sda_70_rise if prev_fall < t < tr]
        if cand:
            results.append(tr - max(cand))
    return results


def data_timing(scope, scl, sda, vdd):
    """
    Read SCL+SDA waveforms from the captured frame and compute per-bit
    tHD;DAT and tSU;DAT. Returns {'thd': [...], 'tsu': [...]} in seconds.
    """
    scl_t, scl_v = scope.read_waveform(scl)
    sda_t, sda_v = scope.read_waveform(sda)
    return {
        "thd": hold_times(scl_t, scl_v, sda_t, sda_v, vdd),
        "tsu": setup_times(scl_t, scl_v, sda_t, sda_v, vdd),
    }


def set_abs_reflevels(scope, high=None, mid=None, low=None, mid2=None):
    """
    Set measurement reference levels as ABSOLUTE volts (not percent-of-signal).

    Your I2C thresholds are defined as % of Vdd, and Vdd is a fixed 1.8 V rail,
    so absolute levels reproduce them exactly and are immune to edge
    overshoot/ringing skewing the scope's idea of the signal 'top'.

    HIGH/LOW are used by rise/fall-time measurements; MID by pulse-width and
    single-source edge timing; MID2 is the second waveform's mid level for
    two-source DELay measurements (SCL vs SDA).
    """
    scope.write("MEASUrement:REFLevel:METHod ABSolute")
    if high is not None:
        scope.write(f"MEASUrement:REFLevel:ABSolute:HIGH {high:.4g}")
    if mid is not None:
        scope.write(f"MEASUrement:REFLevel:ABSolute:MID {mid:.4g}")
    if low is not None:
        scope.write(f"MEASUrement:REFLevel:ABSolute:LOW {low:.4g}")
    if mid2 is not None:
        scope.write(f"MEASUrement:REFLevel:ABSolute:MID2 {mid2:.4g}")


def scl_fall_time(scope, scl, vdd):
    """Tscl_fall: 70%->30% on SCL falling edge. FALL time spans HIGH->LOW ref."""
    set_abs_reflevels(scope, high=0.70 * vdd, low=0.30 * vdd)
    return read_measurement(scope, "FALL", scl)


def scl_high_time(scope, scl, vdd):
    """Tscl_high: 70% rise -> 70% fall. Positive pulse width at MID=70%."""
    set_abs_reflevels(scope, mid=0.70 * vdd)
    return read_measurement(scope, "PWIdth", scl)


def scl_low_time(scope, scl, vdd):
    """Tscl_low: 30% fall -> 30% rise. Negative pulse width at MID=30%."""
    set_abs_reflevels(scope, mid=0.30 * vdd)
    return read_measurement(scope, "NWIdth", scl)


def capture_transaction(scope, scl, sda, vdd, wait_ms=5000):
    """
    Trigger on an SCL falling edge (start of clocking) to capture a live
    transaction, then read HIGH/LOW on both lines from that same frame.
    Returns a dict of Measurements. Raises NoBusActivity if the bus is idle.
    """
    # Trigger at ~half the rail — comfortably between LOW (~0) and HIGH (~Vdd).
    scope.triggered_single(source=scl, slope="FALL", level=vdd / 2.0,
                           wait_ms=wait_ms)
    # Read from the CAPTURED frame — do NOT re-acquire (would lose the frame).
    return {
        "SCL_high": read_measurement(scope, "HIGH", scl),
        "SCL_low":  read_measurement(scope, "LOW", scl),
        "SDA_high": read_measurement(scope, "HIGH", sda),
        "SDA_low":  read_measurement(scope, "LOW", sda),
    }


if __name__ == "__main__":
    resource = os.environ.get("AUTOSCOPE_RESOURCE")
    if not resource:
        raise SystemExit("Set AUTOSCOPE_RESOURCE first (bench machine only).")
    scl = os.environ.get("AUTOSCOPE_SCL", "CH1")
    sda = os.environ.get("AUTOSCOPE_SDA", "CH2")
    vdd = float(os.environ.get("AUTOSCOPE_VDD", "1.8"))

    delay = float(os.environ.get("AUTOSCOPE_DELAY", "5"))

    print(f"SCL={scl}  SDA={sda}  Vdd={vdd} V  (trigger level {vdd/2:.2f} V)")
    with Scope(resource) as s:
        print("IDN :", s.idn())

        # Give the operator time to get probes on the board before capture.
        if delay > 0:
            print(f"\nProbe now — capturing in {int(delay)} s "
                  f"(set AUTOSCOPE_DELAY to change):")
            for remaining in range(int(delay), 0, -1):
                print(f"  {remaining}...", end="", flush=True)
                time.sleep(1)
            print(" capturing.")

        try:
            results = capture_transaction(s, scl, sda, vdd)
        except NoBusActivity as e:
            raise SystemExit(f"\nNo transaction captured: {e}\n"
                             "Make sure the board is actively driving the bus,\n"
                             "and that SCL/SDA channel assignment is correct.")
        print("\nCaptured a transaction. Levels on the captured frame:")
        for name, m in results.items():
            v = f"{m.value:.3f} {m.units}" if m.valid else f"INVALID ({m.note})"
            print(f"  {name:9s}: {v}")

        # SCL timing on that SAME captured frame (no re-acquire).
        print(f"\nSCL timing (70% = {0.70 * vdd:.2f} V, 30% = {0.30 * vdd:.2f} V):")
        for name, fn in (("Tscl_fall", scl_fall_time),
                         ("Tscl_high", scl_high_time),
                         ("Tscl_low",  scl_low_time)):
            m = fn(s, scl, vdd)
            if m.valid:
                print(f"  {name:9s}: {m.value * 1e9:8.1f} ns")
            else:
                print(f"  {name:9s}: INVALID ({m.note})")

        # I2C data timing from the raw waveforms of the SAME captured frame.
        print("\nI2C data timing (per bit, worst = tightest margin):")
        dt = data_timing(s, scl, sda, vdd)
        for label, key in (("tHD;DAT", "thd"), ("tSU;DAT", "tsu")):
            vals = dt[key]
            if vals:
                allns = ", ".join(f"{v * 1e9:.1f}" for v in vals)
                print(f"  {label}: worst {min(vals) * 1e9:7.1f} ns   "
                      f"(n={len(vals)}: {allns} ns)")
            else:
                print(f"  {label}: no measurable SDA transitions in frame — "
                      f"fix SDA scaling / widen timebase so SDA edges are captured.")

        print("\nSanity check: HIGH ~= Vdd, LOW ~= 0. If HIGH is well below Vdd\n"
              "or a reading is INVALID, the line may be off-screen or mis-scaled.")
