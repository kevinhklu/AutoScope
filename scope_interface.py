"""
scope_interface.py — thin, testable wrapper around a Tektronix MDO4000C via PyVISA.

Design intent: every scripted measurement goes through one deterministic
acquisition path so results are repeatable and comparable to the manual
cursor method. No AUTOSet, no free-running reads.

Adapt open()/close() to match your already-validated connection code.
"""

import pyvisa

TEK_INVALID = 9.9e37
_INVALID_THRESHOLD = 9.8e37


def is_invalid(value: float) -> bool:
    return abs(value) > _INVALID_THRESHOLD


# Tektronix USB vendor id, hex (NI-VISA) and decimal (some backends) forms.
_TEK_VIDS = ("0x0699", "1689")


def find_scope_resource(vendor_ids=_TEK_VIDS):
    """
    Return the VISA resource string of the first connected Tektronix USB scope,
    or None if none is found. Lets the app auto-fill the resource instead of
    making the user type USB0::0x0699::...::INSTR by hand.
    """
    rm = pyvisa.ResourceManager()
    try:
        for r in rm.list_resources():
            u = r.upper()
            if u.startswith("USB") and any(v.upper() in u for v in vendor_ids):
                return r
    finally:
        rm.close()
    return None


class NoBusActivity(Exception):
    """No qualifying trigger edge arrived in time — e.g. an idle I2C bus."""


class Scope:
    def __init__(self, resource: str, timeout_ms: int = 10_000):
        self.resource = resource
        self.timeout_ms = timeout_ms
        self._rm = None
        self._inst = None

    def __enter__(self):
        self._rm = pyvisa.ResourceManager()          # NI-VISA backend
        self._inst = self._rm.open_resource(self.resource)
        self._inst.timeout = self.timeout_ms
        self.write("HEADer OFF")   # no ':CMD ' prefix -> float()able
        return self

    def __exit__(self, *exc):
        # Always hand the scope back in a LIVE state, even if the body crashed.
        if self._inst is not None:
            try:
                self.restore_live()
            except Exception:
                pass
            self._inst.close()
        if self._rm is not None:
            self._rm.close()

    def restore_live(self) -> None:
        """
        Return the scope to continuous, free-running, AUTO-triggered
        acquisition — i.e. the normal front-panel 'Run' state. Call this after
        any scripted single/triggered acquisition so the display keeps updating
        for whoever is standing at the bench.
        """
        self.write("TRIGger:A:MODe AUTO")
        self.write("ACQuire:STOPAfter RUNSTop")   # continuous, not single
        self.write("ACQuire:STATE RUN")

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

    def ensure_channel_on(self, channel: str) -> None:
        """
        Make sure an analog channel is displayed/acquired BEFORE measuring it.

        An IMMed measurement (or CURVe?) on a channel that is turned OFF returns
        the invalid sentinel (9.9e37) / no data — which shows up as 'INVALID'
        even when the scope's own on-screen badge reads fine on a channel that
        happens to be enabled. Enabling an already-on channel is harmless.
        """
        self.write(f"SELect:{channel} ON")

    def single_acquisition(self) -> None:
        """
        Arm one single-sequence acquisition and BLOCK until it completes

        *OPC? returns 1 only after the SEQuence finishes, so the follow-on
        measurement reads a fully-settled, known frame
        """
        self.write("TRIGger:A:MODe AUTO")
        self.write("ACQuire:STOPAfter SEQuence")
        self.write("ACQuire:STATE RUN")
        self.query("*OPC?")   # ensure self.timeout_ms > acquisition time

    def triggered_single(self, source: str, slope: str = "FALL",
                         level: float | None = None,
                         wait_ms: int = 5_000) -> None:
        """
        Arm a NORMAL edge-triggered single acquisition and block until a REAL
        edge fires it

        Raises NoBusActivity if no edge arrives in wait_ms
        """
        self.write("TRIGger:A:MODe NORMal")
        self.write("TRIGger:A:TYPe EDGE")
        self.write(f"TRIGger:A:EDGE:SOURce {source}")
        self.write(f"TRIGger:A:EDGE:SLOpe {slope}")
        if level is not None:
            self.write(f"TRIGger:A:LEVel:{source} {level}")
        self.write("ACQuire:STOPAfter SEQuence")
        self.write("ACQuire:STATE RUN")

        prev_timeout = self._inst.timeout
        self._inst.timeout = wait_ms
        try:
            self.query("*OPC?")
        except pyvisa.errors.VisaIOError as e:
            if e.error_code == pyvisa.constants.StatusCode.error_timeout:
                self.write("ACQuire:STATE STOP")   # disarm the dangling sequence
                raise NoBusActivity(
                    f"No {slope} edge on {source} within {wait_ms} ms "
                    f"bus idle, wrong trigger source/level, or line off-screen."
                ) from None
            raise
        finally:
            self._inst.timeout = prev_timeout

    def read_waveform(self, source: str):
        """
        Return (times, volts) as parallel lists, read from the
        frame CURRENTLY in acquisition memory (call after triggered_single;
        does NOT acquire). Raw 8-bit samples are converted with the WFMOutpre
        preamble — getting this scaling right is the whole game:
            volts = YZEro + YMUlt * (raw - YOFf)
            time  = XZEro + XINcr * sample_index
        """
        self.write(f"DATa:SOUrce {source}")
        self.write("DATa:ENCdg RIBinary")     # signed integer, big-endian
        self.write("DATa:WIDth 1")            # 8-bit (full MDO4000 vertical res)
        self.write("DATa:STARt 1")
        rec = int(float(self.query("HORizontal:RECOrdlength?")))
        self.write(f"DATa:STOP {rec}")

        xincr = float(self.query("WFMOutpre:XINcr?"))
        xzero = float(self.query("WFMOutpre:XZEro?"))
        ymult = float(self.query("WFMOutpre:YMUlt?"))
        yoff = float(self.query("WFMOutpre:YOFf?"))
        yzero = float(self.query("WFMOutpre:YZEro?"))

        # A full-record CURVe? over USB can exceed the normal timeout; give the
        # bulk transfer plenty of headroom, then restore the working timeout.
        prev_timeout = self._inst.timeout
        self._inst.timeout = max(self.timeout_ms, 60_000)
        try:
            raw = self._inst.query_binary_values("CURVe?", datatype="b", container=list)
        finally:
            self._inst.timeout = prev_timeout

        volts = [yzero + ymult * (r - yoff) for r in raw]
        times = [xzero + xincr * i for i in range(len(raw))]
        return times, volts
