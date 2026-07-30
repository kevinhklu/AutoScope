# AutoScope

Scripted, logged, repeatable bench measurements on a Tektronix MDO4054C,
replacing manual cursor reads.

> **Status: learning / prototype. NOT qualified.** Nothing here is validated
> for use in a test record until it has been run side-by-side against the
> manual cursor method on known-good boards and signed off by the owner of
> the test procedure.

## Two-machine workflow

| Machine | Role | What runs here |
|---|---|---|
| **Mac** | Write code | Offline logic tests only (`test_offline.py`) — a mock scope, no VISA driver needed |
| **Windows laptop** | Bench | Real hardware tests against the scope — requires NI-VISA + the instrument |

The Mac has no VISA driver, so it can never open the scope. That's fine: the
code is structured so all measurement *logic* is testable with a mock, and only
the actual instrument I/O needs the bench.

## Setup

Both machines:
```
pip install -r requirements.txt
```
Windows bench machine additionally needs **NI-VISA** installed (from ni.com) —
it is not a pip package. See `requirements.txt`.

## Run the offline logic tests (Mac, anytime)
```
python3 test_offline.py
```
Verifies valid readings, invalid-value (9.9e37) screening, and acquisition
ordering — without any hardware.

## Run a live reading (Windows bench machine)
```
# set your scope's VISA string (keeps the serial out of git):
set AUTOSCOPE_RESOURCE=USB0::0x0699::0x0456::<serial>::INSTR
python measurements.py
```
Prints `*IDN?` (instrument ID), `*OPT?` (installed options — look for
`DPOEMBD`, the I2C/SPI decode package), and a live CH1 peak-to-peak reading.

## Files
- `scope_interface.py` — VISA connection + the deterministic single-acquisition path
- `measurements.py` — built-in measurements (Vpp, Vmean)
- `test_offline.py` — hardware-free logic tests
- `results/` — CSV logs (git-ignored; raw bench data, not source)
