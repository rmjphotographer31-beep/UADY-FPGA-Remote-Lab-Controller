# -*- coding: utf-8 -*-
"""Class-based Classic SSH/Quartus backend.

This module replaces the old large procedural FPGA.py implementation with a
small class that owns config loading, SSH connection, JTAG detection, server
listing, programming, upload, and logs.

Backwards-compatible functions are kept at the bottom so the existing GUI can
continue to call FPGA.detectar_fpgas_disponibles(), FPGA.dse(), etc.
"""
from __future__ import annotations

import datetime
import os
import sys
import traceback
from pathlib import Path
from typing import List, Optional, Tuple

try:
    import paramiko
except Exception:  # shown when Classic Mode SSH is used
    paramiko = None

from .classic_config import ClassicConfigStore, SETUP_MESSAGE
from .models import ClassicServerProfile

BASE_DIR = Path(__file__).resolve().parents[2]


def parse_quartus_cables(text: str) -> List[str]:
    """Parse board/cable names from quartus_pgm -l output."""
    cables: List[str] = []
    for line in (text or "").splitlines():
        raw = line.strip()
        if not raw:
            continue
        low = raw.lower()

        if "de-soc" in low or "de1" in low or "agilex" in low or "de-10" in low or "de10" in low:
            try:
                item = raw.split(None, 1)[1].strip()
            except Exception:
                item = raw
            if item and item not in cables:
                cables.append(item)
            continue

        if ")" in raw and ("USB" in raw or "Blaster" in raw):
            item = raw.split(")", 1)[1].strip()
            if item and item not in cables:
                cables.append(item)
            continue

        if "usb-blaster" in low or "usb-blasterii" in low:
            if raw not in cables:
                cables.append(raw)
    return cables


class ClassicFPGAProgrammer:
    """Classic Mode service for SSH + Quartus operations."""

    def __init__(self, config_store: Optional[ClassicConfigStore] = None) -> None:
        self.config_store = config_store or ClassicConfigStore(BASE_DIR)

    def classic_config_storage_path(self) -> str:
        return str(self.config_store.active_path())

    def obtener_config_servidor(self, key_path: str) -> Optional[ClassicServerProfile]:
        try:
            return self.config_store.profile_for_key(key_path)
        except Exception:
            print("[FAIL] Error reading the private Classic Mode server profile")
            return None

    def conectar_ssh(self, key_path: str, modo_netbird: bool = False):
        if paramiko is None:
            print("[FAIL] Falta instalar paramiko. Ejecuta: pip install paramiko")
            return None, None
        profile = self.obtener_config_servidor(key_path)
        if not profile:
            return None, None

        ssh = paramiko.SSHClient()
        ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        host = profile.host(modo_netbird)

        try:
            ssh.connect(host, username=profile.user, key_filename=key_path, timeout=15)
            return ssh, profile
        except Exception as exc:
            print(f"[FAIL] Error de conexión SSH: {exc}")
            return None, None

    @staticmethod
    def run_remote(ssh, cmd: str, timeout: int = 30) -> Tuple[int, str, str]:
        stdin, stdout, stderr = ssh.exec_command(cmd, timeout=timeout)
        out = stdout.read().decode("utf-8", errors="ignore")
        err = stderr.read().decode("utf-8", errors="ignore")
        code = stdout.channel.recv_exit_status()
        return code, out, err

    @staticmethod
    def jtag_index_for_board(board_or_cable: str) -> str:
        return "1" if "agilex" in (board_or_cable or "").lower() else "2"

    def detectar_fpgas_disponibles(self, key_path: str, modo_netbird: bool = False) -> List[str]:
        ssh, profile = self.conectar_ssh(key_path, modo_netbird)
        if not ssh:
            return []
        try:
            print("[INFO] Buscando hardware/JTAG en el servidor...")
            commands = []
            if profile.quartus_path:
                commands.append(f"{profile.quartus_path} -l")
            if profile.quartus_primepro_path:
                pro = profile.quartus_primepro_path
                commands.append(f"{pro if pro.endswith('quartus_pgm') else pro.rstrip('/') + '/quartus_pgm'} -l")

            detected: List[str] = []
            for cmd in commands:
                # Keep the old non-blocking-ish behavior. Some Quartus/jtagd
                # combinations print useful output but delay exit status.
                stdin, stdout, stderr = ssh.exec_command(cmd)
                out = stdout.read().decode("utf-8", errors="ignore")
                err = stderr.read().decode("utf-8", errors="ignore")
                for item in parse_quartus_cables(out + "\n" + err):
                    if item not in detected:
                        detected.append(item)

            if detected:
                print("[OK] JTAG detectado: " + ", ".join(detected))
            else:
                print("[WARN] No se detectaron cables JTAG con quartus_pgm -l")
            return detected
        except Exception as exc:
            print(f"[FAIL] Error al detectar hardware: {exc}")
            return []
        finally:
            ssh.close()

    def pgmlist(self, key_path: str, modo_netbird: bool = False) -> List[str]:
        ssh, profile = self.conectar_ssh(key_path, modo_netbird)
        if not ssh:
            return []
        try:
            paths = [p for p in (profile.base_project_path, profile.primepro_project_path) if p]
            paths_str = " ".join(f'"{p}"' for p in paths)
            code, out, err = self.run_remote(ssh, f"find {paths_str} -name '*.sof' 2>/dev/null", timeout=30)
            files = [x.strip() for x in out.splitlines() if x.strip()]
            return sorted(set(os.path.basename(f).replace(".sof", "") for f in files))
        except Exception as exc:
            print(f"[FAIL] Error al listar proyectos del servidor: {exc}")
            return []
        finally:
            ssh.close()

    def _program_via_jtag(
        self,
        ssh,
        profile: ClassicServerProfile,
        ip_local: str,
        cadena_fpga: str,
        filename: str,
        hostname: str,
        carrera: str,
    ) -> None:
        try:
            base_remote_dir = profile.base_dir_for_board(cadena_fpga)
            quartus_exe = profile.quartus_for_board(cadena_fpga)
            jtag_index = self.jtag_index_for_board(cadena_fpga)

            base_path = os.path.join(base_remote_dir, filename).replace("\\", "/")
            code, out, err = self.run_remote(ssh, f'find "{base_path}" -name "{filename}.sof" | head -n 1', timeout=20)
            sof_path = out.strip()
            if not sof_path:
                print(f"[FAIL] No se encontró {filename}.sof en {base_remote_dir}")
                return

            print(f"[INFO] Programando {cadena_fpga} con {sof_path}...")
            cmd = (
                f'"{quartus_exe}" -c "{cadena_fpga}" -m JTAG -o "p;{sof_path}@{jtag_index}" '
                f'</dev/null > /tmp/fpga_out.log 2>&1; RET=$?; cat /tmp/fpga_out.log; exit $RET'
            )
            code, out, err = self.run_remote(ssh, cmd, timeout=180)
            full_output = out + "\n" + err
            if code == 0 or "Configuration succeeded" in full_output or "Successfully performed" in full_output:
                print("[OK] Programación exitosa.")
                self.registrar_log(ip_local, cadena_fpga, filename, hostname, carrera, ssh, profile)
            else:
                print(f"[FAIL] Error de Quartus:\n{full_output.strip()}")
        except Exception as exc:
            print(f"[FAIL] Error en programación SSH: {exc}")
            print(traceback.format_exc())

    def ssh_conection(
        self,
        ip_local: str,
        cadena_fpga: str,
        filename: str,
        key_path: str,
        hostname: str,
        carrera: str,
        modo_netbird: bool = False,
    ) -> None:
        ssh, profile = self.conectar_ssh(key_path, modo_netbird)
        if not ssh:
            return
        try:
            self._program_via_jtag(ssh, profile, ip_local, cadena_fpga, filename, hostname, carrera)
        finally:
            ssh.close()

    def dse(
        self,
        ip_local: str,
        key_path: str,
        map_path: str,
        route: str,
        cadena_fpga: str,
        hostname: str,
        carrera: str,
        modo_netbird: bool = False,
    ) -> None:
        filename = os.path.basename(route).replace(".qpf", "")
        local_sof = os.path.join(map_path, "output_files", f"{filename}.sof")
        if not os.path.exists(local_sof):
            local_sof = os.path.join(map_path, f"{filename}.sof")
        if not os.path.exists(local_sof):
            print(f"[FAIL] No se encontró {filename}.sof localmente.")
            return

        ssh, profile = self.conectar_ssh(key_path, modo_netbird)
        if not ssh:
            return
        try:
            base_remote_dir = profile.base_dir_for_board(cadena_fpga)
            remote_path = os.path.join(base_remote_dir, filename).replace("\\", "/")
            ssh.exec_command(f'mkdir -p "{remote_path}"')
            sftp = ssh.open_sftp()
            try:
                print(f"[UPLOAD] Enviando {filename}.sof a {remote_path}...")
                sftp.put(local_sof, f"{remote_path}/{filename}.sof")
            finally:
                sftp.close()
        except Exception as exc:
            print(f"[FAIL] Error SFTP: {exc}")
            ssh.close()
            return

        try:
            self._program_via_jtag(ssh, profile, ip_local, cadena_fpga, filename, hostname, carrera)
        finally:
            ssh.close()

    def registrar_log(
        self,
        ip: str,
        fpga: str,
        filename: str,
        hostname: str,
        carrera: str,
        ssh,
        profile: ClassicServerProfile,
    ) -> None:
        timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        log_path = profile.log_path_for_board(fpga)
        log_cmd = f'mkdir -p "$(dirname {log_path})"; echo "FPGA {fpga} | {hostname}({ip}) | {carrera} | {filename} | {timestamp}" >> "{log_path}"'
        ssh.exec_command(log_cmd)

    def logs(self, key_path: str, modo_netbird: bool = False) -> None:
        ssh, profile = self.conectar_ssh(key_path, modo_netbird)
        if not ssh:
            return
        try:
            log_paths = [profile.log_file_path]
            if profile.primepro_log_file_path:
                log_paths.append(profile.primepro_log_file_path)
            for path in log_paths:
                if not path:
                    continue
                print(f"\n[LOG] {path}")
                code, out, err = self.run_remote(ssh, f'tail -n 15 "{path}" 2>/dev/null || true', timeout=15)
                print(out.strip() or "Sin registros.")
        except Exception as exc:
            print(f"[FAIL] Error al recuperar logs: {exc}")
        finally:
            ssh.close()


# ---------------------------------------------------------------------------
# Backwards-compatible API for the current gui.py
# ---------------------------------------------------------------------------
_PROGRAMMER = ClassicFPGAProgrammer()


def classic_config_storage_path():
    return _PROGRAMMER.classic_config_storage_path()


def obtener_config_servidor(key_path):
    profile = _PROGRAMMER.obtener_config_servidor(key_path)
    if profile is None:
        return None
    # Existing gui.py expects a ConfigParser section-like object with .get().
    return {
        "ip_local": profile.ip_local,
        "ip_netbird": profile.ip_netbird,
        "user": profile.user,
        "quartus_path": profile.quartus_path,
        "base_project_path": profile.base_project_path,
        "log_file_path": profile.log_file_path,
        "quartus_primepro_path": profile.quartus_primepro_path,
        "primepro_project_path": profile.primepro_project_path,
        "primepro_log_file_path": profile.primepro_log_file_path,
    }


def conectar_ssh(key, modo_netbird=False):
    return _PROGRAMMER.conectar_ssh(key, modo_netbird)


def _run_remote(ssh, cmd, timeout=30):
    return ClassicFPGAProgrammer.run_remote(ssh, cmd, timeout)


def _parse_quartus_cables(text):
    return parse_quartus_cables(text)


def detectar_fpgas_disponibles(key, modo_netbird=False):
    return _PROGRAMMER.detectar_fpgas_disponibles(key, modo_netbird)


def pgmlist(key, modo_netbird=False):
    return _PROGRAMMER.pgmlist(key, modo_netbird)


def _quartus_for_board(info, cadena_fpga):
    if isinstance(info, ClassicServerProfile):
        return info.quartus_for_board(cadena_fpga)
    profile = ClassicServerProfile.from_config_section("legacy", info)
    return profile.quartus_for_board(cadena_fpga)


def _base_dir_for_board(info, cadena_fpga):
    if isinstance(info, ClassicServerProfile):
        return info.base_dir_for_board(cadena_fpga)
    profile = ClassicServerProfile.from_config_section("legacy", info)
    return profile.base_dir_for_board(cadena_fpga)


def _jtag_index_for_board(cadena_fpga):
    return ClassicFPGAProgrammer.jtag_index_for_board(cadena_fpga)


def _program_via_jtag(ssh, info, ip_local, cadena_fpga, filename, hostname, carrera):
    profile = info if isinstance(info, ClassicServerProfile) else ClassicServerProfile.from_config_section("legacy", info)
    return _PROGRAMMER._program_via_jtag(ssh, profile, ip_local, cadena_fpga, filename, hostname, carrera)


def ssh_conection(ip_local, cadena_fpga, filename, key, hostname, carrera, modo_netbird=False):
    return _PROGRAMMER.ssh_conection(ip_local, cadena_fpga, filename, key, hostname, carrera, modo_netbird)


def dse(ip_local, key, map_path, route, cadena_fpga, hostname, carrera, modo_netbird=False):
    return _PROGRAMMER.dse(ip_local, key, map_path, route, cadena_fpga, hostname, carrera, modo_netbird)


def registrar_log(ip, fpga, filename, hostname, carrera, ssh, info):
    profile = info if isinstance(info, ClassicServerProfile) else ClassicServerProfile.from_config_section("legacy", info)
    return _PROGRAMMER.registrar_log(ip, fpga, filename, hostname, carrera, ssh, profile)


def logs(key, modo_netbird=False):
    return _PROGRAMMER.logs(key, modo_netbird)
