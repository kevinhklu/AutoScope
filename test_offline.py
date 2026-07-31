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


def test_scl_timing_reflevels():
    from i2c import scl_fall_time, scl_high_time, scl_low_time
    # Fall time: 70%/30% of 1.8V as ABSOLUTE HIGH/LOW refs, measured with FALL.
    s = MockScope(value=30e-9)
    scl_fall_time(s, "CH1", 1.8)
    assert "MEASUrement:REFLevel:METHod ABSolute" in s.log
    assert "MEASUrement:REFLevel:ABSolute:HIGH 1.26" in s.log
    assert "MEASUrement:REFLevel:ABSolute:LOW 0.54" in s.log
    assert "MEASUrement:IMMed:TYPe FALL" in s.log
    assert "<single_acquisition>" not in s.log      # measured on captured frame

    # High time: MID=70% (1.26V), positive pulse width.
    s = MockScope(value=1e-6)
    scl_high_time(s, "CH1", 1.8)
    assert "MEASUrement:REFLevel:ABSolute:MID 1.26" in s.log
    assert "MEASUrement:IMMed:TYPe PWIdth" in s.log

    # Low time: MID=30% (0.54V), negative pulse width.
    s = MockScope(value=1e-6)
    scl_low_time(s, "CH1", 1.8)
    assert "MEASUrement:REFLevel:ABSolute:MID 0.54" in s.log
    assert "MEASUrement:IMMed:TYPe NWIdth" in s.log
    print("PASS scl-timing: fall/high/low use correct absolute 70/30% refs")


def test_edge_crossings_and_data_timing():
    from i2c import find_crossings, hold_times, setup_times

    # find_crossings: interpolates sub-sample, tags direction.
    cr = find_crossings([0, 1, 2, 3, 4], [0, 0, 1.8, 1.8, 0], 0.9)
    assert len(cr) == 2
    assert cr[0][1] == "rise" and abs(cr[0][0] - 1.5) < 1e-9
    assert cr[1][1] == "fall" and abs(cr[1][0] - 3.5) < 1e-9

    # One data bit: SCL falls ~10.4, rises ~29.6; SDA falls (70%@19.6, 30%@20.4).
    vdd = 1.8
    scl_t, scl_v = [0, 9, 11, 29, 31], [1.8, 1.8, 0, 0, 1.8]
    sda_t, sda_v = [0, 19, 21], [1.8, 1.8, 0]

    holds = hold_times(scl_t, scl_v, sda_t, sda_v, vdd)
    setups = setup_times(scl_t, scl_v, sda_t, sda_v, vdd)
    # tHD = first SDA change (70%@19.6) - SCL fall(30%@10.4) = 9.2
    assert len(holds) == 1 and abs(holds[0] - 9.2) < 1e-9
    # tSU = SCL rise(30%@29.6) - preceding SDA 70%(19.6) = 10.0
    assert len(setups) == 1 and abs(setups[0] - 10.0) < 1e-9
    print("PASS data-timing: crossings + tHD/tSU match hand calculation")


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
    test_scl_timing_reflevels()
    test_edge_crossings_and_data_timing()
    test_read_measurement_does_not_acquire()
    print("\nAll offline logic tests passed.")
