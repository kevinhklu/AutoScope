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

from scope_interface import Scope, NoBusActivity
from measurements import read_measurement


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

    print(f"SCL={scl}  SDA={sda}  Vdd={vdd} V  (trigger level {vdd/2:.2f} V)")
    with Scope(resource) as s:
        print("IDN :", s.idn())
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
        print("\nSCL timing (70%=%.2fV, 30%=%.2fV):" % (0.70 * vdd, 0.30 * vdd))
        for name, fn in (("Tscl_fall", scl_fall_time),
                         ("Tscl_high", scl_high_time),
                         ("Tscl_low",  scl_low_time)):
            m = fn(s, scl, vdd)
            if m.valid:
                print(f"  {name:9s}: {m.value * 1e9:8.1f} ns")
            else:
                print(f"  {name:9s}: INVALID ({m.note})")

        print("\nSanity check: HIGH ~= Vdd, LOW ~= 0. If HIGH is well below Vdd\n"
              "or a reading is INVALID, the line may be off-screen or mis-scaled.")
