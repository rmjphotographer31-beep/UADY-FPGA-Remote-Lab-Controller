# UADY FPGA Controller v5.3 — AI Handoff and Board Selection Fix

Fixes the two observed problems:

1. The GUI no longer shows a DE1-SoC or DE10-Agilex slot before Qwen has
   completed a valid board-family decision.
2. Qwen now receives the C extractor JSON plus declarative profile guidance
   loaded from `board_profiles.json`.

The structured output schema contains only:
- target_board
- confidence_percent
- safe_to_program
- decision_type
- reason

Qwen is not required to regenerate core evidence.

Strong Agilex evidence includes QSF family Agilex 7, AGFB014 devices,
LED_BRACKET, SI5397, QSFP, PCIe, or DDR-bank names.

Strong DE1-SoC evidence includes Cyclone V, 5CSEMA5 devices, LEDR, HEX,
CLOCK_50, DRAM, or HPS names.

During AI analysis the public queue shows blank Slot and JTAG fields.
A physical slot is exposed only after a valid AI board selection.
