# Changelog

## v5.4 Final Polished + Manual-Grounded AI Fix — GitHub packaging

This GitHub-ready package uses the supplied `UADY_FPGA_Controller_v5_4_final_polished_AI_manual_grounded_fix` archive as the runtime baseline.

Repository-packaging changes:

- Added full replication, architecture, API, security, queue, AI, benchmark, and troubleshooting documentation.
- Restored the missing `SETUP_CLASSIC_AND_PI.py` helper required by `UADY_SETUP.py`.
- Replaced hard-coded setup defaults tied to one Raspberry Pi username/path with user-derived defaults.
- Removed deployment-specific NetBird/IP values from the retained legacy deployment note.
- Added GUI requirements file, CI workflow, repository validation, packaging regression tests, issue templates, and systemd example.
- Preserved core GUI, Pi controller, queue, AI classifier, and programming logic.

## v5.4 Final Polished

- Stateless current-job C evidence extraction and Qwen classification path.
- Manual-grounded deterministic QSF identity guard.
- Exact recognized DE1-SoC and DE10-Agilex device identity cannot be overridden by model hallucination.
- Raw AI target and guarded target are both visible in classifier benchmarks.
- Provisional JTAG/slot visibility is hidden until a valid AI selection exists.
- Compact timing/decision logging.

## v5.3 AI handoff

- Refined AI handoff from extracted evidence to the model.
- Kept board-selection responsibility on Qwen while retaining deterministic safety enforcement.

## v5.2 Ephemeral pipeline

- Source, extracted evidence, AI prompt, and raw AI result moved toward temporary/memory-only handling.
- Permanent history retained as lightweight metadata.

## v5.1 timeout/reliability work

- Programming timeout and recovery behavior refined for Standard vs Pro/Agilex paths.

## Dynamic production / queue reliability lineage

Earlier iterations introduced the behavior still present in this final build: dynamic live JTAG profiles, FIFO routing, one active job per student, duplicate-submission protection, cancellation/requeue recovery, one history record per Job ID, event-driven queue updates, JTAG prewarming, dynamic Quartus discovery, SQLite/WAL job persistence, and testing as a physical board lease.

See `docs/release-notes/` for the retained incremental notes from the supplied archive.
