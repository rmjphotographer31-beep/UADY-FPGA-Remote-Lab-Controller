AI-only board selection mode — controller v4.54

Qwen is the only board-family selector.

C extractor responsibilities
- Extract normalized input/output/inout names and widths.
- Extract normalized QSF assignment targets.
- Extract QSF family, device, and board metadata.
- Emit compact JSON.
- Do not score, label, rank, or choose a board.

Qwen responsibilities
- Use a 192-token structured-output budget to avoid truncated JSON.
- Choose DE1-SoC, DE10-Agilex, Ambiguous, or Conflict.
- Cite literal tokens from the extracted evidence.
- Return structured JSON with confidence and safe_to_program.

Backend safety responsibilities
- Verify that Qwen-cited tokens exist in the extracted evidence.
- Enforce the minimum confidence and safe_to_program gates.
- Filter the AI-selected family to a real, enabled, free JTAG slot.
- Never choose a different family as a fallback.

Queue persistence fix
- Failed/completed/cancelled jobs remain visible in recent queue rows.
- Stale background planner saves cannot erase the terminal job index.
- The GUI reports the exact failure reason.

Startup
1. source .venv/bin/activate
2. pip install -r requirements_pi.txt
3. ./BUILD_SIGNAL_EXTRACTOR.sh
4. python pi_ai_hat_controller.py
