from pathlib import Path
import json
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]


def test_required_entry_points_exist():
    for rel in [
        "RUN_GUI.py",
        "UADY_SETUP.py",
        "SETUP_CLASSIC_AND_PI.py",
        "raspberry_pi_ai_hat/UADY_PI.py",
        "raspberry_pi_ai_hat/pi_ai_hat_controller.py",
    ]:
        assert (ROOT / rel).exists(), rel


def test_setup_help_works():
    p = subprocess.run([sys.executable, "UADY_SETUP.py", "--help"], cwd=ROOT, capture_output=True, text=True)
    assert p.returncode == 0, p.stderr
    assert "One-command secure setup wizard" in p.stdout


def test_core_final_release_policies():
    config = json.loads((ROOT / "raspberry_pi_ai_hat/config_pi_hat.json").read_text())
    assert config["fair_share"]["max_active_jobs_per_student"] == 1
    assert config["server_history"]["one_record_per_job"] is True
    assert config["jtag_prewarm_daemon"]["enabled"] is True
    assert config["ephemeral_job_storage"]["persist_source_contents"] is False
