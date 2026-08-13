# AutoScope

Scripted, logged, repeatable bench measurements on Tektronix MDO4054C and MDO2024B oscilloscopes.

> **Status: prototype.**

## Install

```
pip install -r requirements.txt
```

Also requires **NI-VISA** (from ni.com) to talk to the scope over USB.

## Run

All settings — scope resource, bus voltage, I2C limits, CSV log file — are entered in the GUI:

```
python gui.py
```

1. Enter the VISA resource string and other settings in the side panel.
2. Set a **CSV log file name** (saved under `results/`). A new name creates a file with a header row; an existing name **appends** new rows (never overwrites).
3. Click **Connect**, then **Acquire & Measure** or **I2C Capture**.

Logic tests (no scope):

```
python test_offline.py
```

## Files

- `gui.py` — main application (settings, plot, acquire, I2C, CSV logging)
- `config.py` — settings dataclass and PASS/FAIL limit evaluation
- `scope_interface.py` — VISA connection and acquisition
- `measurements.py` — scope IMMed measurement reads
- `i2c.py` — I2C capture and timing analysis
- `results_log.py` — CSV append logging
- `test_offline.py` — hardware-free logic tests
- `results/` — CSV logs (git-ignored)
