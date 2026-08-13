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


class NoBusActivity(Exception):
    """No qualifying trigger edge arrived in time — e.g. an idle I2C bus."""


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
        self.write("HEADer OFF")   # bare query replies (no ':CMD ' prefix) -> float()able
        return self

    def __exit__(self, *exc):
        # Always hand the scope back in a LIVE state, even if the body crashed.
        # A scripted single-sequence leaves the scope STOPped on one frozen
        # frame; without this the bench display looks dead until someone hits
        # Run. Guarded so a restore failure can never mask the real exception.
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
        acquisition — i.e. the normal front-panel 'Run' state.

        Must reset STOPAfter to RUNSTop: after a scripted SEQuence the scope
        stays in single-sequence mode, so front-panel Run only arms one more
        single shot. Combined with NORMAL trigger (no edge), the display looks
        dead until Default Setup. Also put trigger back to AUTO and measurement
        ref levels back to percent so the bench behaves as before the script.
        """
        self.write("ACQuire:STATE STOP")
        self.write("TRIGger:A:TYPe EDGE")
        self.write("TRIGger:A:MODe AUTO")
        self.write("ACQuire:STOPAfter RUNSTop")   # continuous, not single
        # Absolute refs were set for I2C timing; percent is the front-panel default.
        self.write("MEASUrement:REFLevel:METHod PERCent")
        self.write("ACQuire:STATE RUN")

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

    def ensure_channel_on(self, source: str) -> None:
        """Turn on a channel's display so acquisitions/measurements see it."""
        self.write(f"SELect:{source} ON")

    # --- the deterministic acquisition path ------------------------------
    def single_acquisition(self) -> None:
        """
        Arm one single-sequence acquisition and BLOCK until it completes.

        *OPC? returns '1' only after the SEQuence finishes, so the follow-on
        measurement reads a fully-settled, known frame -- the key to
        repeatability and to agreement with a manual cursor read.

        AUTO trigger mode is essential here: it triggers on the signal if one
        is present, and self-triggers if not, so the sequence ALWAYS completes.
        In NORMAL mode the scope waits forever for a qualifying edge, and with
        no trigger *OPC? blocks until the VISA timeout (VI_ERROR_TMO).
        """
        self.write("ACQuire:STATE STOP")
        self.write("TRIGger:A:TYPe EDGE")
        self.write("TRIGger:A:MODe AUTO")
        self.write("ACQuire:STOPAfter SEQuence")
        self.write("*CLS")                        # clear OPC so *OPC? waits on THIS acq
        self.write("ACQuire:STATE RUN")
        self.query("*OPC?")   # blocks; ensure self.timeout_ms > acquisition time

    def triggered_single(self, source: str, slope: str = "FALL",
                         level: float | None = None,
                         wait_ms: int = 5_000) -> None:
        """
        Arm a NORMAL edge-triggered single acquisition and block until a REAL
        edge fires it — it will NOT self-trigger. That's what you want for a
        bursty bus like I2C: trigger on SCL activity so you capture an actual
        transaction, never an idle frame.

        source : trigger channel, e.g. "CH1" (put SCL here to catch clocking)
        slope  : "FALL" or "RISe" — SCL's falling edge marks clock activity
        level  : trigger threshold in volts; None keeps the scope's current
                 level. Set to ~half the bus voltage (e.g. 0.9 for a 1.8V bus).
        wait_ms: how long to wait for traffic before giving up.

        Raises NoBusActivity (not a raw VISA timeout) if no edge arrives in
        wait_ms — i.e. the bus was idle the whole time.
        """
        # Stop before reprogramming trigger — changing trigger while RUNning is
        # unreliable on MDO4000 and can leave a half-armed single sequence.
        self.write("ACQuire:STATE STOP")
        self.ensure_channel_on(source)
        self.write("TRIGger:A:TYPe EDGE")
        self.write("TRIGger:A:EDGE:COUPling DC")
        self.write(f"TRIGger:A:EDGE:SOURce {source}")
        self.write(f"TRIGger:A:EDGE:SLOpe {slope}")
        if level is not None:
            # MDO4000: TRIGger:A:LEVel:CH1 <volts>
            self.write(f"TRIGger:A:LEVel:{source} {level}")
        self.write("TRIGger:A:MODe NORMal")
        self.write("ACQuire:STOPAfter SEQuence")
        self.write("*CLS")
        self.write("ACQuire:STATE RUN")

        # Allow wait_ms for the edge, plus a short margin to finish filling the
        # record after a late trigger (slow time/div needs this).
        prev_timeout = self._inst.timeout
        self._inst.timeout = wait_ms + 2_000
        try:
            self.query("*OPC?")
        except pyvisa.errors.VisaIOError as e:
            if e.error_code == pyvisa.constants.StatusCode.error_timeout:
                self.write("ACQuire:STATE STOP")   # disarm the dangling sequence
                raise NoBusActivity(
                    f"No {slope} edge on {source} within ~{wait_ms} ms — "
                    f"bus idle, wrong trigger source/level, or line off-screen."
                ) from None
            raise
        finally:
            self._inst.timeout = prev_timeout


    def read_waveform(self, source: str):
        """
        Return (times, volts) as parallel lists for `source`, read from the
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
