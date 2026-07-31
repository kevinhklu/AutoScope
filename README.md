# AutoScope

Scripted, logged, repeatable bench measurements on a Tektronix MDO4054C,
replacing manual cursor reads.

> **Status: learning / prototype. NOT qualified.** Nothing here is validated
> for use in a test record until it has been run side-by-side against the
> manual cursor method on known-good boards and signed off by the owner of
> the test procedure.

## Install

```
pip install -r requirements.txt
```
Also requires **NI-VISA** installed (from ni.com) to talk to the scope — it is
not a pip package.

## Configure

Set your scope's VISA resource string (keeps the serial out of the code):
```
set AUTOSCOPE_RESOURCE=USB0::0x0699::0x0456::<serial>::INSTR
```
Optional overrides (these are the defaults):
```
set AUTOSCOPE_SCL=CH1      REM channel probing SCL
set AUTOSCOPE_SDA=CH2      REM channel probing SDA
set AUTOSCOPE_VDD=1.8      REM bus voltage, volts
set AUTOSCOPE_DELAY=5      REM seconds to wait before capture (0 = none)
```

## Run

Basic voltage reading (`*IDN?`, `*OPT?`, and a CH1 Vpp):
```
python measurements.py
```

I2C timing — captures a live transaction, then prints SCL levels, SCL
fall/high/low times, and per-bit tHD;DAT / tSU;DAT:
```
python i2c.py
```

Logic tests (no scope required):
```
python test_offline.py
```

## Files
- `scope_interface.py` — VISA connection, deterministic acquisition, raw waveform read
- `measurements.py` — built-in measurements (Vpp, Vmean) and the shared read path
- `i2c.py` — I2C capture and timing (SCL fall/high/low, tHD;DAT, tSU;DAT)
- `test_offline.py` — hardware-free logic tests
- `results/` — CSV logs
