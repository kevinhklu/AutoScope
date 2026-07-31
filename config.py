"""
config.py — load test configuration and evaluate measurements against limits.

Layered precedence (highest first):
    environment variables  >  config.local.yaml  >  config.yaml
so a tech can keep a base config in git and override per-bench without edits.
"""

import os
from dataclasses import dataclass, field

import yaml


# Env vars that override specific config values (keeps bench workflow working).
_ENV = {
    "AUTOSCOPE_RESOURCE": ("scope", "resource", str),
    "AUTOSCOPE_SCL": ("channels", "scl", str),
    "AUTOSCOPE_SDA": ("channels", "sda", str),
    "AUTOSCOPE_VDD": ("bus", "vdd", float),
    "AUTOSCOPE_DELAY": ("scope", "probe_delay_s", float),
}


@dataclass
class LimitCheck:
    status: str            # "PASS", "FAIL", or "NO LIMIT"
    min_ns: float | None
    max_ns: float | None


@dataclass
class Config:
    resource: str
    scl: str
    sda: str
    vdd: float
    probe_delay_s: float
    high_pct: float
    low_pct: float
    limits: dict = field(default_factory=dict)

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


def _deep_merge(base: dict, over: dict) -> dict:
    """Recursively overlay `over` onto `base` (non-dict values replace)."""
    out = dict(base)
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def load_config(path: str = "config.yaml") -> Config:
    with open(path) as f:
        data = yaml.safe_load(f) or {}

    local = os.path.join(os.path.dirname(path) or ".", "config.local.yaml")
    if os.path.exists(local):
        with open(local) as f:
            data = _deep_merge(data, yaml.safe_load(f) or {})

    # env-var overrides
    for env, (section, keyname, cast) in _ENV.items():
        val = os.environ.get(env)
        if val is not None and val != "":
            sec = data.get(section)
            if not isinstance(sec, dict):        # missing or emptied to None
                sec = data[section] = {}
            sec[keyname] = cast(val)

    # `or {}` guards against a section written but left empty (parses as None)
    scope = data.get("scope") or {}
    channels = data.get("channels") or {}
    bus = data.get("bus") or {}
    thresholds = data.get("thresholds") or {}

    resource = scope.get("resource")
    if not resource:
        raise SystemExit(
            "No scope resource string. Set AUTOSCOPE_RESOURCE, or put it in "
            "config.local.yaml / config.yaml under scope.resource."
        )

    return Config(
        resource=resource,
        scl=channels.get("scl", "CH1"),
        sda=channels.get("sda", "CH2"),
        vdd=float(bus.get("vdd", 1.8)),
        probe_delay_s=float(scope.get("probe_delay_s", 5)),
        high_pct=float(thresholds.get("high_pct", 70)),
        low_pct=float(thresholds.get("low_pct", 30)),
        limits=data.get("limits", {}) or {},
    )
