UADY FPGA Lab package v5 — Controller v4.54 Queue Persistence + AI Evidence Fix

THIS BUILD FIXES THE REPORTED JOB cb2a1064bd BEHAVIOR
- A submitted job no longer disappears from the Real-Time Queue Jobs table after it fails.
- Failed, completed, and cancelled jobs are reconstructed from saved terminal job records even
  if a stale background planner snapshot temporarily overwrites recent_jobs.
- Background /boards and queue-repair saves preserve newer job lifecycle state.
- The GUI prints and displays the exact failure reason once for each failed job.
- The AI exact-evidence gate now validates Qwen citations against the C extractor output.
  It no longer falsely rejects labels such as QSF_DEVICE:5CSEMA5F31C6 merely because the raw
  QSF file does not literally contain the synthetic QSF_DEVICE: prefix.
- Qwen has a 192-token structured-response budget so the required JSON is not cut off.
- Qwen may make one repair pass when its first response is malformed or below the configured
  confidence/safety threshold. Both attempts use Qwen; there is no local board-selection fallback.
- The C extractor emits signal and QSF evidence only. It does not score or label either board.
- The previous strncpy compiler warning was removed.

EXPECTED JOB FLOW
receiving -> queued -> analyzing -> running/programming -> testing -> completed

A safe AI rejection stays visible as:
failed -> <exact reason>

WINDOWS GUI START
1. Open PowerShell in this folder.
2. Run:
   py -m pip install requests
   py RUN_GUI.py

RASPBERRY PI INSTALL/START
1. Replace ~/raspberry_pi_ai_hat with the included folder.
2. On the Pi run:
   cd ~/raspberry_pi_ai_hat
   python3 -m venv .venv
   source .venv/bin/activate
   pip install -r requirements_pi.txt
   chmod +x BUILD_SIGNAL_EXTRACTOR.sh
   ./BUILD_SIGNAL_EXTRACTOR.sh
   python pi_ai_hat_controller.py

CONNECTION
- Test Pi calls /status and sends X-API-Key.
- Opening http://PI-IP:5050/ in a normal browser may return 401; that is expected.

HISTORY
- Job history remains under /home/lab4p0/History_of_jobs.
- Pi API and terminal keys remain outside the project folder.
