# Quartus Server and JTAG Setup

## Why the project uses JTAG

The final architecture programs FPGA configuration images through Intel Quartus `quartus_pgm` and USB JTAG rather than using Raspberry Pi GPIO/SPI as the FPGA programming transport. JTAG allows the controller to work with the vendor-supported programming chain, detect physical cables/devices, and route the same Pi controller to multiple board families.

GPIO fields remain in board profiles for optional external power/reset/status hardware, but `use_gpio` is `false` in the shipped final configuration.

## Quartus toolchains

The project distinguishes:

- **Quartus Standard** for DE1-SoC / Cyclone V SoC;
- **Quartus Pro** for DE10-Agilex / Agilex 7.

Configure the absolute `quartus_pgm` paths on the Pi with `UADY_PI.py --setup`. Toolchain discovery can also use:

```bash
export UADY_QUARTUS_STANDARD_PGM=/path/to/standard/quartus_pgm
export UADY_QUARTUS_PRO_PGM=/path/to/pro/quartus_pgm
export UADY_QUARTUS_SEARCH_ROOTS=/opt:/home
```

Inspect discovery:

```bash
python3 dynamic_toolchain_discovery.py
```

## SSH trust

The Pi, not the student laptop, should hold the SSH private key used to reach the Quartus server. Configure its path with the Pi setup flow. Use restrictive filesystem permissions and a dedicated server account with only the permissions needed for project files, history, and programming commands.

## History directory

The lab lineage used `/home/lab4p0/History_of_jobs` as the server history directory, but this is deployment-specific. For a new installation, choose the intended server path during setup and ensure the configured SSH user can create/update files there.

The final controller is configured for one record per Job ID. Queue acceptance creates/initializes the record, and later lifecycle events update it rather than creating a second record for the same job.

## JTAG prewarming

The final config enables background prewarming:

- startup iterations: 3;
- startup delay: 0.25 s;
- periodic interval: 6 s;
- pauses during active programming;
- refreshes JTAG cache.

This was added because earlier behavior sometimes failed the first programming attempt and only succeeded after a later retry. Prewarming moves JTAG/jtagd initialization before the first student job.

Check:

```bash
python3 UADY_PI.py --test-jtag
python3 pi_ai_hat_controller.py --jtag-prewarm-startup
```

The second command requires the Pi Python dependencies to be installed.

## Board profile defaults

The shipped profiles expect cable/device patterns similar to:

- DE1-SoC: USB-Blaster / device pattern `5CSEMA5.*`, default chain index `2`;
- DE10-Agilex: USB-BlasterII / device pattern `AGFB014.*`, default chain index `1`.

Treat these as deployment defaults, not universal truths. Always verify `quartus_pgm -l` / JTAG chain output on the target lab and adjust `board_profiles.json` if the physical chain differs.
