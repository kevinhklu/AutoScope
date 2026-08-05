# AutoScope

Scripted, logged, repeatable bench measurements on Tektronix MDO4054C and MDO2024B oscilloscope, replacing manual cursor reads.

> **Status: prototype.** 

## Install

```
pip install -r requirements.txt
```
Also requires **NI-VISA** installed (from ni.com) to talk to the scope

## Configure

All test settings are in **`config.yaml`**. Edit that file as needed. 

Set the scope's VISA resource string:
```
set AUTOSCOPE_RESOURCE=USB0::0x0699::0x0456::<serial>::INSTR
```
Precedence (highest first): **env vars > `config.local.yaml` > `config.yaml`**.
Env overrides: `AUTOSCOPE_RESOURCE`, `AUTOSCOPE_SCL`, `AUTOSCOPE_SDA`,
`AUTOSCOPE_VDD`, `AUTOSCOPE_DELAY`.

## Run

Basic measurement reading (`*IDN?`, `*OPT?`, and a CH1 Vpp, V DC, Frequency, etc):
```
python measurements.py
```

I2C timing --> captures a live transaction, prints SCL levels, SCL fall/high/low
times, and per-bit tHD;DAT / tSU;DAT with PASS/FAIL against the configured
limits, and appends every result to `results/measurements.csv`:
```
python i2c.py
```

Logic tests (no scope required):
```
python test_offline.py
```

## Files
- `config.yaml` — test configuration: channels, bus voltage, thresholds, spec limits
- `config.py` — loads config (env / config.local.yaml / config.yaml) and evaluates limits
- `scope_interface.py` — VISA connection, deterministic acquisition, raw waveform read
- `measurements.py` — built-in measurements (Vpp, Vmean) and the shared read path
- `i2c.py` — I2C capture and timing (SCL fall/high/low, tHD;DAT, tSU;DAT)
- `results_log.py` — appends measurements to a CSV (one row per measurement)
- `test_offline.py` — hardware-free logic tests
- `results/` — CSV logs (git-ignored)
