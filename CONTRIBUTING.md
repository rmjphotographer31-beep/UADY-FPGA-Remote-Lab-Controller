# Contributing

Keep changes small and testable. This controller touches shared physical hardware, so queue/resource safety is more important than cosmetic simplification.

Before a pull request, run:

```bash
python -m compileall -q .
python scripts/validate_repository.py
cd raspberry_pi_ai_hat
python3 TEST_FPGA_CLASSIFIER_POLICY.py
python3 VALIDATE_V5.py
```

Any change to JTAG programming, queue ownership, cancellation, AI grounding, or history should also be verified on the real lab hardware before release.

Never commit Pi API keys, terminal keys, queue creator tokens, private SSH keys, or deployment-only private configuration.
