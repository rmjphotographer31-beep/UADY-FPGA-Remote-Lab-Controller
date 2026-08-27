# Replication Checklist

Use this checklist for a fresh lab deployment.

## Hardware

- [ ] Raspberry Pi with reliable network access.
- [ ] Quartus/JTAG server reachable by SSH from Pi.
- [ ] DE1-SoC connected and visible in the Standard JTAG chain.
- [ ] DE10-Agilex connected and visible in the Pro/Agilex JTAG chain.
- [ ] USB/JTAG permissions configured on the Quartus server.
- [ ] Optional external relay/reset GPIO wiring verified if enabling `use_gpio`.

## Quartus server

- [ ] Standard `quartus_pgm` absolute path verified.
- [ ] Pro `quartus_pgm` absolute path verified.
- [ ] SSH account can run the required commands.
- [ ] Pi SSH public key is authorized.
- [ ] History directory exists and is writable.
- [ ] JTAG cable/device indices match `board_profiles.json` or profiles are adjusted.

## Raspberry Pi

- [ ] Python virtual environment created.
- [ ] `requirements_pi.txt` installed.
- [ ] `fpga_signal_extractor` built from C source.
- [ ] Ollama running.
- [ ] `qwen2.5-coder:1.5b` installed.
- [ ] `UADY_PI.py --setup` completed.
- [ ] `UADY_PI.py --check` passes.
- [ ] `UADY_PI.py --test-jtag` passes.
- [ ] `VALIDATE_V5.py` passes.
- [ ] `TEST_FPGA_CLASSIFIER_POLICY.py` passes.
- [ ] API/terminal keys recorded securely for administrators.
- [ ] Controller starts with `UADY_PI.py --start`.

## Windows GUI

- [ ] Python virtual environment created.
- [ ] `requirements_gui.txt` installed.
- [ ] `UADY_SETUP.py --help` works.
- [ ] Pi host and API key configured.
- [ ] GUI **Test Pi** passes.
- [ ] `/boards` shows expected live boards.
- [ ] `/stream/queue` updates without repeated reconnects.

## End-to-end acceptance tests

- [ ] Submit known DE1-SoC `.v/.qsf/.sof`; guarded classification is DE1-SoC.
- [ ] Submit known DE10-Agilex `.v/.qsf/.sof`; guarded classification is DE10-Agilex.
- [ ] Correct physical board programs successfully on first normal attempt after prewarm.
- [ ] Job enters testing and board remains reserved.
- [ ] Test completion releases board and user can immediately submit another job.
- [ ] Cancellation releases user's active-job allowance and safely frees/requeues resources.
- [ ] Second simultaneous active submission by same student is rejected when limit is 1.
- [ ] Accidental duplicate active `.v/.sof` submission is not duplicated.
- [ ] Waiting FIFO job starts when a compatible board becomes free.
- [ ] Exactly one history record exists for each Job ID and is updated through completion.
- [ ] Terminal source/evidence files are cleaned from Pi spool.
- [ ] GUI reconnect still shows retained terminal jobs.

## Performance characterization

- [ ] Measure GUI↔Pi `/sync/ping` latency.
- [ ] Benchmark C extractor.
- [ ] Benchmark raw Qwen and guarded classification.
- [ ] Measure Standard programming time.
- [ ] Measure Pro/Agilex programming time.
- [ ] Report p50/p95 rather than claiming deterministic millisecond/nanosecond completion.
