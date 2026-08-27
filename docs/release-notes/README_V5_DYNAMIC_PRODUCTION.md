# UADY FPGA Controller v5.0 — Dynamic Production Build

This package consolidates the fixes and architecture developed in the chat:

- Live JTAG-slot scaling; no fixed board count.
- Board profiles are external in `board_profiles.json`.
- Quartus executables can be discovered from environment variables, PATH, or
  filesystem search using `dynamic_toolchain_discovery.py`.
- Unknown hardware is quarantined rather than guessed.
- The C extractor emits compact Verilog/QSF evidence only.
- Qwen remains the sole job board selector.
- Qwen is kept warm for 10 minutes and inference concurrency remains 1 on the Pi.
- A high-confidence safe single-board result is normalized from `conflict` to
  `match` only when no opposite-board evidence was cited.
- Failed/cancelled/completed jobs remain available as recent jobs.
- `job_store_sqlite.py` supplies WAL persistence and revision compare-and-swap.
- Queue progression is event-driven; static workflow countdowns are disabled.
- Testing is an asynchronous board lease: it does not retain the AI slot, but
  the physical FPGA remains reserved until testing ends or is cancelled.
- Cancellation policy requires active AI/Quartus work to stop, late results to
  be rejected, and the dispatcher to wake immediately.

## Validate on the Raspberry Pi

```bash
cd ~/raspberry_pi_ai_hat
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements_pi.txt
python VALIDATE_V5.py
python pi_ai_hat_controller.py
```

Expected banner:

`UADY Pi AI/HAT Dynamic JTAG Controller v5.0 Dynamic Production`

## Optional Quartus environment overrides

```bash
export UADY_QUARTUS_STANDARD_PGM=/path/to/standard/quartus_pgm
export UADY_QUARTUS_PRO_PGM=/path/to/pro/quartus_pgm
export UADY_QUARTUS_SEARCH_ROOTS=/opt:/home
```

Run discovery independently:

```bash
python dynamic_toolchain_discovery.py
```

## Adding another board family

Add one object to `board_profiles.json`. Do not edit controller Python.
A new profile should define display name, cable/device patterns, Quartus family,
features, and optional GPIO mapping.

## Safety

A profile match identifies connected hardware. Qwen still determines which
profile a submitted design targets. Programming occurs only when the AI result,
extracted evidence, connected profile, and available JTAG reservation agree.
