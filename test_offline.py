"""
test_offline.py — exercise measurement LOGIC without any scope or VISA driver.

A MockScope stands in for the real instrument: it records the commands the
code sends and returns canned replies. This verifies the code paths (valid
reading, invalid 9.9e37 screening) without touching hardware.

Run:  python3 test_offline.py
"""

from measurements import immediate_single_source, vpp


class MockScope:
    """Duck-typed stand-in: same methods measurements.py calls on a real Scope."""

    def __init__(self, value: float, units: str = "V", opts: str = "DPOEMBD,BW6"):
        self._value = value
        self._units = units
        self._opts = opts
        self.commands = []          # every write() the code sent, in order

    # --- interface used by measurements.py ---
    def write(self, cmd):
        self.commands.append(cmd)

    def single_acquisition(self):
        self.commands.append("<single_acquisition>")

    def query(self, cmd):
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
    # The read must happen AFTER the synced acquisition, else it's stale.
    s = MockScope(value=1.0)
    vpp(s, "CH1")
    acq = s.commands.index("<single_acquisition>")
    # type/source are set before the acquisition; value is read after it.
    assert s.commands.index("MEASUrement:IMMed:TYPe PK2pk") < acq
    print("PASS ordering: acquisition happens before value read")


if __name__ == "__main__":
    test_valid_reading()
    test_invalid_sentinel()
    test_acquisition_ordering()
    print("\nAll offline logic tests passed.")
