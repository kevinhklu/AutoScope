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


def read_measurement(scope: Scope, meas_type: str, source: str) -> Measurement:
    """
    Read an IMMed measurement from the frame ALREADY in acquisition memory.
    """

    scope.write(f"MEASUrement:IMMed:TYPe {meas_type}")
    scope.write(f"MEASUrement:IMMed:SOURCE1 {source}")

    raw = float(scope.query("MEASUrement:IMMed:VALue?"))
    units = scope.query("MEASUrement:IMMed:UNIts?").strip('"')

    if is_invalid(raw):
        return Measurement(meas_type, source, None, units, valid=False,
                           note="scope returned 9.9e37 (flat/off-screen/no edges)")
    return Measurement(meas_type, source, raw, units, valid=True)


def immediate_single_source(scope: Scope, meas_type: str, source: str) -> Measurement:
    """
    Take ONE synced AUTO acquisition, then read. For standalone amplitude
    measurements on a continuous signal (Vpp, Vmean on a function-gen sine).
    Do NOT use on a triggered I2C frame — single_acquisition() would overwrite
    the captured transaction. Use triggered_single() + read_measurement() there.
    """
    scope.single_acquisition()                       # deterministic frame
    return read_measurement(scope, meas_type, source)


def vpp(scope: Scope, source: str = "CH1") -> Measurement:
    return immediate_single_source(scope, "PK2pk", source)


def vmean(scope: Scope, source: str = "CH1") -> Measurement:
    return immediate_single_source(scope, "MEAN", source)

def frequency(scope: Scope, source: str = "CH1") -> Measurement:
    return immediate_single_source(scope, "FREQuency", source)

def period(scope: Scope, source: str = "CH1") -> Measurement:
    return immediate_single_source(scope, "PERiod", source)

def dutycycle(scope: Scope, source: str = "CH1") -> Measurement:
    return immediate_single_source(scope, "PDUty", source)

