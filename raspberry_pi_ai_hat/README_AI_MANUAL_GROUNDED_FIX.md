# Ollama FPGA classifier fix - manual-grounded edition

This package combines two evidence sources:

1. The 10 matched Verilog/QSF projects in `Trainmoudle.zip`:
   - 4 DE10-Agilex projects
   - 6 DE1-SoC projects
2. The Terasic board manuals:
   - `DE10_Agilex_User_Manual_revc-3599500.pdf`
   - `de1-soc_user_manual.pdf`

## Authoritative identity

| Board | Accepted QSF device identifiers | Family |
|---|---|---|
| DE1-SoC | `5CSEMA5F31C6`, `5CSEMA5F31C6N` | `Cyclone V`, `Cyclone V SoC` |
| DE10-Agilex | `AGFB014R24B2E2V`, `AGFB014R24B1E1V` | `Agilex 7`, `Intel Agilex 7` |

The supplied projects use `5CSEMA5F31C6` and `AGFB014R24B2E2V`. The extra identifiers are manual-documented variants.

## Manual-verified signal evidence

The prompt now includes the board manuals' documented signal naming and widths.

DE1-SoC examples:

- `SW[9:0]`, `KEY[3:0]`, `LEDR[9:0]`
- `HEX0` through `HEX5`, each seven bits
- `CLOCK_50`, `CLOCK2_50`, `CLOCK3_50`, `CLOCK4_50`
- `GPIO_0` and `GPIO_1` expansion signals
- Actual top-level `HPS_*`, `DRAM_*`, `VGA_*`, `AUD_*`, `TD_*`, `IRDA_*`, `ADC_*`, and `PS2_*` signals

DE10-Agilex examples:

- `SW0`, `SW1`, `BUTTON0`, `BUTTON1`
- four `LED` signals and four `LED_BRACKET` signals
- `GPIO_P0` through `GPIO_P3`, `GPIO_CLK0`, `GPIO_CLK1`
- `SI5397A_*`, board clocks, `QSFPDD*`, `DDR4A-D_*`, and `PCIE_*`

These are fallback clues only. An exact QSF device remains authoritative.

## Why QSF targets are not active design evidence

The DE1-SoC QSF files in the training set are often complete board templates with many assignments not used by the current Verilog module. Therefore:

- use actual `verilog_signals` and `verilog_ports` for signal evidence;
- do not treat QSF-only targets as active design features;
- never allow signal evidence to override a recognized QSF device.

## Files

- `ollama_fpga_classifier_prompt.txt`: manual-grounded stateless Ollama prompt.
- `board_profiles_balanced.json`: project- and manual-derived profiles.
- `fpga_classifier_policy.py`: deterministic identity and hallucination guard.
- `manual_reference_evidence.json`: concise facts extracted from both manuals.
- `dataset_manifest.json`: summary of all supplied project pairs.
- `test_policy.py`: deterministic regression tests.

## Recommended integration into benchmark_classifier.py

Place the package files beside `benchmark_classifier.py`, then import:

```python
from fpga_classifier_policy import build_prompt, enforce_grounding, ollama_json_schema
```

Use the imported prompt builder:

```python
prompt = build_prompt(evidence, profiles)
```

Immediately after Ollama returns `parsed`, add:

```python
parsed = enforce_grounding(parsed, evidence)
```

In the end-to-end pipeline:

```python
current_prompt = build_prompt(current_evidence, profiles)
parsed = enforce_grounding(parsed, current_evidence)
```

When constructing the Ollama request payload, use the schema when supported:

```python
payload = {
    "model": model,
    "prompt": prompt,
    "stream": False,
    "format": ollama_json_schema(),
    "keep_alive": keep_alive,
    "options": {
        "temperature": 0,
        "seed": 42,
        "num_predict": 220,
    },
}
```

Do not reuse Ollama conversation `context` between classifications. Each job must be stateless.

## Validation

Run:

```bash
python3 test_policy.py
```

Then cross-check both boards with the benchmark. Exact recognized device identity should produce `correct=True` and `safe_match=True` even when the raw Ollama answer hallucinates another board, because the deterministic guard prevents the model from overriding the QSF identity.
