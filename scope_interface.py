"""
scope_interface.py — thin, testable wrapper around a Tektronix MDO4000C via PyVISA.

Design intent: every scripted measurement goes through one deterministic
acquisition path so results are repeatable and comparable to the manual
cursor method. No AUTOSet, no free-running reads.

Adapt open()/close() to match your already-validated connection code.
"""

import pyvisa

# Tektronix returns this for an invalid / uncomputable measurement.
# It is a normal float on the wire, NOT an error — must be screened.
TEK_INVALID = 9.9e37
_INVALID_THRESHOLD = 9.8e37


def is_invalid(value: float) -> bool:
    return abs(value) > _INVALID_THRESHOLD


class Scope:
    def __init__(self, resource: str, timeout_ms: int = 10_000):
        self.resource = resource
        self.timeout_ms = timeout_ms
        self._rm = None
        self._inst = None

    # --- lifecycle -------------------------------------------------------
    def __enter__(self):
        self._rm = pyvisa.ResourceManager()          # NI-VISA backend
        self._inst = self._rm.open_resource(self.resource)
        self._inst.timeout = self.timeout_ms
        return self

    def __exit__(self, *exc):
        if self._inst is not None:
            self._inst.close()
        if self._rm is not None:
            self._rm.close()

    # --- primitives ------------------------------------------------------
    def write(self, cmd: str) -> None:
        self._inst.write(cmd)

    def query(self, cmd: str) -> str:
        return self._inst.query(cmd).strip()

    def idn(self) -> str:
        return self.query("*IDN?")

    def options(self) -> str:
        """Installed options string. Look for DPOEMBD (I2C/SPI decode)."""
        # *OPT? is the SCPI-standard installed-options query.
        return self.query("*OPT?")

    # --- the deterministic acquisition path ------------------------------
    def single_acquisition(self) -> None:
        """
        Arm one single-sequence acquisition and BLOCK until it completes.

        *OPC? returns '1' only after the SEQuence finishes, so the follow-on
        measurement reads a fully-settled, known frame — the key to
        repeatability and to agreement with a manual cursor read.

        AUTO trigger mode is essential here: it triggers on the signal if one
        is present, and self-triggers if not, so the sequence ALWAYS completes.
        In NORMAL mode the scope waits forever for a qualifying edge, and with
        no trigger *OPC? blocks until the VISA timeout (VI_ERROR_TMO).
        """
        self.write("TRIGger:A:MODe AUTO")
        self.write("ACQuire:STOPAfter SEQuence")
        self.write("ACQuire:STATE RUN")
        self.query("*OPC?")   # blocks; ensure self.timeout_ms > acquisition time
