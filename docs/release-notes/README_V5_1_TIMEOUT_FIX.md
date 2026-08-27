# UADY FPGA Controller v5.1 — Dynamic Production Timeout Fix

This build includes everything from v5.0 plus the validated Ollama classification fix.

## AI runtime settings

- `timeout_seconds`: 120
- `num_predict`: 160
- controller-enforced response range: 80–160 tokens
- `num_ctx`: 2048
- `keep_alive`: 10m
- maximum concurrent AI requests: 1

## Why this fixes the observed failure

The manual Ollama API test completed successfully in about 8.4 seconds, proving that
Qwen and Ollama were healthy. The production controller, however, allowed up to
320 generated tokens while enforcing only a 60-second deadline. The new limits keep
the structured JSON response compact and give the real classification prompt enough
time to finish.

## Expected startup banner

`UADY Pi AI/HAT Dynamic JTAG Controller v5.1 Dynamic Production Timeout Fix`

## Raspberry Pi validation

```bash
cd ~/raspberry_pi_ai_hat
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements_pi.txt
python VALIDATE_V5.py
python pi_ai_hat_controller.py
```
