# Repository Validation Report

**Packaging date:** 2026-08-09  
**Baseline:** `UADY_FPGA_Controller_v5_4_final_polished_AI_manual_grounded_fix.zip`

## Checks completed successfully

The GitHub-ready tree was checked with:

```bash
python scripts/validate_repository.py
python -m pytest -q
cd raspberry_pi_ai_hat
python3 TEST_FPGA_CLASSIFIER_POLICY.py
python3 VALIDATE_V5.py
```

Observed result:

- repository required-file check: PASS;
- all Python source syntax: PASS;
- JSON syntax: PASS;
- production policy invariants: PASS;
- authoritative DE1-SoC/DE10-Agilex device identities present: PASS;
- basic secret/deployment-literal scan: PASS;
- `UADY_SETUP.py --help` packaging regression: PASS;
- packaging tests: 3 passed;
- classifier policy regression: PASS;
- C signal extractor build: PASS;
- v5.4 manual-grounded validation: PASS.

## Packaging defect repaired

The supplied final archive contained `UADY_SETUP.py`, but the imported module `SETUP_CLASSIC_AND_PI.py` was missing. This made the root one-command setup wizard fail immediately. The GitHub package restores that helper using the storage formats and `UADY_PI.py --save` interface already present in the final source.

The setup wizard's Raspberry Pi username/path defaults were also generalized so the repository is not tied to one historical account.

## Public-repository sanitation

The retained legacy deployment note was sanitized to remove deployment-specific NetBird/user values. A repository scan found no historical private/tunnel IPs or personal usernames in the final tree, and no private-key/setup-key literals were detected by the included basic scanner.

This is not a substitute for GitHub secret scanning or a dedicated security scanner, especially after future commits.

## Not validated in this packaging environment

No physical Raspberry Pi, Quartus server, Ollama daemon, USB-Blaster/JTAG chain, DE1-SoC, or DE10-Agilex board was attached to the packaging environment. Therefore the following still require deployment acceptance testing:

- Pi-to-Quartus SSH authentication;
- actual `quartus_pgm` paths and permissions;
- cable/device index matching;
- first-attempt JTAG programming reliability;
- DE1-SoC programming;
- DE10-Agilex programming;
- live FIFO progression under multiple submissions;
- live cancellation of active Quartus work;
- testing lease release;
- server history permissions and one-record-per-job behavior;
- real GUI/NetBird/SSE latency;
- real Ollama/Qwen inference speed and accuracy on the target Pi.

Use `docs/REPRODUCIBILITY_CHECKLIST.md` before calling a deployment fully reproduced.
