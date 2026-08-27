# Windows GUI Installation

## Requirements

- Windows 10/11 or another environment that can run Tkinter GUI applications.
- Python 3.10+ recommended by the root project metadata.
- Network path to the Raspberry Pi API, typically LAN or NetBird.
- Pi API key printed from `python3 UADY_PI.py --keys` on the Raspberry Pi.

## Install

From PowerShell in the repository root:

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
py -m pip install --upgrade pip
py -m pip install -r requirements_gui.txt
```

Run the setup wizard:

```powershell
py UADY_SETUP.py
```

Then launch:

```powershell
py RUN_GUI.py
```

or double-click `RUN_GUI.bat`.

## Private GUI storage

The GUI does not need secrets committed beside the source. `uady_secure_store.py` uses an OS-specific application-data directory. On Windows this is under `%APPDATA%\UADY_FPGA_Lab` when `%APPDATA%` is available.

Typical private files include:

- GUI secrets, including the stored Pi API key;
- queue creator/cancel tokens;
- `classic_servers.ini` if Classic Mode is configured;
- reusable setup profile answers.

Do not copy these files into the Git repository.

## Pi connection

The GUI's production Pi mode expects:

- remote connection checkbox enabled;
- Pi NetBird/LAN host/IP;
- port `5050` unless changed on the Pi;
- Pi API key.

Use **Test Pi** before submitting a job. The test calls `/status` with `X-API-Key`.

## Submission files

The final flow is designed for a compiled Quartus job and uses:

- `.v` or `.sv` top-level/source evidence;
- `.qsf` hardware identity and assignment context;
- `.sof` programming image.

The QSF is especially important because recognized exact device identity is the deterministic safety authority for DE1-SoC vs DE10-Agilex.
