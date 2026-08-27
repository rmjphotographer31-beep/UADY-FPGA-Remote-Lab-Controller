## Summary

## Component(s) changed

## Safety/resource impact
- [ ] No change to board identity guard
- [ ] No change to creator cancellation authority
- [ ] No change to one-active-job fair-share behavior
- [ ] No change to physical JTAG slot ownership
- [ ] Temporary/student data retention reviewed

## Validation
- [ ] `python -m compileall -q .`
- [ ] `python scripts/validate_repository.py`
- [ ] `python raspberry_pi_ai_hat/TEST_FPGA_CLASSIFIER_POLICY.py`
- [ ] `python raspberry_pi_ai_hat/VALIDATE_V5.py`
- [ ] Live hardware test performed if programming/JTAG logic changed
