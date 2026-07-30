"""
test_offline.py — exercise measurement LOGIC without any scope or VISA driver.

A MockScope stands in for the real instrument: it records the commands the
code sends and returns canned replies. This verifies the code paths (valid
reading, invalid 9.9e37 screening) without touching hardware.

Run:  python3 test_offline.py
"""

from measurements import immediate_single_source, read_measurement, vpp


class MockScope:
    """Duck-typed stand-in: same methods measurements.py calls on a real Scope."""

    def __init__(self, value: float, units: str = "V", opts: str = "DPOEMBD,BW6"):
        self._value = value
        self._units = units
        self._opts = opts
        self.log = []               # every write/acquire/query, in call order

    @property
    def commands(self):
        return self.log

    # --- interface used by measurements.py ---
    def write(self, cmd):
        self.log.append(cmd)

    def single_acquisition(self):
        self.log.append("<single_acquisition>")

    def query(self, cmd):
        self.log.append(f"?{cmd}")
        if cmd == "*IDN?":
            return "TEKTRONIX,MDO4054C,C099999,CF:FAKE"
        if cmd == "*OPT?":
            return self._opts
        if cmd.startswith("MEASUrement:IMMed:VALue?"):
            return str(self._value)
        if cmd.startswith("MEASUrement:IMMed:UNIts?"):
            return f'"{self._units}"'
        raise AssertionError(f"unexpected query: {cmd}")


def test_valid_reading():
    s = MockScope(value=3.312)
    m = vpp(s, "CH1")
    assert m.valid is True
    assert m.value == 3.312
    assert m.units == "V"
    # confirm the code actually took a synced acquisition before reading:
    assert "<single_acquisition>" in s.commands
    # confirm it set type + source:
    assert "MEASUrement:IMMed:TYPe PK2pk" in s.commands
    assert "MEASUrement:IMMed:SOURCE1 CH1" in s.commands
    print("PASS valid   :", m)


def test_invalid_sentinel():
    s = MockScope(value=9.9e37)          # scope's "bad measurement" value
    m = vpp(s, "CH1")
    assert m.valid is False
    assert m.value is None               # NOT 9.9e37 leaking into a log
    assert "9.9e37" in m.note
    print("PASS invalid :", m)


def test_acquisition_ordering():
    # The value must be READ after the synced acquisition, else it's stale.
    s = MockScope(value=1.0)
    vpp(s, "CH1")
    acq = s.log.index("<single_acquisition>")
    val_read = s.log.index("?MEASUrement:IMMed:VALue?")
    assert acq < val_read
    print("PASS ordering: value is read after acquisition")


def test_read_measurement_does_not_acquire():
    # On a triggered I2C frame, re-acquiring would destroy the transaction.
    # read_measurement() must read the existing frame and NEVER acquire.
    s = MockScope(value=1.4)
    m = read_measurement(s, "HIGH", "CH1")
    assert m.valid and m.value == 1.4
    assert "<single_acquisition>" not in s.log
    print("PASS no-acquire : read_measurement leaves the captured frame intact")


if __name__ == "__main__":
    test_valid_reading()
    test_invalid_sentinel()
    test_acquisition_ordering()
    test_read_measurement_does_not_acquire()
    print("\nAll offline logic tests passed.")
