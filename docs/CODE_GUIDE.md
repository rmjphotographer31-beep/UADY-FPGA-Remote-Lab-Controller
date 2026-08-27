# Code Guide

This guide is for maintainers who need to change the final system without reintroducing earlier queue, synchronization, or board-selection failures.

## Root / Windows side

### `gui.py`

The main Tkinter application. It still contains much of the historical UI/workflow logic in one file. Important areas include:

- Pi API connection/authentication;
- background worker dispatch so network/SSH work does not block Tkinter;
- live `/boards` display;
- persistent `/stream/queue` SSE consumption;
- board-selection calls;
- prequeue + multipart upload flow;
- creator token storage and batch cancellation;
- local optimistic queue row handling;
- legacy Classic Mode compatibility.

When modifying submission logic, preserve the two-phase invariant: **real server Job ID first, large file upload second**.

### `RUN_GUI.py`

Adds `src/` to `sys.path` and launches the legacy GUI through `uady_fpga_gui.launch`.

### `src/uady_fpga_gui/`

A partially modularized layer around the GUI:

- `models.py`: typed server/connection/project data;
- `classic_config.py`: private Classic Mode profile loading;
- `classic_fpga.py`: direct/legacy Quartus SSH helpers;
- `pi_client.py`: Pi API client abstraction;
- `background.py`: non-blocking task executor;
- `gui_paths.py`: runtime paths;
- `launch.py`: GUI entry point.

### `uady_secure_store.py`

Shared per-user secure-ish storage helper. It moves secrets/configuration out of the repository folder and applies restrictive POSIX permissions when supported.

### `UADY_SETUP.py` + `SETUP_CLASSIC_AND_PI.py`

Cross-machine setup wizard. `SETUP_CLASSIC_AND_PI.py` was missing from the supplied final archive and is restored in this GitHub package. The helper writes Classic configuration locally and delegates Pi configuration to protected Pi storage / `UADY_PI.py --save` for remote setup.

## Raspberry Pi side

### `pi_ai_hat_controller.py`

The central production service. It includes several subsystems in one file:

- Flask routes and global API-key gate;
- adaptive traffic limits;
- state/persistence integration;
- queue admission;
- fair-share and duplicate checks;
- upload staging and SOF integrity verification;
- AI classification orchestration;
- live JTAG inventory and physical-slot ownership;
- FIFO planning/dispatch;
- programming workers and remote Quartus commands;
- testing leases;
- cancellation;
- automatic repair/watchdogs;
- JTAG prewarm daemon;
- SSE queue broadcast;
- remote history logging.

Because this file owns physical resource safety, avoid broad refactors unless there is a complete hardware regression plan.

### `fpga_signal_extractor.c`

C parser that extracts current-job Verilog/QSF identity and signal evidence. It should not decide the target board. Build with `BUILD_SIGNAL_EXTRACTOR.sh`.

### `fpga_classifier_policy.py`

Deterministic post-model policy. Key responsibilities are prompt construction/schema, exact identity mapping, conflict checks, and correction of hallucinated/stale model evidence.

### `ollama_fpga_classifier_prompt.txt`

Current stateless model prompt. It describes the supported boards and evidence rules. Keep its board facts aligned with `board_profiles.json` and policy tests.

### `board_profiles.json`

Operational hardware/JTAG profiles merged with manual-grounded AI identity evidence. It is used both to identify connected hardware and to constrain classification safety.

### `production_policy.json`

Readable declaration of intended production invariants: event-driven dispatch, SQLite WAL persistence, cancellation behavior, AI handoff, ephemeral data, and benchmarking.

### `job_store_sqlite.py`

SQLite/WAL job persistence utilities with revision/compare-and-swap support.

### `dynamic_toolchain_discovery.py`

Locates Standard/Pro Quartus programming executables using configuration, environment, PATH, and search roots.

### `benchmark_classifier.py`

Measures extractor, AI, and end-to-end classifier performance. Keep raw AI and guarded board outputs separate so safety corrections remain observable.

### `UADY_PI.py` and `src/uady_fpga_pi/`

Administrative/operations layer:

- `cli.py`: arguments and interactive menu;
- `config.py`: protected Pi setup and checks;
- `jtag.py`: JTAG test helper;
- `runtime.py`: venv/dependency setup, prewarm, watchdog process;
- `process.py`: subprocess helpers;
- `paths.py`: controller/runtime paths.

## Safety-critical invariants

Before merging changes, verify all of these still hold:

1. Recognized QSF device identity cannot be overridden by Qwen.
2. A job cannot program an unknown/quarantined/mismatched physical JTAG slot.
3. One physical slot cannot have two active owners.
4. Default fair-share is one active job per student.
5. Terminal jobs stop counting against fair-share.
6. An active duplicate `.v/.sof` submission is rejected, but the same files can be submitted again after terminal completion/cancellation/failure.
7. Cancellation invalidates late worker results and releases resources safely.
8. Testing retains the physical FPGA lease until completion/cancel.
9. Job history remains one stable record per Job ID.
10. Student source/evidence is not intentionally persisted in permanent history.

## Testing hierarchy

Use increasing levels of confidence:

```text
compileall
  -> repository validation
  -> classifier policy regression
  -> VALIDATE_V5 (includes C build)
  -> Pi setup/JTAG test
  -> known DE1-SoC end-to-end job
  -> known DE10-Agilex end-to-end job
  -> queue/cancel/fair-share/duplicate stress tests
```

Never treat packaging-only validation as proof that the real Quartus/JTAG hardware path works.
