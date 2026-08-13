"""
config.py — measurement settings and limit evaluation.

All values are supplied by the GUI at runtime; there is no config file or
environment-variable loading.
"""

from dataclasses import dataclass, field


@dataclass
class LimitCheck:
    status: str            # "PASS", "FAIL", or "NO LIMIT"
    min_ns: float | None
    max_ns: float | None


# Fast-mode (400 kHz) I2C defaults — shown in the GUI on startup.
DEFAULT_LIMITS: dict[str, dict[str, float]] = {
    "tscl_fall": {"max_ns": 300},
    "scl_high": {"min_ns": 600},
    "scl_low": {"min_ns": 600},
    "thd_dat": {"min_ns": 0},
    "tsu_dat": {"min_ns": 100},
}


@dataclass
class Config:
    resource: str
    scl: str = "CH1"
    sda: str = "CH2"
    vdd: float = 1.8
    probe_delay_s: float = 5.0
    high_pct: float = 70.0
    low_pct: float = 30.0
    limits: dict = field(default_factory=lambda: dict(DEFAULT_LIMITS))

    @property
    def high_v(self) -> float:
        return self.vdd * self.high_pct / 100.0

    @property
    def low_v(self) -> float:
        return self.vdd * self.low_pct / 100.0

    def evaluate(self, key: str, value_s: float) -> LimitCheck:
        """Compare a measurement (in seconds) against its ns limits."""
        lim = self.limits.get(key)
        if not lim:
            return LimitCheck("NO LIMIT", None, None)
        min_ns = lim.get("min_ns")
        max_ns = lim.get("max_ns")
        value_ns = value_s * 1e9
        failed = (min_ns is not None and value_ns < min_ns) or \
                 (max_ns is not None and value_ns > max_ns)
        return LimitCheck("FAIL" if failed else "PASS", min_ns, max_ns)
