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
        print("\nSanity check: HIGH ~= Vdd, LOW ~= 0. If HIGH is well below Vdd\n"
              "or the frame is INVALID, the line may be off-screen or mis-scaled.")
