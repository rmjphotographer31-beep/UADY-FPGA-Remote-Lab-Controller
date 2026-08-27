# Classifier Benchmarking

`raspberry_pi_ai_hat/benchmark_classifier.py` measures three layers:

1. C extractor latency;
2. AI selector latency;
3. end-to-end current-job classification pipeline.

The final benchmark records both the raw model answer and the guarded answer so a hallucination can be counted without being allowed to program the wrong FPGA.

## Example

```bash
cd raspberry_pi_ai_hat
source .venv/bin/activate
python3 benchmark_classifier.py \
  --verilog /tmp/Test_1A.v \
  --qsf /tmp/Test_1A.qsf \
  --expected-board DE10-Agilex \
  --extractor ./fpga_signal_extractor \
  --c-runs 500 \
  --ai-runs 50 \
  --pipeline-runs 25 \
  --output-prefix benchmark_results/Test_1A
```

## Important metrics

- `raw_ai_accuracy_percent`: accuracy of Qwen before deterministic enforcement;
- `guarded_accuracy_percent`: accuracy after authoritative identity grounding;
- `safe_match_rate_percent`: fraction of runs that produce a programming-safe final match;
- latency percentiles such as p50/p95: p95 means 95% of measured runs completed at or below that latency, while the slowest 5% took longer.

## Exact-device guard

When the QSF contains a recognized supported FPGA device, the guard checks/corrects the model result to that board. This prevents an internally valid-looking JSON answer containing stale or invented evidence from routing a job to the opposite FPGA.

## Reproducibility

For comparable runs, keep constant:

- Pi model/CPU settings;
- Ollama and Qwen model version;
- model keep-alive state;
- extractor binary build flags;
- Verilog/QSF files;
- concurrent system load;
- run counts.

Warm and cold model/JTAG conditions should be reported separately because they measure different operational states.
