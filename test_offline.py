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

    # One data bit: SCL falls ~10.4, rises ~29.6; SDA RISES (30%@19.6, 70%@20.4).
    vdd = 1.8
    scl_t, scl_v = [0, 9, 11, 29, 31], [1.8, 1.8, 0, 0, 1.8]
    sda_t, sda_v = [0, 19, 21], [0, 0, 1.8]

    holds = hold_times(scl_t, scl_v, sda_t, sda_v, vdd)
    setups = setup_times(scl_t, scl_v, sda_t, sda_v, vdd)
    # tHD = first SDA change (30%@19.6) - SCL fall(30%@10.4) = 9.2
    assert len(holds) == 1 and abs(holds[0] - 9.2) < 1e-9
    # tSU = SCL rise(30%@29.6) - SDA rising-edge 70%(20.4) = 9.2
    assert len(setups) == 1 and abs(setups[0] - 9.2) < 1e-9

    # A FALLING SDA edge must NOT produce a tSU value (excluded by definition).
    setups_fall = setup_times(scl_t, scl_v, [0, 19, 21], [1.8, 1.8, 0], vdd)
    assert setups_fall == []
    print("PASS data-timing: crossings + tHD/tSU match hand calc; falling-SDA tSU excluded")


def test_config_and_logging():
    import tempfile
    import os as _os
    import csv as _csv
    from config import Config, load_config
    from results_log import ResultLogger

    # evaluate(): PASS / FAIL / NO LIMIT and threshold voltages.
    cfg = Config(resource="x", scl="CH1", sda="CH2", vdd=1.8, probe_delay_s=0,
                 high_pct=70, low_pct=30,
                 limits={"tsu_dat": {"min_ns": 100}, "tscl_fall": {"max_ns": 300}})
    assert cfg.evaluate("tsu_dat", 388e-9).status == "PASS"
    assert cfg.evaluate("tsu_dat", 50e-9).status == "FAIL"       # below min
    assert cfg.evaluate("tscl_fall", 400e-9).status == "FAIL"    # above max
    assert cfg.evaluate("thd_dat", 10e-9).status == "NO LIMIT"   # no limit defined
    assert abs(cfg.high_v - 1.26) < 1e-9 and abs(cfg.low_v - 0.54) < 1e-9

    d = tempfile.mkdtemp()
    cfgpath = _os.path.join(d, "config.yaml")
    with open(cfgpath, "w") as f:
        f.write("scope: {resource: BASE, probe_delay_s: 5}\n"
                "channels: {scl: CH1, sda: CH2}\n"
                "bus: {vdd: 1.8}\n"
                "thresholds: {high_pct: 70, low_pct: 30}\n"
                "limits: {tsu_dat: {min_ns: 100}}\n")
    _os.environ["AUTOSCOPE_VDD"] = "3.3"                          # env override
    try:
        loaded = load_config(cfgpath)
    finally:
        del _os.environ["AUTOSCOPE_VDD"]
    assert loaded.resource == "BASE"
    assert loaded.vdd == 3.3 and abs(loaded.high_v - 2.31) < 1e-9

    # ResultLogger writes a header once, then a data row.
    logpath = _os.path.join(d, "results", "m.csv")
    log = ResultLogger(logpath)
    log.log("Tscl_fall", 19.8, status="PASS", limit_max_ns=300)
    with open(logpath) as f:
        rows = list(_csv.reader(f))
    assert rows[0][0] == "timestamp"
    assert rows[1][2] == "Tscl_fall" and rows[1][6] == "PASS"
    print("PASS config+logging: evaluate, env override, CSV header+row")


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
    test_config_and_logging()
    test_read_measurement_does_not_acquire()
    print("\nAll offline logic tests passed.")
