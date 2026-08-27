# UADY FPGA Controller v5.4 Final Polished
## Manual-Grounded AI Classifier Fix Included

## Verified production flow

1. The Windows GUI uploads `.v`/`.sv`, `.qsf`, and `.sof`.
2. The Raspberry Pi stages those files temporarily.
3. The C extractor reads Verilog and QSF and creates ephemeral evidence.
4. Qwen receives a fresh, stateless prompt containing only the current QSF identity and actual top-level Verilog ports/signals.
5. `fpga_classifier_policy.py` verifies the response and enforces exact recognized QSF hardware identity.
6. The controller assigns only a live JTAG slot matching the guarded board result, programs, tests, records lightweight metadata, and deletes temporary source/evidence files.

## Why the new guard is necessary

A small local model can return valid JSON while inventing evidence from a previous request. The new policy prevents that error from routing a job to the wrong FPGA:

- `5CSEMA5F31C6` or `5CSEMA5F31C6N` → `DE1-SoC`
- `AGFB014R24B2E2V` or `AGFB014R24B1E1V` → `DE10-Agilex`

Recognized device identity is authoritative. QSF family and board name are checked for conflicts. Signals are fallback clues only, and QSF-only board-template targets are never treated as active Verilog features.

## Included AI fix files

- `fpga_classifier_policy.py`
- `ollama_fpga_classifier_prompt.txt`
- `board_profiles_manual_grounded.json`
- merged operational `board_profiles.json`
- `manual_reference_evidence.json`
- `dataset_manifest.json`
- `TEST_FPGA_CLASSIFIER_POLICY.py`
- `README_AI_MANUAL_GROUNDED_FIX.md`

The profiles combine the supplied ten Verilog/QSF project pairs with the Terasic DE1-SoC and DE10-Agilex manuals.

## Privacy and temporary evidence

No source code, QSF body, prompt, raw Qwen response, or extracted evidence JSON is retained in job history. The guard runs in memory during the classification call. Only compact decision and timing metadata may be returned.

## Benchmark

`benchmark_classifier.py` now records both:

- `raw_ai_target`: the board Ollama originally returned.
- `target_board`: the final manual-grounded guarded result.

The summary includes `raw_ai_accuracy_percent`, `guarded_accuracy_percent`, and `safe_match_rate_percent`. This makes hallucinations visible without allowing them to program the wrong board.

Example:

```bash
python3 benchmark_classifier.py \
  --verilog /tmp/Test_1A.v \
  --qsf /tmp/Test_1A.qsf \
  --expected-board DE10-Agilex \
  --c-runs 10 \
  --ai-runs 3 \
  --pipeline-runs 2 \
  --output-prefix Test_1A_grounded
```

## Deployment validation

On the Raspberry Pi:

```bash
cd ~/raspberry_pi_ai_hat
source .venv/bin/activate
python3 VALIDATE_V5.py
```

The validation checks Python syntax, JSON files, prompt markers, both board identities, the hallucination correction guard, and the C extractor build.
