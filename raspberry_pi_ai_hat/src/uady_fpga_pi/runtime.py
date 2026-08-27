"""Controller dependency setup, JTAG prewarm, and watchdog runtime."""
from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import time
import venv
from pathlib import Path
from typing import List

from uady_secure_store import ensure_private_dir

from .paths import CONTROLLER, LOG_DIR, PI_ROOT, REQUIREMENTS, VENV_DIR
from .process import format_cmd, run_command


class PiControllerRuntime:
    """Starts ``pi_ai_hat_controller.py`` with dependency setup and watchdog."""

    @property
    def venv_python(self) -> Path:
        return VENV_DIR / "bin" / "python"

    @property
    def venv_pip(self) -> Path:
        return VENV_DIR / "bin" / "pip"

    @staticmethod
    def sha256_file(path: Path) -> str:
        h = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1024 * 1024), b""):
                h.update(chunk)
        return h.hexdigest()

    def ensure_venv_and_requirements(self) -> None:
        if not VENV_DIR.exists() or not self.venv_python.exists():
            print("[INFO] Creating Python virtual environment...")
            venv.EnvBuilder(with_pip=True).create(str(VENV_DIR))
        if not REQUIREMENTS.exists():
            print("[WARN] requirements_pi.txt not found; skipping dependency install.")
            return
        hash_file = VENV_DIR / ".requirements_pi.sha256"
        current_hash = self.sha256_file(REQUIREMENTS)
        old_hash = hash_file.read_text(encoding="utf-8").strip() if hash_file.exists() else ""
        if os.environ.get("FORCE_PIP_INSTALL") == "1" or current_hash != old_hash:
            print("[INFO] Installing/updating Python dependencies...")
            run_command([str(self.venv_python), "-m", "pip", "install", "--upgrade", "pip"], check=True)
            run_command([str(self.venv_python), "-m", "pip", "install", "-r", str(REQUIREMENTS)], check=True)
            hash_file.write_text(current_hash, encoding="utf-8")
        else:
            print("[OK] Python dependencies already installed; requirements_pi.txt unchanged.")

    def build_c_classifier(self) -> None:
        src = PI_ROOT / "fpga_board_classifier_c.c"
        out = PI_ROOT / "fpga_board_classifier_c"
        if not src.exists():
            return
        if out.exists() and out.stat().st_mtime >= src.stat().st_mtime:
            print("[OK] Optional C classifier already built.")
            return
        gcc = shutil.which("gcc")
        if not gcc:
            print("[WARN] gcc not found; Python FPGA classifier fallback will be used.")
            return
        print("[INFO] Building optional C FPGA classifier accelerator...")
        run_command([gcc, "-O2", "-std=c11", "-Wall", "-Wextra", "-o", str(out), str(src)], check=False)
        if out.exists():
            try:
                os.chmod(out, 0o755)
            except Exception:
                pass

    def prewarm_jtag(self) -> None:
        if not CONTROLLER.exists():
            print("[WARN] pi_ai_hat_controller.py not found; skipping JTAG prewarm.")
            return
        print("=" * 64)
        print("JTAG prewarm before controller server")
        print("=" * 64)
        run_command([str(self.venv_python), str(CONTROLLER), "--jtag-prewarm-startup"], check=False)

    def stream_process_to_log(self, cmd: List[str], log_path: Path) -> int:
        with open(log_path, "a", encoding="utf-8", errors="ignore") as log:
            log.write(f"\n[START] {format_cmd(cmd)}\n")
            log.flush()
            proc = subprocess.Popen(
                cmd,
                cwd=str(PI_ROOT),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            )
            try:
                assert proc.stdout is not None
                for line in proc.stdout:
                    print(line, end="")
                    log.write(line)
                    log.flush()
                return int(proc.wait())
            except KeyboardInterrupt:
                print("\n[INFO] Stopping controller...")
                log.write("\n[STOP] KeyboardInterrupt received.\n")
                try:
                    proc.terminate()
                    proc.wait(timeout=5)
                except Exception:
                    try:
                        proc.kill()
                    except Exception:
                        pass
                raise

    def start_controller(self) -> None:
        print("=" * 64)
        print("Starting UADY Raspberry Pi AI/HAT Controller")
        print("=" * 64)
        if not CONTROLLER.exists():
            print(f"[FAIL] Missing controller file: {CONTROLLER}")
            raise SystemExit(2)
        self.ensure_venv_and_requirements()
        self.build_c_classifier()
        self.prewarm_jtag()
        ensure_private_dir(LOG_DIR)
        restart_count = 0
        print("=" * 64)
        print("Starting controller API with Python watchdog")
        print("Press Ctrl+C to stop.")
        print(f"Logs: {LOG_DIR}/controller_YYYYMMDD_HHMMSS.log")
        print("=" * 64)
        try:
            while True:
                stamp = time.strftime("%Y%m%d_%H%M%S")
                log_path = LOG_DIR / f"controller_{stamp}.log"
                print(f"[WATCHDOG] Launching controller API (restart count: {restart_count})")
                print(f"[WATCHDOG] Logging to {log_path}")
                code = self.stream_process_to_log([str(self.venv_python), "-u", str(CONTROLLER)], log_path)
                restart_count += 1
                print(f"[WATCHDOG] Controller exited with code {code}. Restarting in 0.5 seconds...")
                for cache_dir in PI_ROOT.glob("**/__pycache__"):
                    try:
                        shutil.rmtree(cache_dir)
                    except Exception:
                        pass
                time.sleep(0.5)
        except KeyboardInterrupt:
            print("[OK] Controller stopped.")
