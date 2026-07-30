"""
measurements.py — built-in (IMMed) scope measurements.

Each returns a Measurement so a None-valued result is impossible to confuse
with a real reading, and the CSV logger can record *why* a read was invalid.
"""

from dataclasses import dataclass
from typing import Optional

from scope_interface import Scope, is_invalid


@dataclass
class Measurement:
    kind: str            # ex: "PK2PK"
    source: str          # ex: "CH1"
    value: Optional[float]   # None when invalid
    units: str
    valid: bool
    note: str = ""


def immediate_single_source(scope: Scope, meas_type: str, source: str) -> Measurement:
    """
    Configure a single-source IMMed measurement, take ONE synced acquisition,
    then read value + units. Screens the 9.9e37 invalid sentinel.

    NOTE on the source keyword: MDO4000-series immediate measurements use
    SOURCE1 (SOURCE2 exists for two-source measurements like DELay). If your
    firmware rejects SOURCE1, it accepts the older 'IMMed:SOUrce'.
    """
    scope.write(f"MEASUrement:IMMed:TYPe {meas_type}")
    scope.write(f"MEASUrement:IMMed:SOURCE1 {source}")

    scope.single_acquisition()   # deterministic frame

    raw = float(scope.query("MEASUrement:IMMed:VALue?"))
    units = scope.query("MEASUrement:IMMed:UNIts?").strip('"')

    if is_invalid(raw):
        return Measurement(meas_type, source, None, units, valid=False,
                           note="scope returned 9.9e37 (flat/off-screen/no edges)")
    return Measurement(meas_type, source, raw, units, valid=True)


def vpp(scope: Scope, source: str = "CH1") -> Measurement:
    """Peak-to-peak voltage. Does NOT depend on reference levels."""
    return immediate_single_source(scope, "PK2pk", source)


def vmean(scope: Scope, source: str = "CH1") -> Measurement:
    """Your already-validated DC mean, re-expressed through the shared path."""
    return immediate_single_source(scope, "MEAN", source)




if __name__ == "__main__":
    import os
    # Bench machine only — needs NI-VISA + the scope. Keeps your unit's serial
    # OUT of the committed code. Set it in the shell before running:
    #   Windows CMD:  set AUTOSCOPE_RESOURCE=USB0::0x0699::0x0456::C013718::INSTR
    #   PowerShell:   $env:AUTOSCOPE_RESOURCE="USB0::0x0699::0x0456::C013718::INSTR"
    RESOURCE = os.environ.get("AUTOSCOPE_RESOURCE")
    if not RESOURCE:
        raise SystemExit(
            "Set AUTOSCOPE_RESOURCE to your scope's VISA string first.\n"
            "(This runs on the Windows bench machine, not the Mac.)"
        )
    with Scope(RESOURCE) as s:
        print("IDN :", s.idn())
        print("OPT :", s.options())          # check for DPOEMBD here
        m = vpp(s, "CH1")
        print(f"Vpp : {m.value} {m.units}  valid={m.valid} {m.note}")
