# -*- coding: utf-8 -*-
"""
Interfaz Duplex - Clean restart with Raspberry Pi AI/HAT dynamic JTAG workflow.

Classic mode is preserved. New Pi mode:
GUI sends .v/.sv + .sof to the Raspberry Pi controller. The Pi asks the
Quartus server which JTAG cables are connected, selects only from available
boards, controls locks/power/reset/status, and commands the server to program.
"""
import configparser
import datetime
import json
import hashlib
import os
import socket
import sys
import threading
import time
import traceback
import atexit
from collections import deque
from concurrent.futures import ThreadPoolExecutor
import tkinter as tk
from tkinter import filedialog, messagebox, simpledialog, ttk

try:
    import requests
except Exception:  # shown in GUI if used
    requests = None

try:
    from ttkthemes import ThemedTk
except Exception:
    ThemedTk = None

try:
    from PIL import Image, ImageTk
except Exception:
    Image = None
    ImageTk = None

import FPGA
from uady_gui_utils import ApiSession, treeview_sync_by_key, dedupe_rows_by_key
from uady_quartus_project_resolver import resolve_quartus_project, format_resolution_summary
from uady_secure_store import (
    get_or_create_user_secret,
    get_user_secret,
    set_user_secret,
    queue_token_path,
    legacy_migrate_json,
    read_json,
    write_json_secure,
    user_config_path,
)

APP_TITLE = "Granja Remota de FPGAs - Panel Único Pi AI/HAT + JTAG Dinámico"
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = str(user_config_path("gui_settings.ini"))
LEGACY_QUEUE_TOKEN_PATH = os.path.join(BASE_DIR, "my_queue_tokens.json")
QUEUE_TOKEN_PATH = str(queue_token_path())
GUI_CLIENT_TOKEN = get_or_create_user_secret("gui_client_token", nbytes=36)

# Migrate old per-folder queue tokens once. After this, all extracted GUI copies
# use the same per-user store instead of keeping secrets beside gui.py.
legacy_migrate_json(LEGACY_QUEUE_TOKEN_PATH, queue_token_path(), delete_legacy=True)


def load_queue_tokens():
    """Load queue cancel tokens from shared per-user storage, not from gui.py's folder."""
    return read_json(queue_token_path(), {})


def save_queue_tokens(tokens):
    """Persist local queue cancel tokens in protected user app storage.

    The file is shared by every extracted GUI copy for the same OS user. It is
    never bundled into the source folder and is written with private permissions
    where the OS supports chmod.
    """
    try:
        current = read_json(queue_token_path(), {})
        if current == (tokens or {}):
            return
        write_json_secure(queue_token_path(), tokens or {})
    except Exception as e:
        print(f"[WARN] Could not save queue tokens: {e}")


created_job_tokens = load_queue_tokens()

# v4.20: staged TMP handoff plus board_state temp cleanup and queue kick fix.
# The backend also enforces this, so duplicates are blocked even if two GUI requests race.
IN_FLIGHT_SUBMISSION_SIGNATURES = set()
IN_FLIGHT_SUBMISSION_LOCK = threading.Lock()
LAST_ACCEPTED_SUBMISSION_SIGNATURE = ""

LIVE_QUEUE_SECONDS = 0.25
LIVE_JTAG_SECONDS = 8
AUTO_QUEUE_TIMEOUT_SECONDS = 1
AUTO_JTAG_TIMEOUT_SECONDS = 4
AUTO_BACKOFF_MAX_SECONDS = 5

live_queue_enabled = False
live_jtag_enabled = False
live_queue_polling = False
live_jtag_polling = False
queue_refresh_fail_count = 0
jtag_refresh_fail_count = 0
queue_stream_enabled = True
queue_stream_running = False
queue_stream_start_lock = threading.Lock()
queue_stream_fail_count = 0
queue_stream_last_update_ts = 0.0
terminal_unlocked = False

# Shared bounded worker pool for short GUI background tasks.  The previous code
# created a new daemon Thread for nearly every button press and polling tick.
# A bounded pool prevents thread growth during classroom bursts while keeping
# all network/SSH/file work off Tkinter's mainloop.  Persistent SSE streaming
# still uses its own thread so it does not occupy a general worker forever.
GUI_BG_EXECUTOR = ThreadPoolExecutor(max_workers=6, thread_name_prefix="gui_bg")
PI_PROGRESS_ACTIVE_TASKS = 0
PI_PROGRESS_LOCK = threading.RLock()


atexit.register(lambda: GUI_BG_EXECUTOR.shutdown(wait=False, cancel_futures=True))


def submit_background(fn):
    try:
        return GUI_BG_EXECUTOR.submit(fn)
    except RuntimeError:
        # Executor may already be shutting down during window close; do not crash
        # Tk callbacks that race with shutdown.
        return None


def pi_progress_begin():
    global PI_PROGRESS_ACTIVE_TASKS
    with PI_PROGRESS_LOCK:
        PI_PROGRESS_ACTIVE_TASKS += 1
        should_start = PI_PROGRESS_ACTIVE_TASKS == 1
    if should_start:
        try:
            pi_progress.start(10)
        except Exception:
            pass


def pi_progress_end():
    global PI_PROGRESS_ACTIVE_TASKS
    with PI_PROGRESS_LOCK:
        PI_PROGRESS_ACTIVE_TASKS = max(0, PI_PROGRESS_ACTIVE_TASKS - 1)
        should_stop = PI_PROGRESS_ACTIVE_TASKS == 0
    if should_stop:
        try:
            pi_progress.stop()
        except Exception:
            pass


def ui_var_set_if_changed(var, value):
    try:
        value = str(value)
        if var.get() != value:
            var.set(value)
    except Exception:
        try:
            var.set(value)
        except Exception:
            pass


# ==========================
# Terminal access key helpers
# ==========================
# The GUI terminal key is not stored in gui.py, config.ini, or a bundled file.
# It is generated and verified by the Raspberry Pi controller so every GUI uses
# the same terminal key. Normal GUI users cannot change it from the GUI.

TERMINAL_LAST_VERIFY_ERROR = ""


def terminal_key_config_exists():
    return True


def verify_terminal_key(secret):
    """Verify the terminal access key against the Raspberry Pi controller."""
    global TERMINAL_LAST_VERIFY_ERROR
    TERMINAL_LAST_VERIFY_ERROR = ""
    try:
        data = api_post_json("/security/verify_terminal_key", {"terminal_key": str(secret or "")}, timeout=10)
        ok_value = bool(data.get("success") and data.get("unlocked"))
        if not ok_value:
            TERMINAL_LAST_VERIFY_ERROR = str(data.get("error") or data.get("message") or "Invalid terminal key")
        return ok_value
    except Exception as e:
        TERMINAL_LAST_VERIFY_ERROR = str(e)
        print(f"[FAIL] Terminal key verification failed: {e}")
        return False


def change_terminal_key_with_admin(admin_key, new_terminal_key):
    # Disabled by design: terminal key is managed on the Raspberry Pi only.
    return False, "pi_managed_only"


def terminal_key_help_text():
    return tr(
        "Enter the terminal key",
        "Entra la llave de terminal"
    )


# ==========================
# Resources/config
# ==========================
def resource_path(relative_path):
    try:
        base_path = sys._MEIPASS
    except Exception:
        base_path = os.path.abspath(".")
    return os.path.join(base_path, relative_path)


def load_icon(window):
    for rel in ("micro.ico", os.path.join("data", "micro.ico")):
        icon_path = resource_path(rel)
        if os.path.exists(icon_path):
            try:
                window.iconbitmap(icon_path)
                return
            except Exception:
                pass


def load_image(filename):
    if Image is None or ImageTk is None:
        return None
    for rel in (os.path.join("icons", filename), os.path.join("data", filename), filename):
        path = resource_path(rel)
        if os.path.exists(path):
            try:
                return ImageTk.PhotoImage(Image.open(path).resize((16, 16)))
            except Exception:
                return None
    return None


def read_app_config():
    """Read GUI runtime config from private per-user storage.

    The shipped folder intentionally has no config.ini. Settings saved by the
    GUI go to the user's app-storage folder so extracted GUI copies do not
    expose lab defaults or secrets.
    """
    cfg = configparser.ConfigParser()
    cfg.read(CONFIG_PATH, encoding="utf-8")
    return cfg


def default_pi_config_value(name, fallback=""):
    cfg = read_app_config()
    if cfg.has_section("raspberry_pi"):
        return cfg.get("raspberry_pi", name, fallback=fallback)
    return fallback


def default_pi_netbird_ip():
    return default_pi_config_value("netbird_ip", "")


def default_pi_port():
    return default_pi_config_value("port", "5050")


def default_pi_scheme():
    return default_pi_config_value("scheme", "http")


def default_pi_url():
    scheme = default_pi_scheme()
    port = default_pi_port()
    ip = default_pi_netbird_ip().strip()
    return f"{scheme}://{ip}:{port}" if ip else ""


def default_pi_key():
    """Load the Pi API key from protected user storage, with one-time legacy migration.

    The key is no longer kept in config.ini or gui.py. Existing installs that
    still have [raspberry_pi] api_key in config.ini are migrated silently.
    """
    env_key = os.environ.get("UADY_PI_API_KEY", "").strip()
    if env_key:
        return env_key
    stored = get_user_secret("pi_api_key", "").strip()
    if stored:
        return stored
    cfg = read_app_config()
    legacy = ""
    if cfg.has_section("raspberry_pi"):
        legacy = cfg.get("raspberry_pi", "api_key", fallback="").strip()
        if legacy:
            set_user_secret("pi_api_key", legacy)
            try:
                cfg.remove_option("raspberry_pi", "api_key")
                with open(CONFIG_PATH, "w", encoding="utf-8") as f:
                    cfg.write(f)
            except Exception:
                pass
            return legacy
    return ""


def default_pi_openssh_key_path():
    # Pi API mode does not use or store a local OpenSSH key. The Quartus-server
    # SSH key stays on the Raspberry Pi / server side. Classic Mode still lets
    # the user select a local SSH key manually for direct SSH programming.
    return ""


def save_pi_connection_config():
    cfg = read_app_config()
    if not cfg.has_section("raspberry_pi"):
        cfg.add_section("raspberry_pi")
    cfg.set("raspberry_pi", "scheme", default_pi_scheme())
    cfg.set("raspberry_pi", "netbird_ip", pi_netbird_ip_var.get().strip())
    cfg.set("raspberry_pi", "use_netbird", "true")
    cfg.set("raspberry_pi", "port", pi_port_var.get().strip() or "5050")
    api_key_value = pi_key_var.get().strip()
    if api_key_value:
        set_user_secret("pi_api_key", api_key_value)
    if cfg.has_option("raspberry_pi", "api_key"):
        cfg.remove_option("raspberry_pi", "api_key")
    # Never store local SSH key paths or secrets in config.ini.
    for secret_option in ("api_key", "terminal_key", "admin_key", "token", "openssh_key_path", "granja_key_path"):
        if cfg.has_option("raspberry_pi", secret_option):
            cfg.remove_option("raspberry_pi", secret_option)
    os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        cfg.write(f)
    try:
        os.chmod(CONFIG_PATH, 0o600)
    except Exception:
        pass
    update_pi_url_from_connection_vars()
    print(f"[OK] Raspberry Pi NetBird connection saved. Current Pi URL: {pi_base_url()}")


def update_pi_url_from_connection_vars():
    scheme = default_pi_scheme()
    port = pi_port_var.get().strip() or "5050"
    ip = pi_netbird_ip_var.get().strip()
    try:
        if ip:
            pi_url_var.set(f"{scheme}://{ip}:{port}")
            if pi_use_netbird_var.get():
                pi_connection_mode_var.set(f"NetBird: {ip}:{port}")
            else:
                pi_connection_mode_var.set("NetBird checkbox is required before connecting")
        else:
            pi_url_var.set("")
            pi_connection_mode_var.set("Pi NetBird IP not configured")
    except Exception:
        pass



def local_gui_user():
    """User identity for queue ownership/display is based on the computer running the GUI."""
    try:
        name = socket.gethostname().strip()
        if name:
            return name
    except Exception:
        pass
    return "unknown-computer"


# ==========================
# UI root
# ==========================
if ThemedTk:
    root = ThemedTk(theme="vista")
else:
    root = tk.Tk()
root.title(APP_TITLE)
root.geometry("1650x1020")
root.minsize(1400, 900)

# Start maximized/full screen by default.
# On Windows, "zoomed" opens the GUI maximized with the taskbar still visible.
# On Linux, the attributes fallback covers window managers that do not support "zoomed".
try:
    root.state("zoomed")
except Exception:
    try:
        root.attributes("-zoomed", True)
    except Exception:
        try:
            root.attributes("-fullscreen", True)
        except Exception:
            pass

# F11 toggles true fullscreen; Escape exits true fullscreen/maximized mode.
def toggle_true_fullscreen(event=None):
    try:
        root.attributes("-fullscreen", not bool(root.attributes("-fullscreen")))
    except Exception:
        pass

def exit_fullscreen(event=None):
    try:
        root.attributes("-fullscreen", False)
    except Exception:
        pass

root.bind("<F11>", toggle_true_fullscreen)
root.bind("<Escape>", exit_fullscreen)

load_icon(root)

style = ttk.Style()
try:
    style.configure("Header.TLabel", font=("Segoe UI", 14, "bold"))
    style.configure("SubHeader.TLabel", font=("Segoe UI", 10, "bold"))
except Exception:
    pass

root.columnconfigure(0, weight=1)
root.rowconfigure(0, weight=1)

main_frame = ttk.Frame(root, padding="10")
main_frame.grid(row=0, column=0, sticky="nsew")
main_frame.columnconfigure(0, weight=1)
main_frame.rowconfigure(1, weight=1)

# ==========================
# Language support
# ==========================
LANG = tk.StringVar(value="English")
_TEXT_WIDGETS = []

def tr(en_text, es_text):
    return es_text if LANG.get() == "Español" else en_text


def lang_is_es():
    try:
        return LANG.get() == "Español"
    except Exception:
        return False


_STATUS_ES = {
    "queued": "En cola",
    "running": "Ejecutando",
    "analyzing": "Analizando IA",
    "testing": "Probando",
    "completed": "Completado",
    "failed": "Falló",
    "cancelled": "Cancelado",
    "pending": "Pendiente",
    "sending": "Enviando",
    "receiving": "Recibiendo",
    "uploading": "Subiendo",
}
_ROLE_ES = {
    "Teacher": "Profesor",
    "Student": "Estudiante",
    "Background": "Fondo",
    "teacher": "Profesor",
    "student": "Estudiante",
    "background": "Fondo",
}
_MODE_ES = {
    "server_paths": "Rutas servidor",
    "code_server_sof": "Código + SOF servidor",
    "upload": "Subida local",
    "server_verilog_local_sof": "Verilog servidor + SOF local",
}


def localize_status_text(value):
    text = str(value or "")
    return _STATUS_ES.get(text, text) if lang_is_es() else text


def localize_role_text(value):
    text = str(value or "")
    return _ROLE_ES.get(text, text) if lang_is_es() else text


def localize_mode_text(value):
    text = str(value or "")
    return _MODE_ES.get(text, text) if lang_is_es() else text


def localize_enabled_text(value):
    return tr("Enabled", "Habilitado") if bool(value) else tr("Disabled", "Deshabilitado")


def localize_yes_no(value):
    return tr("yes", "sí") if value else tr("no", "no")


def localize_queue_message(msg):
    text = str(msg or "")
    if not lang_is_es():
        return text
    replacements = [
        ("waiting:", "esperando:"),
        ("You are next in line for", "Siguiente en línea para"),
        ("You are #", "Número #"),
        ("in line for", "en línea para"),
        ("ETA to start:", "ETA para iniciar:"),
        ("testing:", "probando:"),
        ("remaining on", "restante en"),
        ("completed on", "completado en"),
        ("test timer complete; slot cleared and released", "temporizador terminado; slot limpiado y liberado"),
        ("test timer complete; released", "temporizador terminado; liberado"),
        ("student test timer active:", "temporizador de prueba activo:"),
        ("running after smart FIFO assignment:", "ejecutando después de asignación FIFO inteligente:"),
        ("Teacher override:", "Anulación Profesor:"),
        ("bumped lower-priority job", "interrumpió job de menor prioridad"),
        ("assigned to", "asignado a"),
        ("interrupted by Teacher override; returned to queue from", "interrumpido por Profesor; regresó a cola desde"),
        ("slot cleared", "slot limpiado"),
        ("released", "liberado"),
        ("cancelled by creator", "cancelado por creador"),
        ("queued", "en cola"),
    ]
    for a, b in replacements:
        text = text.replace(a, b)
    return text


def register_text(widget, en_text, es_text):
    _TEXT_WIDGETS.append((widget, en_text, es_text))
    try:
        widget.configure(text=tr(en_text, es_text))
    except Exception:
        pass
    return widget

def apply_language():
    for widget, en_text, es_text in _TEXT_WIDGETS:
        try:
            widget.configure(text=tr(en_text, es_text))
        except Exception:
            pass
    try:
        root.title(tr("Remote FPGA Farm - Pi AI/HAT + Dynamic JTAG", "Granja Remota de FPGAs - Pi AI/HAT + JTAG Dinámico"))
    except Exception:
        pass
    try:
        # Table headings
        bh = {
            "instance": ("JTAG #", "JTAG #"),
            "board": ("Detected Type", "Tipo detectado"),
            "enabled": ("Enabled", "Habilitado"),
            "busy_time": ("Remain", "Restante"),
            "cable": ("Raw Cable Name from Server", "Cable real detectado del servidor"),
            "quartus": ("Quartus", "Quartus"),
        }
        for col, pair in bh.items():
            board_tree.heading(col, text=tr(pair[0], pair[1]))
    except Exception:
        pass
    try:
        qh = {
            "job_id": ("Job ID", "ID Job"),
            "mine": ("Mine", "Mío"),
            "status": ("Status", "Estado"),
            "priority": ("Role", "Rol"),
            "student": ("User", "Usuario"),
            "mode": ("Mode", "Modo"),
            "assigned_slot": ("Slot", "Slot"),
            "wait_eta": ("Wait", "Espera"),
            "jtag_instance": ("JTAG", "JTAG"),
            "test_time": ("Test", "Prueba"),
            "created": ("Queued", "En cola"),
            "started": ("Started", "Inicio"),
            "remaining": ("Remain", "Restante"),
        }
        for col, pair in qh.items():
            queue_tree.heading(col, text=tr(pair[0], pair[1]))
    except Exception:
        pass

def toggle_language():
    LANG.set("Español" if LANG.get() == "English" else "English")
    apply_language()
    try:
        refresh_queue(silent=True)
        refresh_boards(silent=True)
    except Exception:
        pass

header = register_text(ttk.Label(main_frame, text="", style="Header.TLabel"), "Remote FPGA Programming | v4.51 Program Button", "Programación Remota de FPGAs | v4.51 Botón Programar")
header.grid(row=0, column=0, sticky="w", pady=(0, 8))

# Main GUI uses tabs only for output separation:
#   1) Control Panel keeps AI programming controls visible and uncluttered.
#   2) Terminal tab shows full logs/output with more room.
content_frame = ttk.Frame(main_frame)
content_frame.grid(row=1, column=0, sticky="nsew")
content_frame.columnconfigure(0, weight=0)
content_frame.columnconfigure(1, weight=1)
content_frame.rowconfigure(0, weight=1)

sidebar_frame = register_text(ttk.LabelFrame(content_frame, text="", padding=10), "Menu", "Menú")
sidebar_frame.grid(row=0, column=0, sticky="nsw", padx=(0, 8))
sidebar_frame.columnconfigure(0, weight=1)

page_area = ttk.Frame(content_frame)
page_area.grid(row=0, column=1, sticky="nsew")
page_area.columnconfigure(0, weight=1)
page_area.rowconfigure(0, weight=1)

classic_tab = register_text(ttk.LabelFrame(page_area, text="", padding=10), "Classic Mode: Server/JTAG", "Modo clásico: servidor/JTAG")
classic_tab.grid(row=0, column=0, sticky="nsew")

pi_tab = register_text(ttk.LabelFrame(page_area, text="", padding=10), "Raspberry Pi AI/HAT + Dynamic JTAG", "Raspberry Pi AI/HAT + JTAG dinámico")
pi_tab.grid(row=0, column=0, sticky="nsew")

jtag_page = register_text(ttk.LabelFrame(page_area, text="", padding=10), "Real-Time JTAG Reading", "Lectura JTAG en tiempo real")
jtag_page.grid(row=0, column=0, sticky="nsew")

jobs_page = register_text(ttk.LabelFrame(page_area, text="", padding=10), "Real-Time Queue Jobs", "Jobs en cola en tiempo real")
jobs_page.grid(row=0, column=0, sticky="nsew")

terminal_page = ttk.Frame(page_area, padding="6")
terminal_page.grid(row=0, column=0, sticky="nsew")

current_page_var = tk.StringVar(value="Raspberry Pi")

def show_sidebar_page(page_name):
    classic_tab.grid_remove()
    pi_tab.grid_remove()
    jtag_page.grid_remove()
    jobs_page.grid_remove()
    terminal_page.grid_remove()
    if page_name == "classic":
        classic_tab.grid()
        current_page_var.set(tr("Classic Mode", "Modo clásico"))
    elif page_name == "jtag":
        jtag_page.grid()
        current_page_var.set(tr("JTAG Real-Time", "JTAG en tiempo real"))
    elif page_name == "jobs":
        jobs_page.grid()
        current_page_var.set(tr("Jobs Queue", "Cola de jobs"))
    elif page_name == "terminal":
        terminal_page.grid()
        current_page_var.set(tr("Terminal", "Terminal"))
    else:
        pi_tab.grid()
        current_page_var.set(tr("Raspberry Pi AI/HAT", "Raspberry Pi AI/HAT"))

ttk.Label(sidebar_frame, textvariable=current_page_var, style="SubHeader.TLabel").grid(row=0, column=0, sticky="ew", pady=(0, 10))
btn_side_pi = register_text(ttk.Button(sidebar_frame, text="", command=lambda: show_sidebar_page("pi")), "Raspberry Pi", "Raspberry Pi")
btn_side_pi.grid(row=1, column=0, sticky="ew", pady=3)
btn_side_jtag = register_text(ttk.Button(sidebar_frame, text="", command=lambda: show_sidebar_page("jtag")), "Real-Time JTAG", "JTAG en tiempo real")
btn_side_jtag.grid(row=3, column=0, sticky="ew", pady=3)
btn_side_jobs = register_text(ttk.Button(sidebar_frame, text="", command=lambda: show_sidebar_page("jobs")), "Queue Jobs / Cancel", "Cola de jobs / Cancelar")
btn_side_jobs.grid(row=4, column=0, sticky="ew", pady=3)
btn_side_classic = register_text(ttk.Button(sidebar_frame, text="", command=lambda: show_sidebar_page("classic")), "Classic Mode", "Modo clásico")
btn_side_classic.grid(row=2, column=0, sticky="ew", pady=3)
btn_side_terminal = register_text(ttk.Button(sidebar_frame, text="", command=lambda: request_terminal_access()), "Terminal", "Terminal")
btn_side_terminal.grid(row=5, column=0, sticky="ew", pady=3)
btn_lang = register_text(ttk.Button(sidebar_frame, text="", command=toggle_language), "Español", "English")
btn_lang.grid(row=6, column=0, sticky="ew", pady=(16,3))

terminal_frame = register_text(ttk.LabelFrame(terminal_page, text="", padding="8"), "Terminal Output", "Terminal de salida")
terminal_frame.grid(row=0, column=0, sticky="nsew")
terminal_frame.columnconfigure(0, weight=1)
terminal_frame.rowconfigure(0, weight=1)

termf = tk.Text(terminal_frame, height=38, font=("Consolas", 10), bg="white", wrap="none", undo=False)
termf.grid(row=0, column=0, sticky="nsew")
scrollbar = ttk.Scrollbar(terminal_frame, command=termf.yview)
termf.config(yscrollcommand=scrollbar.set)
scrollbar.grid(row=0, column=1, sticky="ns")
h_scrollbar = ttk.Scrollbar(terminal_frame, orient="horizontal", command=termf.xview)
termf.config(xscrollcommand=h_scrollbar.set)
h_scrollbar.grid(row=1, column=0, sticky="ew")

terminal_button_frame = ttk.Frame(terminal_frame)
terminal_button_frame.grid(row=2, column=0, columnspan=2, sticky="e", pady=(6, 0))
clean_terminal_mode_var = tk.BooleanVar(value=False)

def clean_terminal():
    termf.delete("1.0", "end")
    termf.insert("end", "Terminal cleaned.\n")

def clear_terminal():
    termf.delete("1.0", "end")


def lock_terminal():
    global terminal_unlocked
    terminal_unlocked = False
    try:
        termf.delete("1.0", "end")
        termf.insert("end", "Terminal locked. Enter key to open again.\n")
    except Exception:
        pass
    show_sidebar_page("pi")
    print("[OK] Terminal locked. Key required to open Terminal again.")

def copy_terminal():
    root.clipboard_clear()
    root.clipboard_append(termf.get("1.0", "end-1c"))

def save_terminal_log():
    path = filedialog.asksaveasfilename(defaultextension=".txt", filetypes=(("Text", "*.txt"), ("All", "*.*")))
    if path:
        with open(path, "w", encoding="utf-8", errors="ignore") as f:
            f.write(termf.get("1.0", "end-1c"))

def print_full_terminal_info():
    """
    Print a complete diagnostic snapshot to the Terminal page.
    This is manual, so it does not spam the terminal during automatic refresh.
    """
    def task():
        print("\n" + "=" * 72)
        print("[FULL INFO] Raspberry Pi / Quartus Server / JTAG / Queue snapshot")
        print("=" * 72)
        print(f"[GUI] Computer/user: {local_gui_user()}")
        print(f"[GUI] Local IP: {get_local_ip()}")
        print(f"[GUI] Pi connection mode: {pi_connection_mode_var.get()}")
        try:
            validate_pi_netbird_and_key()
            print("[GUI] Pi mode local OpenSSH key: not used / not stored")
        except Exception as e:
            print(f"[GUI] Pi connection validation: {e}")
        print(f"[GUI] Pi URL: {pi_base_url()}")
        print(f"[GUI] Selected force board display: {requested_board_var.get()}")
        print(f"[GUI] Selected force board request: {board_request_value() or 'Auto'}")
        print(f"[GUI] QPF path: {qpf_path_var.get().strip()}")
        print(f"[GUI] Verilog path: {verilog_path_var.get().strip()}")
        print(f"[GUI] QSF path: {qsf_path_var.get().strip()}")
        print(f"[GUI] SOF path: {sof_path_var.get().strip()}")
        print(f"[GUI] Queue role: {priority_role_label(queue_priority_var.get())}")
        print(f"[GUI] Student test time: {get_test_minutes_value()} minutes")
        endpoints = [
            ("STATUS", "/status", 8),
            ("JTAG FORCE READ", "/jtag?force=1", 45),
            ("BOARDS FORCE READ", "/boards?force=1", 45),
            ("QUEUE", "/queue", 30),
        ]
        for title, path, timeout in endpoints:
            print("\n" + "-" * 72)
            print(f"[FULL INFO] {title}")
            print("-" * 72)
            try:
                data = api_get(path, timeout=timeout)
                print(json.dumps(data, indent=2, ensure_ascii=False))
            except Exception as e:
                print(f"[ERROR] Could not read {title}: {e}")
        print("\n" + "=" * 72)
        print("[FULL INFO] Snapshot complete.")
        print("=" * 72 + "\n")
    run_thread(task, "Printing full terminal information")


def terminal_print_section(title):
    print("\n" + "=" * 72)
    print(title)
    print("=" * 72)


def terminal_test_connection():
    """Manual terminal test: NetBird + Pi API."""
    def task():
        terminal_print_section("[TEST CONNECTION] NetBird + Pi API")
        try:
            print(f"[GUI] Pi URL: {pi_base_url()}")
            print(f"[GUI] NetBird checked: {pi_use_netbird_var.get()}")
            print(f"[GUI] Optional local OpenSSH key: {pi_openssh_key_var.get().strip() or '(not selected; key stays on Raspberry Pi)'}")
            print(f"[GUI] Pi API key entered: {bool(pi_key_var.get().strip())}")
            validate_pi_netbird_and_key()
            print("[OK] Local GUI validation passed.")
        except Exception as e:
            print(f"[FAIL] Local GUI validation failed: {e}")
            return

        try:
            data = api_get("/status", timeout=10)
            print("[OK] Raspberry Pi /status responded.")
            print(json.dumps(data, indent=2, ensure_ascii=False))
            root.after(0, lambda: pi_status_var.set("Conectado" if data.get("success", True) else "Error"))
        except Exception as e:
            print(f"[FAIL] Raspberry Pi connection test failed: {e}")
    run_thread(task, "Terminal test: connection")


def terminal_test_jtag():
    """Manual terminal test: Quartus server JTAG detection through the Pi."""
    def task():
        terminal_print_section("[TEST JTAG] Quartus server cable detection")
        try:
            data = api_get("/jtag?force=1", timeout=45)
            cables = data.get("cables", []) or []
            if not data.get("success", True):
                print("[FAIL] JTAG setup/detection failed.")
                for err in data.get("errors", []) or []:
                    print(f"  - {err}")
                print("\nRaw response:")
                print(json.dumps(data, indent=2, ensure_ascii=False))
                return
            if not data.get("server_host"):
                print("[WARN] JTAG returned no server_host. The Pi-side Quartus server setup is missing.")
            print(f"[OK] JTAG test completed. Cables detected: {len(cables)}")
            for i, cable in enumerate(cables, start=1):
                print(f"  JTAG-{i}: {cable}")
            print("\nRaw response:")
            print(json.dumps(data, indent=2, ensure_ascii=False))
            refresh_boards(silent=True)
        except Exception as e:
            print(f"[FAIL] JTAG test failed: {e}")
    run_thread(task, "Terminal test: JTAG")


def terminal_test_boards_rotation():
    """Manual terminal test: board availability and JTAG usage rotation data."""
    def task():
        terminal_print_section("[TEST BOARDS / ROTATION] Available boards and JTAG usage")
        try:
            data = api_get("/boards?force=1", timeout=45)
            instances = data.get("board_instances") or data.get("raw_jtag_instances") or []
            if not instances:
                print("[WARN] No physical JTAG instances returned.")
            else:
                print("[OK] Physical JTAG instances:")
                for inst in instances:
                    enabled_txt = "Enabled" if inst.get("enabled") else "Disabled"
                    ai_txt = "yes" if inst.get("enabled") and inst.get("jtag_detected") else "no"
                    print(
                        f"  {inst.get('instance_id','')} | "
                        f"{inst.get('board','')} | "
                        f"{enabled_txt} | AI selectable={ai_txt} | "
                        f"available={inst.get('available')} | "
                        f"program_count={inst.get('program_count', 0)} | "
                        f"total_seconds={inst.get('total_program_seconds', 0)} | "
                        f"last_used={inst.get('last_used_at', '')} | "
                        f"{inst.get('detected_cable','')}"
                    )
            print("\nRaw response:")
            print(json.dumps(data, indent=2, ensure_ascii=False))
            refresh_boards(silent=True)
        except Exception as e:
            print(f"[FAIL] Boards/rotation test failed: {e}")
    run_thread(task, "Terminal test: boards and JTAG rotation")


def terminal_test_queue():
    """Manual terminal test: queue status including selected JTAG."""
    def task():
        terminal_print_section("[TEST QUEUE] Queue jobs and JTAG usage")
        try:
            data = api_get("/queue?force=1", timeout=30)
            jobs = data.get("jobs", {}) or {}
            queue = data.get("queue", []) or []
            print(f"[OK] Queue test completed. Queued/running IDs: {len(queue)} | known jobs: {len(jobs)}")
            if queue:
                print("Queue order:")
                for job_id in queue:
                    job = jobs.get(job_id, {})
                    print(
                        f"  {job_id} | status={job.get('status','')} | "
                        f"board={job.get('selected_board') or job.get('requested_board') or 'Auto'} | "
                        f"jtag={job.get('jtag_instance') or job.get('selected_instance_id') or ''} | "
                        f"cable={job.get('jtag_cable') or job.get('selected_jtag_cable') or ''}"
                    )
            usage = data.get("jtag_usage", {}) or {}
            if usage:
                print("\nJTAG usage counters:")
                for key, item in usage.items():
                    print(
                        f"  {key}: count={item.get('program_count', 0)}, "
                        f"success={item.get('success_count', 0)}, "
                        f"fail={item.get('fail_count', 0)}, "
                        f"total_seconds={int(item.get('total_program_seconds', 0) or 0)}, "
                        f"last_used={item.get('last_used_at', '')}"
                    )
            print("\nRaw response:")
            print(json.dumps(data, indent=2, ensure_ascii=False))
            refresh_queue(silent=True)
        except Exception as e:
            print(f"[FAIL] Queue test failed: {e}")
    run_thread(task, "Terminal test: queue")


def terminal_test_ai_select():
    """Manual terminal test: run AI select and show selected board/JTAG."""
    path = verilog_path_var.get().strip()
    if not path:
        print("[FAIL] Select a Verilog/SystemVerilog file or server path before running Test AI Select.")
        return

    requested = board_request_value()
    payload = {
        "requested_board": requested or None,
        "force_refresh": True,
    }

    if is_local_file(path):
        try:
            with open(path, "r", encoding="utf-8", errors="ignore") as f:
                payload["verilog_code"] = f.read()
            payload["filename"] = os.path.basename(path)
        except Exception as e:
            print(f"[FAIL] Could not read local Verilog file: {e}")
            return
    else:
        payload["verilog_path"] = path

    def task():
        terminal_print_section("[TEST AI SELECT] Board and physical JTAG selection")
        try:
            data = api_post_json("/ai/select_board", payload, timeout=90)
            board = data.get("selected_board") or "None"
            inst = data.get("selected_instance_id") or ""
            cable = data.get("selected_jtag_cable") or ""
            conf = data.get("confidence") or "unknown"
            print(f"[OK] AI selection result: board={board}, confidence={conf}, instance={inst}, cable={cable}")
            print(f"Reason: {data.get('reason', '')}")
            print("\nRaw response:")
            print(json.dumps(data, indent=2, ensure_ascii=False))
            root.after(0, lambda: selected_ai_var.set(f"AI: {board} {inst} ({conf})"))
            refresh_boards(silent=True)
        except Exception as e:
            print(f"[FAIL] AI select test failed: {e}")
    run_thread(task, "Terminal test: AI select")


def terminal_test_programming():
    """
    Legacy helper retained only for compatibility.
    Direct programming was removed in v3.67; programming now goes through the fair queue.
    """
    print("[INFO] Direct Test Programming was removed. Queuing the program instead.")
    queue_ai()


def terminal_test_all_no_program():
    """Run all safe terminal tests. Programming is not run automatically."""
    def task():
        terminal_print_section("[TEST ALL SAFE] Connection, JTAG, Boards/Rotation, Queue")
        print("[INFO] This safe test does NOT program an FPGA.")
        tests = [
            ("STATUS", "/status", 10),
            ("JTAG FORCE READ", "/jtag?force=1", 45),
            ("BOARDS / ROTATION", "/boards?force=1", 45),
            ("QUEUE / JTAG USAGE", "/queue", 30),
        ]
        for title, path, timeout in tests:
            print("\n" + "-" * 72)
            print(f"[TEST] {title}")
            print("-" * 72)
            try:
                data = api_get(path, timeout=timeout)
                if title == "JTAG FORCE READ":
                    for i, cable in enumerate(data.get("cables", []) or [], start=1):
                        print(f"  JTAG-{i}: {cable}")
                elif title == "BOARDS / ROTATION":
                    for inst in data.get("board_instances", []) or data.get("raw_jtag_instances", []) or []:
                        print(
                            f"  {inst.get('instance_id','')} | {inst.get('board','')} | "
                            f"enabled={inst.get('enabled')} | count={inst.get('program_count', 0)} | "
                            f"seconds={inst.get('total_program_seconds', 0)} | {inst.get('detected_cable','')}"
                        )
                elif title == "QUEUE / JTAG USAGE":
                    print(f"  queue_len={data.get('queue_length', 0)} running={data.get('running_count', 0)} testing={data.get('testing_count', 0)}")
                    plan = data.get("queue_plan", {}) or {}
                    print(f"  queue_plan_slots={plan.get('slot_count', 0)} policy={plan.get('policy', '')}")
                    print(f"  jtag_usage_entries={len(data.get('jtag_usage', {}) or {})}")
                print(json.dumps(data, indent=2, ensure_ascii=False))
            except Exception as e:
                print(f"[FAIL] {title}: {e}")
        print("\n[TEST ALL SAFE] Complete.")
        root.after(0, lambda: (refresh_queue(silent=True), refresh_boards(silent=True)))
    run_thread(task, "Terminal test: all safe checks")



def open_admin_terminal_key_tool(parent=None):
    messagebox.showinfo(
        tr("Terminal Key Managed by Raspberry Pi", "Llave de terminal administrada por Raspberry Pi"),
        terminal_key_help_text(),
        parent=root if parent is None else parent,
    )


def request_terminal_access():
    global terminal_unlocked
    if terminal_unlocked:
        show_sidebar_page("terminal")
        return

    dialog = tk.Toplevel(root)
    dialog.title(tr("Terminal Access", "Acceso a terminal"))
    dialog.transient(root)
    dialog.grab_set()
    dialog.resizable(False, False)
    dialog.columnconfigure(1, weight=1)

    message = terminal_key_help_text()

    ttk.Label(dialog, text=message, wraplength=420).grid(row=0, column=0, columnspan=2, sticky="w", padx=12, pady=(12, 6))
    ttk.Label(dialog, text=tr("Key:", "Llave:")).grid(row=1, column=0, sticky="w", padx=12, pady=6)
    key_var = tk.StringVar()
    key_entry = ttk.Entry(dialog, textvariable=key_var, show="*", width=38)
    key_entry.grid(row=1, column=1, sticky="ew", padx=12, pady=6)

    status_var = tk.StringVar(value="")
    ttk.Label(dialog, textvariable=status_var).grid(row=2, column=0, columnspan=2, sticky="w", padx=12, pady=6)

    def submit_key():
        global terminal_unlocked
        key = key_var.get()
        if verify_terminal_key(key):
            terminal_unlocked = True
            dialog.destroy()
            show_sidebar_page("terminal")
        else:
            TERMINAL_LAST_VERIFY_ERROR or tr("Invalid key.", "Llave inválida.")
            status_var.set(tr("Invalid key", "Llave inválida"))

    btn_frame = ttk.Frame(dialog)
    btn_frame.grid(row=3, column=0, columnspan=2, sticky="e", padx=12, pady=(6, 12))
    ttk.Button(btn_frame, text=tr("Cancel", "Cancelar"), command=dialog.destroy).grid(row=0, column=0, padx=4)
    ttk.Button(btn_frame, text=tr("Unlock Terminal", "Desbloquear terminal"), command=submit_key).grid(row=0, column=1, padx=4)

    key_entry.focus_set()
    dialog.bind("<Return>", lambda _e: submit_key())


def show_terminal_tab():
    request_terminal_access()


def show_control_tab():
    show_sidebar_page("pi")


register_text(ttk.Button(terminal_button_frame, text="", command=terminal_test_connection), "Test Connection", "Probar conexión").grid(row=0, column=0, padx=3, pady=2)
register_text(ttk.Button(terminal_button_frame, text="", command=terminal_test_jtag), "Test JTAG", "Probar JTAG").grid(row=0, column=1, padx=3, pady=2)
register_text(ttk.Button(terminal_button_frame, text="", command=terminal_test_boards_rotation), "Test Boards/Rotation", "Probar boards/rotación").grid(row=0, column=2, padx=3, pady=2)
register_text(ttk.Button(terminal_button_frame, text="", command=terminal_test_queue), "Test Queue", "Probar cola").grid(row=0, column=3, padx=3, pady=2)
register_text(ttk.Button(terminal_button_frame, text="", command=terminal_test_ai_select), "Test AI Select", "Probar AI").grid(row=0, column=4, padx=3, pady=2)
register_text(ttk.Button(terminal_button_frame, text="", command=terminal_test_all_no_program), "Test All Safe", "Probar todo seguro").grid(row=0, column=5, padx=3, pady=2)
register_text(ttk.Button(terminal_button_frame, text="", command=print_full_terminal_info), "Print Full Raw Info", "Mostrar info cruda").grid(row=0, column=6, padx=3, pady=2)

register_text(ttk.Button(terminal_button_frame, text="", command=lock_terminal), "LOCK TERMINAL", "BLOQUEAR TERMINAL").grid(row=1, column=0, padx=3, pady=2)
register_text(ttk.Button(terminal_button_frame, text="", command=show_control_tab), "Back to Controls", "Volver a controles").grid(row=1, column=1, padx=3, pady=2)
register_text(ttk.Button(terminal_button_frame, text="", command=clean_terminal), "Clean Terminal", "Limpiar terminal").grid(row=1, column=2, padx=3, pady=2)
register_text(ttk.Checkbutton(terminal_button_frame, text="", variable=clean_terminal_mode_var), "Clean mode OFF = full info", "Modo limpio OFF = toda info").grid(row=1, column=3, padx=3, pady=2)
register_text(ttk.Button(terminal_button_frame, text="", command=copy_terminal), "Copy Terminal", "Copiar terminal").grid(row=1, column=4, padx=3, pady=2)
register_text(ttk.Button(terminal_button_frame, text="", command=save_terminal_log), "Save Log", "Guardar log").grid(row=1, column=5, padx=3, pady=2)
register_text(ttk.Button(terminal_button_frame, text="", command=open_admin_terminal_key_tool), "Terminal Key Info", "Info llave terminal").grid(row=1, column=6, padx=3, pady=2)

register_text(ttk.Label(terminal_button_frame, text=""), "Admin JTAG control:", "Control admin JTAG:").grid(row=2, column=0, padx=3, pady=(6,2), sticky="w")
register_text(ttk.Button(terminal_button_frame, text="", command=lambda: selected_tree_jtag_action("disable")), "Disable JTAG - block new AI/FIFO jobs", "Deshabilitar JTAG - quitar de selección AI").grid(row=2, column=1, padx=3, pady=(6,2))
register_text(ttk.Button(terminal_button_frame, text="", command=lambda: selected_tree_jtag_action("enable")), "Enable JTAG - allow new AI/FIFO jobs", "Habilitar JTAG - permitir selección AI").grid(row=2, column=2, padx=3, pady=(6,2))
def print_remembered_jtag():
    if remembered_jtag_selection:
        print(
            "Remembered JTAG: "
            f"{remembered_jtag_selection.get('instance','')} | "
            f"{remembered_jtag_selection.get('board','')} | "
            f"{remembered_jtag_selection.get('enabled','')} | "
            f"{remembered_jtag_selection.get('detected_cable','')}"
        )
    else:
        print("Remembered JTAG: none")

register_text(ttk.Button(terminal_button_frame, text="", command=print_remembered_jtag), "Show Selected JTAG", "Mostrar JTAG seleccionado").grid(row=2, column=3, padx=3, pady=(6,2))

register_text(ttk.Label(terminal_button_frame, text=""), "Terminal queue routing:", "Enrutamiento de cola Terminal:").grid(row=3, column=0, padx=3, pady=(6,2), sticky="w")
register_text(ttk.Button(terminal_button_frame, text="", command=lambda: queue_ai("Teacher")), "Queue as Teacher Override", "Encolar como Profesor").grid(row=3, column=1, padx=3, pady=(6,2))
register_text(ttk.Button(terminal_button_frame, text="", command=lambda: queue_ai("Background")), "Queue as Background", "Encolar como Fondo").grid(row=3, column=2, padx=3, pady=(6,2))
register_text(ttk.Button(terminal_button_frame, text="", command=lambda: cancel_selected_queue_job()), "Cancel My Selected Job", "Cancelar mi job seleccionado").grid(row=3, column=3, padx=3, pady=(6,2))


def append_terminal_text(text):
    if not text:
        return
    try:
        if not termf or not termf.winfo_exists():
            sys.__stdout__.write(text)
            return
        clean_mode = True
        try:
            clean_mode = bool(clean_terminal_mode_var.get())
        except Exception:
            pass
        # Build one text block and insert once.  Inserting line-by-line into a
        # Tk Text widget is a visible hot path when JSON/status logs are large.
        # This keeps the exact same filtered terminal output while reducing the
        # number of Tcl/Tk calls made from each buffered flush.
        output_parts = []
        prefix = time.strftime("[%H:%M:%S] ") if not clean_mode else ""
        for part in str(text).splitlines(True):
            raw = part.rstrip("\n")
            if not raw.strip():
                if not clean_mode:
                    output_parts.append(part)
                continue
            # In clean mode, skip very noisy duplicate auto-refresh and /status JSON lines.
            if raw.startswith("[API ") or raw.startswith("[API_") or raw.startswith("API "):
                continue
            if clean_mode and raw.startswith("[AUTO-REFRESH"):
                continue
            if clean_mode and raw.strip() in ("{", "}"):
                continue
            if clean_mode and any(k in raw for k in ('"controller":', '"dry_run":', '"host":', '"hostname":', '"port":', '"server_host":', '"success": true', '"time":', '"use_gpio":')):
                continue
            if clean_mode:
                output_parts.append(raw + "\n")
            else:
                output_parts.append(prefix + part)
        if output_parts:
            termf.insert("end", "".join(output_parts))
        # Keep the terminal from growing forever and slowing the GUI.
        try:
            line_count = int(termf.index("end-1c").split(".")[0])
            keep_lines = 700 if clean_mode else 6000
            max_lines = 900 if clean_mode else 7000
            if line_count > max_lines:
                termf.delete("1.0", f"{line_count - keep_lines}.0")
        except Exception:
            pass
        termf.see("end")
    except Exception:
        pass


# Coalesce stdout/stderr writes into one Tk callback per burst.  Worker
# threads can print many small chunks during JSON dumps and SSH logs; scheduling
# root.after(0, ...) for every chunk can backlog the Tk event queue.
_TERMINAL_BUFFER = deque()
_TERMINAL_BUFFER_LOCK = threading.Lock()
_TERMINAL_FLUSH_SCHEDULED = False
_TERMINAL_FLUSH_MAX_CHUNKS = 200


def schedule_terminal_text(text):
    global _TERMINAL_FLUSH_SCHEDULED
    if not text:
        return
    try:
        with _TERMINAL_BUFFER_LOCK:
            _TERMINAL_BUFFER.append(str(text))
            if _TERMINAL_FLUSH_SCHEDULED:
                return
            _TERMINAL_FLUSH_SCHEDULED = True
        root.after(0, flush_terminal_text_buffer)
    except Exception:
        try:
            sys.__stdout__.write(str(text))
        except Exception:
            pass


def flush_terminal_text_buffer():
    global _TERMINAL_FLUSH_SCHEDULED
    chunks = []
    try:
        with _TERMINAL_BUFFER_LOCK:
            for _ in range(min(len(_TERMINAL_BUFFER), _TERMINAL_FLUSH_MAX_CHUNKS)):
                chunks.append(_TERMINAL_BUFFER.popleft())
            _TERMINAL_FLUSH_SCHEDULED = False
        if chunks:
            append_terminal_text("".join(chunks))
        with _TERMINAL_BUFFER_LOCK:
            if _TERMINAL_BUFFER and not _TERMINAL_FLUSH_SCHEDULED:
                _TERMINAL_FLUSH_SCHEDULED = True
                root.after(20, flush_terminal_text_buffer)
    except Exception:
        with _TERMINAL_BUFFER_LOCK:
            _TERMINAL_FLUSH_SCHEDULED = False


class Redirigir:
    def __init__(self):
        self._last = None
        self._last_count = 0

    def write(self, text):
        if not text:
            return
        try:
            # Tkinter must only be updated from the main UI thread.
            # Buffer writes so bursty JSON/SSH output does not enqueue thousands
            # of tiny callbacks ahead of button clicks and table redraws.
            schedule_terminal_text(text)
        except Exception:
            try:
                sys.__stdout__.write(text)
            except Exception:
                pass

    def flush(self):
        pass


sys.stdout = Redirigir()
sys.stderr = Redirigir()

folder_icon = load_image("folder.png")

MAJOR_OPTIONS = ["Mecatrónica", "Física", "Software", "Computación", "Otros"]

def current_pi_major():
    try:
        return (pi_major_var.get() or "Otros").strip() or "Otros"
    except Exception:
        try:
            return (carreratxt.get() or "Otros").strip() or "Otros"
        except Exception:
            return "Otros"


def current_classic_major():
    try:
        return (carreratxt.get() or current_pi_major() or "Otros").strip() or "Otros"
    except Exception:
        return current_pi_major()


jtag_page.columnconfigure(0, weight=1)
jtag_page.rowconfigure(1, weight=1)
jobs_page.columnconfigure(0, weight=1)
jobs_page.rowconfigure(1, weight=1)
terminal_page.columnconfigure(0, weight=1)
terminal_page.rowconfigure(0, weight=1)

# ==========================
# Classic tab
# ==========================
classic_tab.columnconfigure(0, weight=1)
classic_config = register_text(ttk.LabelFrame(classic_tab, text="", padding=10), "Quartus Server Connection", "Conexión al servidor Quartus")
classic_config.grid(row=0, column=0, sticky="ew", pady=5)
classic_config.columnconfigure(1, weight=1)

modo_netbird = tk.BooleanVar(value=False)
register_text(ttk.Checkbutton(classic_config, text="", variable=modo_netbird), "Remote Connection (NetBird)", "Conexión remota (NetBird)").grid(row=0, column=0, columnspan=3, sticky="w")

# Secure default: Classic Mode always uses the SSH key stored on the Raspberry Pi.
# The legacy laptop-key widgets are intentionally not shown in the normal GUI.
classic_use_pi_key_var = tk.BooleanVar(value=True)
keytxt = ttk.Entry(classic_config, state="readonly")
classic_key_hint_var = tk.StringVar(value="Pi secure mode: no local key needed")
classic_key_hint_label = ttk.Label(classic_config, textvariable=classic_key_hint_var)
classic_pi_project_map = {}

classic_project = register_text(ttk.LabelFrame(classic_tab, text="", padding=10), "Classic Programming Options", "Opciones de programación clásica")
classic_project.grid(row=1, column=0, sticky="ew", pady=5)
classic_project.columnconfigure(1, weight=1)

register_text(ttk.Label(classic_project, text=""), "Local Quartus project (.qpf):", "Proyecto Quartus local (.qpf):").grid(row=0, column=0, sticky="w", padx=5, pady=3)
routetxt = ttk.Entry(classic_project, state="readonly")
routetxt.grid(row=0, column=1, sticky="ew", padx=5, pady=3)
programtxt = ttk.Combobox(classic_project, state="readonly")
programtxt.grid(row=0, column=1, sticky="ew", padx=5, pady=3)
programtxt.grid_remove()

register_text(ttk.Label(classic_project, text=""), "Available JTAG/Cable:", "JTAG/Cable disponible:").grid(row=1, column=0, sticky="w", padx=5, pady=3)
fpgatxt = ttk.Combobox(classic_project, state="readonly", width=38)
fpgatxt.grid(row=1, column=1, sticky="w", padx=5, pady=3)

register_text(ttk.Label(classic_project, text=""), "Major:", "Carrera:").grid(row=2, column=0, sticky="w", padx=5, pady=3)
carreratxt = ttk.Combobox(classic_project, values=MAJOR_OPTIONS, state="readonly")
carreratxt.grid(row=2, column=1, sticky="w", padx=5, pady=3)
carreratxt.set("Otros")

check_value = tk.BooleanVar(value=False)


def update_classic_key_mode_ui():
    """Classic Mode can either use legacy direct SSH from the laptop or the secure Pi API.

    Secure Pi-backed mode is the default: the GUI never asks for or stores an
    OpenSSH private key, and the Raspberry Pi uses the key already saved in its
    protected storage. Legacy direct SSH remains available for old labs.
    """
    try:
        use_pi = bool(classic_use_pi_key_var.get())
        state = "disabled" if use_pi else "readonly"
        keytxt.config(state="normal")
        if use_pi:
            keytxt.delete(0, "end")
            keytxt.insert(0, "Raspberry Pi stored key")
            classic_key_hint_var.set(tr(
                "Pi secure mode: no local OpenSSH key is needed. JTAG/programming are routed through the Raspberry Pi.",
                "Modo seguro Pi: no se necesita llave OpenSSH local. JTAG/programación se enrutan por Raspberry Pi.",
            ))
        else:
            if keytxt.get() == "Raspberry Pi stored key":
                keytxt.delete(0, "end")
            classic_key_hint_var.set(tr(
                "Legacy direct mode: select a private key stored on this laptop.",
                "Modo directo heredado: selecciona una llave privada guardada en esta laptop.",
            ))
        keytxt.config(state=state)
        try:
            btn_examinar_key.config(state=("disabled" if use_pi else "normal"))
        except Exception:
            pass
    except Exception:
        pass


def getkey():
    if classic_use_pi_key_var.get():
        messagebox.showinfo(
            tr("Raspberry Pi key mode", "Modo llave Raspberry Pi"),
            tr(
                "Secure Classic mode uses the SSH key stored on the Raspberry Pi. No local OpenSSH key upload/selection is required.",
                "El modo clásico seguro usa la llave SSH guardada en la Raspberry Pi. No se requiere subir/seleccionar una llave OpenSSH local.",
            ),
        )
        return
    path = filedialog.askopenfilename(title="Seleccionar llave", filetypes=(("Llave", "*.*"),))
    if path:
        keytxt.config(state="normal")
        keytxt.delete(0, "end")
        keytxt.insert(0, path)
        keytxt.config(state="readonly")
        actualizar_lista_fpgas()


def getroute():
    path = filedialog.askopenfilename(filetypes=(("Quartus Project", "*.qpf"), ("Todos", "*.*")))
    if path:
        routetxt.config(state="normal")
        routetxt.delete(0, "end")
        routetxt.insert(0, path)
        routetxt.config(state="readonly")


def actualizar_lista_fpgas():
    fpgatxt.set("Buscando...")

    def tarea_pi():
        try:
            data = api_get("/jtag?force=1", timeout=60)
            lista = data.get("cables", []) or []
            root.after(0, lambda: (fpgatxt.config(values=lista), fpgatxt.set(lista[0] if lista else "No hay JTAG")))
            print("[OK] Classic JTAG usando Raspberry Pi key:")
            print(json.dumps(data, indent=2, ensure_ascii=False))
        except Exception as e:
            root.after(0, lambda: (fpgatxt.config(values=[]), fpgatxt.set("No hay JTAG")))
            print(f"[FAIL] Classic secure JTAG via Raspberry Pi failed: {e}")

    if classic_use_pi_key_var.get():
        submit_background(tarea_pi)
        return

    key = keytxt.get()
    if not key:
        print("[FAIL] Legacy Classic Mode needs a local OpenSSH key. Enable Raspberry Pi key mode to avoid this.")
        return

    def tarea():
        lista = FPGA.detectar_fpgas_disponibles(key, modo_netbird.get())
        root.after(0, lambda: (fpgatxt.config(values=lista), fpgatxt.set(lista[0] if lista else "No hay JTAG")))

    submit_background(tarea)


def check_button():
    if check_value.get():
        routetxt.grid_remove()
        programtxt.grid(row=0, column=1, sticky="ew", padx=5, pady=3)
        btn_examinar_proyecto.config(state="disabled")

        def cargar_pi():
            try:
                display_value = fpgatxt.get() or ""
                family = "pro" if any(x in display_value.lower() for x in ("agilex", "de10", "de-10")) else "standard"
                data = api_get(f"/server/projects?family={family}&limit=200", timeout=90)
                projects = data.get("projects", []) or []
                items = []
                classic_pi_project_map.clear()
                for idx, item in enumerate(projects, start=1):
                    name = os.path.basename(os.path.dirname(item.get("verilog_path", "") or item.get("project_folder", ""))) or os.path.basename(item.get("sof_path", "")) or f"project_{idx}"
                    display = f"{name} | {os.path.basename(item.get('sof_path', ''))}"
                    classic_pi_project_map[display] = item
                    items.append(display)
                root.after(0, lambda: (programtxt.config(values=items), programtxt.set(items[0] if items else "Vacío")))
                print(f"[OK] Classic server projects loaded through Raspberry Pi key: {len(items)}")
            except Exception as e:
                root.after(0, lambda: (programtxt.config(values=[]), programtxt.set("Vacío")))
                print(f"[FAIL] Could not list server projects through Raspberry Pi: {e}")

        def cargar():
            p = FPGA.pgmlist(keytxt.get(), modo_netbird.get())
            root.after(0, lambda: (programtxt.config(values=p), programtxt.set(p[0] if p else "Vacío")))

        submit_background(cargar_pi if classic_use_pi_key_var.get() else cargar)
    else:
        programtxt.grid_remove()
        routetxt.grid(row=0, column=1, sticky="ew", padx=5, pady=3)
        btn_examinar_proyecto.config(state="normal")


def programar_clasico():
    # Classic Mode is now a secure queue submitter.  The laptop does not need an
    # OpenSSH private key; the Raspberry Pi uses the key stored in protected Pi
    # storage, and the job appears in Queue Jobs / Cancel like any other job.
    if not bool(classic_use_pi_key_var.get()):
        classic_use_pi_key_var.set(True)
        update_classic_key_mode_ui()
        print("[INFO] Legacy direct Classic SSH is disabled in this build; using Raspberry Pi queue mode.")
    key = keytxt.get()
    fpga_seleccionada = fpgatxt.get()
    proyecto_seleccionado = programtxt.get()
    carrera_seleccionada = carreratxt.get()
    ruta_local_qpf = routetxt.get()
    is_servidor = check_value.get()
    modo_nb = modo_netbird.get()

    if not ensure_pi_connected_or_prompt("Classic Mode Program FPGA"):
        return

    if classic_use_pi_key_var.get():
        # Secure Classic compatibility path: use the Raspberry Pi API/queue, so the
        # SSH key stays on the Pi and is never uploaded/selected on the laptop.
        try:
            pi_major_var.set(carrera_seleccionada or "Otros")
        except Exception:
            pass
        if is_servidor:
            item = classic_pi_project_map.get(proyecto_seleccionado, {})
            if not item:
                print("[FAIL] Select a server project from the Classic list after clicking Program existing .sof file on server.")
                return
            verilog_path_var.set(item.get("verilog_path", ""))
            sof_path_var.set(item.get("sof_path", ""))
            qsf_path_var.set(item.get("qsf_path", ""))
        else:
            if not ruta_local_qpf or not os.path.exists(ruta_local_qpf):
                print("[FAIL] Selecciona un archivo .qpf local válido.")
                return
            info = auto_resolve_qpf_project_path(ruta_local_qpf)
            if info is None:
                return
        if fpga_seleccionada and fpga_seleccionada not in ("No hay JTAG", "Buscando..."):
            requested_board_var.set(fpga_seleccionada)
            requested_board_value_map[fpga_seleccionada] = fpga_seleccionada
        print("[INFO] Classic Mode: registering this programming request as a queue job. Use Queue Jobs / Cancel to cancel it before/during testing.")
        queue_ai(source_mode="classic_mode")
        return

    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip_alumno = s.getsockname()[0]
        s.close()
    except Exception:
        ip_alumno = "127.0.0.1"

    classic_program_button.config(state="disabled")
    classic_progress.start(10)

    def tarea_ssh():
        try:
            if is_servidor:
                FPGA.ssh_conection(ip_alumno, fpga_seleccionada, proyecto_seleccionado, key, socket.gethostname(), carrera_seleccionada, modo_nb)
            else:
                if not ruta_local_qpf or not os.path.exists(ruta_local_qpf):
                    print("[FAIL] Selecciona un archivo .qpf local válido.")
                    return
                carpeta_proyecto = os.path.dirname(ruta_local_qpf)
                FPGA.dse(ip_alumno, key, carpeta_proyecto, ruta_local_qpf, fpga_seleccionada, socket.gethostname(), carrera_seleccionada, modo_nb)
        except Exception as e:
            print(f"[FAIL] Error en proceso clásico: {e}")
        finally:
            root.after(0, lambda: (classic_progress.stop(), classic_program_button.config(state="normal")))

    submit_background(tarea_ssh)


# Hidden legacy button kept only so old helper functions do not break.
# Normal users do not select or upload any OpenSSH key from the laptop.
btn_examinar_key = register_text(ttk.Button(classic_config, text="", image=folder_icon, compound="left", command=getkey), "Browse", "Examinar")
update_classic_key_mode_ui()
btn_examinar_proyecto = register_text(ttk.Button(classic_project, text="", image=folder_icon, compound="left", command=getroute), "Browse", "Examinar")
btn_examinar_proyecto.grid(row=0, column=2, padx=5)
register_text(ttk.Button(classic_project, text="", command=actualizar_lista_fpgas), "Refresh JTAG", "Actualizar JTAG").grid(row=1, column=2, padx=5)

classic_action = ttk.Frame(classic_tab)
classic_action.grid(row=2, column=0, sticky="ew", pady=8)
classic_action.columnconfigure(1, weight=1)
register_text(ttk.Checkbutton(classic_action, text="", variable=check_value, command=check_button), "Program existing .sof file on server", "Programar archivo .sof existente en servidor").grid(row=0, column=0, sticky="w")
classic_progress = ttk.Progressbar(classic_action, mode="indeterminate")
classic_progress.grid(row=0, column=1, sticky="ew", padx=10)
classic_program_button = register_text(ttk.Button(classic_action, text="", command=programar_clasico), "Program FPGA", "Programar FPGA")
classic_program_button.grid(row=0, column=2, padx=5)


# ==========================
# Raspberry Pi AI/HAT tab
# ==========================
pi_tab.columnconfigure(0, weight=1)

pi_config_frame = register_text(ttk.LabelFrame(pi_tab, text="", padding=10), "Raspberry Pi AI/HAT Controller", "Controlador Raspberry Pi AI/HAT")
pi_config_frame.grid(row=0, column=0, sticky="ew", pady=5)
pi_config_frame.columnconfigure(1, weight=1)

# Raspberry Pi connection uses only the Pi API. Local SSH keys are not stored in
# this GUI config. Classic Mode still supports direct SSH programming separately.
pi_use_netbird_var = tk.BooleanVar(value=True)
pi_netbird_ip_var = tk.StringVar(value=default_pi_netbird_ip())
pi_port_var = tk.StringVar(value=default_pi_port())
pi_openssh_key_var = tk.StringVar(value=default_pi_openssh_key_path())
pi_connection_mode_var = tk.StringVar(value="")
pi_url_var = tk.StringVar(value=default_pi_url())

register_text(
    ttk.Checkbutton(pi_config_frame, text="", variable=pi_use_netbird_var, command=update_pi_url_from_connection_vars),
    "Remote Connection (NetBird)",
    "Conexión remota (NetBird)"
).grid(row=0, column=0, columnspan=4, sticky="w", padx=5, pady=3)

register_text(ttk.Label(pi_config_frame, text=""), "Pi NetBird IP:", "IP NetBird del Pi:").grid(row=1, column=0, sticky="w", padx=5, pady=3)
ttk.Entry(pi_config_frame, textvariable=pi_netbird_ip_var, width=32).grid(row=1, column=1, sticky="w", padx=5, pady=3)
register_text(ttk.Label(pi_config_frame, text=""), "Port:", "Puerto:").grid(row=1, column=2, sticky="e", padx=5, pady=3)
ttk.Entry(pi_config_frame, textvariable=pi_port_var, width=8).grid(row=1, column=3, sticky="w", padx=5, pady=3)

ttk.Label(pi_config_frame, textvariable=pi_connection_mode_var).grid(row=2, column=0, columnspan=4, sticky="w", padx=5, pady=3)

pi_status_var = tk.StringVar(value=tr("Not connected", "No conectado"))
register_text(ttk.Button(pi_config_frame, text="", command=lambda: test_pi_connection()), "Test Pi", "Probar Pi").grid(row=3, column=0, sticky="w", padx=5, pady=3)
ttk.Label(pi_config_frame, textvariable=pi_status_var).grid(row=3, column=1, sticky="w", padx=5)
register_text(ttk.Button(pi_config_frame, text="", command=lambda: save_pi_connection_config()), "Save NetBird Pi Connection", "Guardar conexión Pi NetBird").grid(row=3, column=2, sticky="w", padx=5, pady=3)

register_text(ttk.Label(pi_config_frame, text=""), "Pi API Key:", "Llave API Pi:").grid(row=4, column=0, sticky="w", padx=5, pady=3)
pi_key_var = tk.StringVar(value=default_pi_key())
pi_key_entry = ttk.Entry(pi_config_frame, textvariable=pi_key_var, show="*", width=38)
pi_key_entry.grid(row=4, column=1, sticky="w", padx=5, pady=3)
show_pi_key_var = tk.BooleanVar(value=False)

def toggle_pi_key_visible():
    pi_key_entry.config(show="" if show_pi_key_var.get() else "*")

register_text(ttk.Checkbutton(pi_config_frame, text="", variable=show_pi_key_var, command=toggle_pi_key_visible), "Show key", "Mostrar llave").grid(row=4, column=2, sticky="w", padx=5, pady=3)
update_pi_url_from_connection_vars()

file_frame = register_text(ttk.LabelFrame(pi_tab, text="", padding=10), "Files for AI + Programming", "Archivos para AI + programación")
file_frame.grid(row=5, column=0, sticky="ew", pady=5)
file_frame.columnconfigure(1, weight=1)

qpf_path_var = tk.StringVar()
verilog_path_var = tk.StringVar()
qsf_path_var = tk.StringVar()
sof_path_var = tk.StringVar()
requested_board_var = tk.StringVar(value="Auto")
pi_major_var = tk.StringVar(value="Otros")
# Maps dropdown display text to the actual value sent to the Pi controller.
# This lets the dropdown show every physical JTAG instance clearly.
requested_board_value_map = {"Auto": ""}
selected_ai_var = tk.StringVar(value=tr("AI/Queue: ready", "AI/Cola: listo"))
queue_priority_var = tk.StringVar(value="Student")
student_var = tk.StringVar(value=local_gui_user())
test_minutes_var = tk.StringVar(value="5")

register_text(ttk.Label(file_frame, text=""), "Project File (.qpf):", "Archivo de proyecto (.qpf):").grid(row=0, column=0, sticky="w", padx=5, pady=3)
qpf_entry = ttk.Entry(file_frame, textvariable=qpf_path_var)
qpf_entry.grid(row=0, column=1, sticky="ew", padx=5, pady=3)
qpf_entry.bind("<Return>", lambda _e: auto_resolve_qpf_project_path(qpf_path_var.get().strip()))
register_text(
    ttk.Button(file_frame, text="", command=lambda: select_qpf_project()),
    "Select",
    "Selectionar"
).grid(row=0, column=2, padx=5)

register_text(ttk.Label(file_frame, text=""), "Verilog/SystemVerilog for AI:", "Verilog/SystemVerilog para AI:").grid(row=1, column=0, sticky="w", padx=5, pady=3)
ttk.Entry(file_frame, textvariable=verilog_path_var, state="readonly").grid(row=1, column=1, columnspan=2, sticky="ew", padx=5, pady=3)

register_text(ttk.Label(file_frame, text=""), "QSF for AI accuracy:", "QSF para precisión AI:").grid(row=2, column=0, sticky="w", padx=5, pady=3)
ttk.Entry(file_frame, textvariable=qsf_path_var, state="readonly").grid(row=2, column=1, columnspan=2, sticky="ew", padx=5, pady=3)

register_text(ttk.Label(file_frame, text=""), "SOF for programming:", "SOF para programación:").grid(row=3, column=0, sticky="w", padx=5, pady=3)
ttk.Entry(file_frame, textvariable=sof_path_var, state="readonly").grid(row=3, column=1, columnspan=2, sticky="ew", padx=5, pady=3)

register_text(ttk.Label(file_frame, text=""), "Major:", "Carrera:").grid(row=4, column=0, sticky="w", padx=5, pady=3)
pi_major_combo = ttk.Combobox(file_frame, textvariable=pi_major_var, values=MAJOR_OPTIONS, state="readonly", width=24)
pi_major_combo.grid(row=4, column=1, sticky="w", padx=5, pady=3)

register_text(ttk.Label(file_frame, text=""), "Force board from connected JTAG:", "Forzar board según JTAG conectado:").grid(row=5, column=0, sticky="w", padx=5, pady=3)
requested_board_combo = ttk.Combobox(file_frame, textvariable=requested_board_var, values=["Auto"], state="readonly", width=28)
requested_board_combo.grid(row=5, column=1, sticky="w", padx=5, pady=3)
ttk.Label(file_frame, textvariable=selected_ai_var, style="SubHeader.TLabel").grid(row=5, column=2, sticky="e", padx=5)

register_text(ttk.Label(file_frame, text=""), "Queue role:", "Rol de cola:").grid(row=6, column=0, sticky="w", padx=5, pady=3)
priority_frame = ttk.Frame(file_frame)
priority_frame.grid(row=6, column=1, sticky="w", padx=5, pady=3)
queue_priority_var.set("Student")
register_text(ttk.Label(priority_frame, text=""), "Student GUI", "GUI estudiante").grid(row=0, column=0, padx=(0, 12), sticky="w")

register_text(ttk.Label(file_frame, text=""), "Student test time after programming (minutes):", "Tiempo de prueba después de programar (minutos):").grid(row=7, column=0, sticky="w", padx=5, pady=3)
test_time_frame = ttk.Frame(file_frame)
test_time_frame.grid(row=7, column=1, sticky="w", padx=5, pady=3)
test_minutes_combo = ttk.Combobox(test_time_frame, textvariable=test_minutes_var, values=["1", "3", "5", "10", "15", "20", "30"], width=8)
test_minutes_combo.grid(row=0, column=0, sticky="w")
register_text(ttk.Label(test_time_frame, text=""), "minutes", "minutos").grid(row=0, column=1, sticky="w", padx=(8,0))

register_text(ttk.Label(file_frame, text=""), "GUI User / Computer Name:", "Usuario GUI / Nombre de computadora:").grid(row=8, column=0, sticky="w", padx=5, pady=3)
ttk.Entry(file_frame, textvariable=student_var, width=35, state="readonly").grid(row=8, column=1, sticky="w", padx=5, pady=3)

register_text(ttk.Button(file_frame, text="", command=lambda: list_server_projects()), "List Server Projects", "Listar proyectos servidor").grid(row=8, column=2, padx=5, pady=3, sticky="e")

jtag_button_frame = ttk.Frame(jtag_page)
jtag_button_frame.grid(row=0, column=0, sticky="ew", pady=(0, 6))
register_text(ttk.Button(jtag_button_frame, text="", command=lambda: refresh_jtag()), "Refresh JTAG Now", "Actualizar JTAG ahora").grid(row=0, column=0, padx=3)
jtag_live_var = tk.StringVar(value="Live JTAG: waiting for Pi connection")
ttk.Label(jtag_button_frame, textvariable=jtag_live_var).grid(row=0, column=1, padx=12, sticky="w")

boards_frame = register_text(ttk.LabelFrame(jtag_page, text="", padding=10), "Real-Time JTAG from Quartus Server (quartus_pgm -l)", "JTAG en tiempo real desde servidor Quartus (quartus_pgm -l)")
boards_frame.grid(row=1, column=0, sticky="nsew", pady=5)
boards_frame.columnconfigure(0, weight=1)
boards_frame.rowconfigure(0, weight=1)

columns = ("instance", "board", "enabled", "busy_time", "cable", "quartus")
board_tree = ttk.Treeview(boards_frame, columns=columns, show="headings", height=18)
headings = {
    "instance": "JTAG #",
    "board": "Detected Type",
    "enabled": "Enabled",
    "busy_time": "Remain",
    "cable": "Raw Cable Name from Server",
    "quartus": "Quartus",
}
widths = {"instance": 110, "board": 120, "enabled": 80, "busy_time": 95, "cable": 440, "quartus": 85}
for col in columns:
    board_tree.heading(col, text=headings[col])
    board_tree.column(col, width=widths[col], anchor="w")
board_tree.grid(row=0, column=0, sticky="nsew")
board_scroll = ttk.Scrollbar(boards_frame, command=board_tree.yview)
board_tree.configure(yscrollcommand=board_scroll.set)
board_scroll.grid(row=0, column=1, sticky="ns")
board_xscroll = ttk.Scrollbar(boards_frame, orient="horizontal", command=board_tree.xview)
board_tree.configure(xscrollcommand=board_xscroll.set)
board_xscroll.grid(row=1, column=0, sticky="ew")
board_tree.bind("<<TreeviewSelect>>", lambda event: remember_selected_jtag(event))
board_source_var = tk.StringVar(value="Source: waiting for real-time JTAG read")
ttk.Label(boards_frame, textvariable=board_source_var).grid(row=2, column=0, sticky="w", pady=(6,0))

# Remember the last selected physical JTAG row.
# This lets the admin open Terminal and still lock/release/enable/disable
# the row selected earlier in Real-Time JTAG.
remembered_jtag_selection = {}
remembered_jtag_var = tk.StringVar(value="Selected JTAG: none")
ttk.Label(boards_frame, textvariable=remembered_jtag_var).grid(row=3, column=0, sticky="w", pady=(2,0))

jobs_button_frame = ttk.Frame(jobs_page)
jobs_button_frame.grid(row=0, column=0, sticky="ew", pady=(0, 6))
btn_cancel_job = register_text(ttk.Button(jobs_button_frame, text="", command=lambda: cancel_selected_queue_job()), "Cancel Job(s)", "Cancelar job(s)")
btn_cancel_job.grid(row=0, column=0, padx=3)
try:
    btn_cancel_job.configure(state="disabled")
except Exception:
    pass
register_text(ttk.Button(jobs_button_frame, text="", command=lambda: refresh_queue()), "Refresh Jobs Now", "Actualizar jobs ahora").grid(row=0, column=1, padx=3)
jobs_live_var = tk.StringVar(value="Live Queue: waiting for Pi connection")
ttk.Label(jobs_button_frame, textvariable=jobs_live_var).grid(row=0, column=2, padx=12, sticky="w")
queue_cancel_hint_var = tk.StringVar(value="Select one of your own jobs to enable cancel.")
ttk.Label(jobs_button_frame, textvariable=queue_cancel_hint_var).grid(row=1, column=0, columnspan=3, padx=3, sticky="w")

# Remember the selected queue job across automatic refreshes.
# The Treeview is redrawn every second, so selection must be restored by Job ID.
remembered_queue_job_id = ""
queue_focus_var = tk.StringVar(value="Selected job: none")

ACTIVE_CANCEL_STATUSES = {"receiving", "uploading", "queued", "pending", "analyzing", "running", "testing"}
queue_job_status_cache = {}
queue_job_mine_cache = {}
pending_cancel_job_ids = set()

# Immediate local UI rows for queue submissions.
# Large .sof uploads can take time before the Pi returns the real job_id.
local_pending_queue_jobs = {}

# Terminal jobs are kept in the table by the Pi. Report each failure once so the
# user sees the exact reason instead of only watching the row disappear.
reported_terminal_job_ids = set()

queue_frame = register_text(ttk.LabelFrame(jobs_page, text="", padding=10), "Real-Time Queue Jobs", "Jobs en cola en tiempo real")
queue_frame.grid(row=1, column=0, sticky="nsew", pady=5)
queue_frame.columnconfigure(0, weight=1)
queue_frame.rowconfigure(0, weight=1)

queue_columns = ("job_id", "mine", "status", "priority", "student", "major", "mode", "assigned_slot", "wait_eta", "jtag_instance", "test_time", "created", "started", "remaining")
queue_tree = ttk.Treeview(queue_frame, columns=queue_columns, show="headings", height=20, selectmode="extended")
queue_headings = {
    "job_id": "Job ID",
    "mine": "Mine",
    "status": "Status",
    "priority": "Role",
    "student": "User",
    "major": "Major",
    "mode": "Mode",
    "assigned_slot": tr("Slot", "Slot"),
    "wait_eta": tr("Wait", "Espera"),
    "jtag_instance": tr("JTAG", "JTAG"),
    "test_time": "Test",
    "created": "Queued",
    "started": "Started",
    "remaining": "Remain",
}
queue_widths = {"job_id": 78, "mine": 45, "status": 74, "priority": 76, "student": 120, "major": 105, "mode": 92, "assigned_slot": 88, "wait_eta": 66, "jtag_instance": 78, "test_time": 62, "created": 118, "started": 118, "remaining": 62}
for col in queue_columns:
    queue_tree.heading(col, text=queue_headings[col])
    queue_tree.column(col, width=queue_widths[col], anchor="w")
queue_tree.grid(row=0, column=0, sticky="nsew")
queue_scroll = ttk.Scrollbar(queue_frame, command=queue_tree.yview)
queue_tree.configure(yscrollcommand=queue_scroll.set)
queue_scroll.grid(row=0, column=1, sticky="ns")
queue_xscroll = ttk.Scrollbar(queue_frame, orient="horizontal", command=queue_tree.xview)
queue_tree.configure(xscrollcommand=queue_xscroll.set)
queue_xscroll.grid(row=1, column=0, sticky="ew")
queue_tree.bind("<<TreeviewSelect>>", lambda event: update_cancel_button_state(event))

queue_status_var = tk.StringVar(value="Queue: 0 jobs | real-time after Test Pi")
ttk.Label(queue_frame, textvariable=queue_status_var).grid(row=2, column=0, sticky="w", pady=(6,0))
ttk.Label(queue_frame, textvariable=queue_focus_var).grid(row=3, column=0, sticky="w", pady=(2,0))

pi_button_frame = ttk.Frame(pi_tab)
pi_button_frame.grid(row=7, column=0, sticky="ew", pady=6)
for i in range(10):
    pi_button_frame.columnconfigure(i, weight=0)
pi_button_frame.columnconfigure(9, weight=1)

pi_progress = ttk.Progressbar(pi_button_frame, mode="indeterminate")
pi_progress.grid(row=0, column=9, sticky="ew", padx=8)


def select_file(var, filetypes):
    path = filedialog.askopenfilename(filetypes=filetypes)
    if path:
        var.set(path)


def auto_resolve_qpf_project_path(path):
    """Resolve a selected Quartus .qpf into the files needed by the queue workflow."""
    path = str(path or "").strip().strip('"')
    if not path:
        return None
    if not os.path.exists(path) or not path.lower().endswith(".qpf"):
        selected_ai_var.set("Select a valid .qpf first")
        messagebox.showerror(
            tr("Invalid QPF", "QPF inválido"),
            tr("Please select a valid Quartus .qpf project file.", "Selecciona un archivo de proyecto Quartus .qpf válido.")
        )
        return None
    qpf_path_var.set(path)
    try:
        info = resolve_quartus_project(path)
        summary = format_resolution_summary(info)
        print("\n[QPF ONE-BUTTON AUTO-FIND]")
        print(summary)
        print(json.dumps(info, indent=2, ensure_ascii=False))

        # Clear stale paths first so an old .sof/.qsf is not accidentally reused.
        verilog_path_var.set("")
        qsf_path_var.set("")
        sof_path_var.set("")

        if info.get("selected_verilog"):
            verilog_path_var.set(info.get("selected_verilog"))
        if info.get("selected_qsf"):
            qsf_path_var.set(info.get("selected_qsf"))
        if info.get("selected_sof"):
            sof_path_var.set(info.get("selected_sof"))

        status = "QPF ready: .v/.qsf/.sof found" if info.get("success") else "QPF partial: check missing .sof/.v/.qsf"
        selected_ai_var.set(status)
        if not info.get("success"):
            messagebox.showwarning(tr("QPF Auto-Find", "Búsqueda automática QPF"), summary)
        return info
    except Exception as e:
        selected_ai_var.set("QPF auto-find failed")
        print(f"[QPF AUTO-FIND ERROR] {e}")
        messagebox.showerror(tr("QPF Auto-Find failed", "Falló búsqueda QPF"), str(e))
        return None


def select_qpf_project():
    """Pick only a Quartus .qpf; the GUI auto-fills .v/.qsf/.sof from it."""
    path = filedialog.askopenfilename(
        title=tr("Select Quartus project (.qpf)", "Seleccionar proyecto Quartus (.qpf)"),
        filetypes=(("Quartus project", "*.qpf"), ("All files", "*.*")),
    )
    if not path:
        return
    auto_resolve_qpf_project_path(path)


def browse_pi_openssh_key():
    messagebox.showinfo(
        tr("SSH key stored on Raspberry Pi", "Llave SSH guardada en Raspberry Pi"),
        tr(
            "Pi mode does not save or use a local SSH key. The Quartus SSH key stays on the Raspberry Pi. Classic Mode still lets you select a local SSH key manually.",
            "El modo Pi no guarda ni usa una llave SSH local. La llave SSH de Quartus se queda en la Raspberry Pi. El modo clásico todavía permite seleccionar una llave SSH local manualmente."
        ),
    )


def validate_pi_netbird_and_key():
    # Pi mode talks to the Raspberry Pi API only. No local SSH key is required,
    # validated, or stored by the GUI. The Quartus SSH key stays on the Pi side.
    if not pi_use_netbird_var.get():
        raise RuntimeError("Remote Connection (NetBird) must be checked before connecting to the Raspberry Pi.")
    if not pi_netbird_ip_var.get().strip():
        raise RuntimeError("Pi NetBird IP is not configured. Enter the Pi NetBird IP and click Save NetBird Pi Connection.")
    return ""


def pi_base_url():
    update_pi_url_from_connection_vars()
    return pi_url_var.get().rstrip("/")


def pi_headers():
    key = ""
    try:
        key = pi_key_var.get().strip()
    except Exception:
        key = ""
    if not key:
        key = default_pi_key()
    return {"X-API-Key": key} if key else {}


def _require_requests():
    if requests is None:
        raise RuntimeError("Falta instalar requests. Ejecuta: pip install requests")


_api_session = None


def _get_api_session():
    global _api_session
    if _api_session is None:
        _api_session = ApiSession(
            requests_module=requests,
            base_url_getter=pi_base_url,
            headers_getter=pi_headers,
            validator=validate_pi_netbird_and_key,
        )
    return _api_session


def api_get(path, timeout=15):
    return _get_api_session().get_json(path, timeout=timeout)


def api_post_json(path, payload, timeout=20):
    return _get_api_session().post_json(path, payload, timeout=timeout)


def api_post_files(path, fields, files, timeout=900):
    return _get_api_session().post_files(path, fields, files, timeout=timeout)


def api_stream_queue(timeout=(1, 30)):
    return _get_api_session().stream_sse_json("/stream/queue", timeout=timeout)


def _pi_status_text_connected() -> bool:
    try:
        txt = str(pi_status_var.get() or "").strip().lower()
    except Exception:
        txt = ""
    return txt in ("conectado", "connected", "ok") or "conect" in txt or "connect" in txt


def ensure_pi_connected_or_prompt(action_label="program") -> bool:
    """Prevent queue/program actions from silently failing when the Pi is not connected.

    This is intentionally checked before creating the optimistic local queue row so
    users do not see a fake queued job when the Raspberry Pi API is offline, the
    API key is wrong, or the NetBird IP has not been saved yet.
    """
    global auto_refresh_enabled, live_queue_enabled, live_jtag_enabled
    try:
        validate_pi_netbird_and_key()
    except Exception as e:
        messagebox.showwarning(
            tr("Connect to Raspberry Pi", "Conectar a Raspberry Pi"),
            tr(
                f"{action_label} cannot start because the Raspberry Pi connection is not configured.\n\n{e}\n\nEnter the Pi NetBird IP/API key, click Save NetBird Pi Connection, then click Test Pi.",
                f"{action_label} no puede iniciar porque la conexión con Raspberry Pi no está configurada.\n\n{e}\n\nIngresa la IP/API key del Pi, guarda la conexión NetBird y luego presiona Probar Pi."
            ),
        )
        return False

    if bool(auto_refresh_enabled) and _pi_status_text_connected():
        return True

    try:
        data = api_get("/status", timeout=4)
        if isinstance(data, dict) and data.get("success", True):
            auto_refresh_enabled = True
            live_queue_enabled = True
            live_jtag_enabled = True
            try:
                pi_status_var.set("Conectado")
                jobs_live_var.set(tr(f"Live Queue: ON, every {LIVE_QUEUE_SECONDS}s", f"Cola en vivo: ACTIVA cada {LIVE_QUEUE_SECONDS}s"))
                jtag_live_var.set(tr(f"Live JTAG: ON, every {LIVE_JTAG_SECONDS}s", f"JTAG en vivo: ACTIVO cada {LIVE_JTAG_SECONDS}s"))
            except Exception:
                pass
            return True
    except Exception as e:
        err = str(e)
        try:
            pi_status_var.set("No conectado")
            queue_status_var.set(tr("Pi controller offline. Click Test Pi before queueing/programming.", "Controlador Pi desconectado. Presiona Probar Pi antes de encolar/programar."))
        except Exception:
            pass
        print(f"[WARN] {action_label} blocked because Pi is not connected: {err}")
        connect_now = messagebox.askyesno(
            tr("Raspberry Pi not connected", "Raspberry Pi no conectado"),
            tr(
                f"{action_label} needs the Raspberry Pi controller first. The job was not queued.\n\nError: {err}\n\nDo you want to run Test Pi now?",
                f"{action_label} necesita primero el controlador Raspberry Pi. El job no fue encolado.\n\nError: {err}\n\n¿Quieres ejecutar Probar Pi ahora?"
            ),
        )
        if connect_now:
            try:
                show_sidebar_page("pi")
            except Exception:
                pass
            try:
                test_pi_connection()
            except Exception as test_error:
                print(f"[WARN] Could not start Test Pi automatically: {test_error}")
        return False

    messagebox.showwarning(
        tr("Raspberry Pi not ready", "Raspberry Pi no listo"),
        tr(
            f"{action_label} cannot start because the Raspberry Pi did not report a ready status. Click Test Pi and try again.",
            f"{action_label} no puede iniciar porque la Raspberry Pi no reportó estado listo. Presiona Probar Pi e intenta otra vez."
        ),
    )
    return False


def record_from_board_tree_selection():
    sel = board_tree.selection()
    if not sel:
        return {}
    vals = board_tree.item(sel[0], "values")
    # Real-Time JTAG columns:
    # instance, board, enabled, busy_time, cable, quartus
    return {
        "instance": vals[0] if len(vals) > 0 else "",
        "board": vals[1] if len(vals) > 1 else "",
        "enabled": vals[2] if len(vals) > 2 else "",
        "busy_time": vals[3] if len(vals) > 3 else "",
        "detected_cable": vals[4] if len(vals) > 4 else "",
        "quartus": vals[5] if len(vals) > 5 else "",
    }


def remember_selected_jtag(event=None):
    global remembered_jtag_selection
    rec = record_from_board_tree_selection()
    if not rec:
        return
    if not rec.get("board") or rec.get("board") == "Unknown" or not rec.get("detected_cable"):
        return
    remembered_jtag_selection = dict(rec)
    remembered_jtag_var.set(
        tr("Selected JTAG: ", "JTAG seleccionado: ") + f"{rec.get('instance','')} | {rec.get('board','')} | {rec.get('enabled','')} | {rec.get('detected_cable','')}"
    )


def selected_tree_jtag_record():
    # First try current active table selection.
    rec = record_from_board_tree_selection()
    if rec and rec.get("board") and rec.get("board") != "Unknown" and rec.get("detected_cable"):
        remember_selected_jtag()
        return rec

    # If the user selected a row, then opened Terminal or the table refreshed,
    # keep using the last remembered row.
    if remembered_jtag_selection:
        return dict(remembered_jtag_selection)

    return {}


def selected_tree_board():
    rec = selected_tree_jtag_record()
    if rec:
        board = rec.get("board", "")
        return board if board and board != "Unknown" else None
    value = board_request_value()
    return value or None


def selected_tree_jtag_action(action):
    rec = selected_tree_jtag_record()
    if not rec:
        print("[FAIL] No remembered JTAG row. Go to Real-Time JTAG, click one row once, then return to Terminal admin control.")
        return
    board = rec.get("board", "")
    cable = rec.get("detected_cable", "")
    if not board or board == "Unknown" or not cable:
        print("[FAIL] Selected row does not have a valid board and JTAG cable.")
        return

    payload = {"board": board, "detected_cable": cable, "action": action}
    def task():
        data = api_post_json("/jtag/instance/action", payload, timeout=30)
        if action in ("disable", "disabled"):
            remembered_jtag_selection["enabled"] = "Disabled"
            remembered_jtag_var.set(f"Selected JTAG: {rec.get('instance','')} | {board} | Disabled | {cable}")
        elif action in ("enable", "enabled"):
            remembered_jtag_selection["enabled"] = "Enabled"
            remembered_jtag_var.set(f"Selected JTAG: {rec.get('instance','')} | {board} | Enabled | {cable}")
        effect = data.get("effect") or ("Disabled: AI/FIFO will not select this JTAG slot." if action in ("disable", "disabled") else "Enabled: AI/FIFO may select this JTAG slot again.")
        print(f"[OK] JTAG action '{action}' using remembered row: {rec.get('instance')} | {board} | {cable}")
        print(f"[INFO] {effect}")
        print(json.dumps(data, indent=2, ensure_ascii=False))
        refresh_boards(silent=True)
        refresh_queue(silent=True)
    run_thread(task, f"JTAG action: {action}")


def fmt_seconds(value):
    try:
        value = int(value or 0)
    except Exception:
        value = 0
    m, s = divmod(max(0, value), 60)
    h, m = divmod(m, 60)
    if h:
        return f"{h:02d}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"


def priority_role_value(role):
    role = (role or "Student").strip().lower()
    if role == "teacher":
        return 10
    if role == "background":
        return 1
    return 5


def priority_role_label(role):
    role = (role or "Student").strip()
    if role.lower() == "teacher":
        return "Teacher"
    if role.lower() == "background":
        return "Background"
    return "Student"


def priority_role_display(role):
    label = priority_role_label(role)
    if label == "Teacher":
        return tr("Teacher", "Profesor")
    if label == "Background":
        return tr("Background", "Fondo")
    return tr("Student", "Estudiante")


def board_request_value():
    display_value = requested_board_var.get()
    return requested_board_value_map.get(display_value, "" if display_value == "Auto" else display_value)


def instance_display_label(inst):
    board = inst.get("board", "Unknown")
    instance_id = inst.get("instance_id", "")
    cable = inst.get("detected_cable", "")
    available = "available" if inst.get("available") else ("busy" if inst.get("busy") else "not available")
    if instance_id and cable:
        return f"{instance_id} | {board} | {cable} | {available}"
    if cable:
        return f"{board} | {cable} | {available}"
    return f"{board} | {available}"


def build_force_board_options(instances, boards):
    """
    Build Force Board dropdown values from live physical JTAG instances.
    It includes every connected board instance, not only one board type.
    """
    display_values = ["Auto"]
    value_map = {"Auto": ""}

    # First show every physical instance.
    for inst in instances:
        board = inst.get("board", "")
        if not board or board == "Unknown" or not inst.get("jtag_detected", True):
            continue
        label = instance_display_label(inst)
        value = inst.get("instance_id") or inst.get("detected_cable") or board
        if label not in value_map:
            display_values.append(label)
            value_map[label] = value

    # Then include board-family options so Auto can be restricted to any available board of that type.
    for name, b in boards.items():
        if b.get("jtag_detected"):
            count = b.get("available_count", b.get("detected_count", ""))
            label = f"Any {name} ({count} available)"
            if label not in value_map:
                display_values.append(label)
                value_map[label] = name

    return display_values, value_map



def infer_board_from_cable_gui(cable, boards):
    """GUI fallback inference when the Pi controller returns only /jtag cables."""
    text = str(cable or "").lower()

    # Prefer aliases from the Pi/controller config response if available.
    for name, b in (boards or {}).items():
        aliases = [name] + list(b.get("jtag_aliases", []) or [])
        for alias in aliases:
            alias = str(alias or "").strip().lower()
            if alias and alias in text:
                return name

    # Conservative fallback for the current lab cable names.
    if "de10" in text or "agilex" in text:
        return "DE10-Agilex"
    if "de-soc" in text or "de1" in text:
        return "DE1-SoC"
    return "Unknown"


def build_instances_from_jtag_cables(jtag_data, boards):
    """
    Last-resort GUI fallback:
    If /boards does not return raw_jtag_instances or board_instances, build rows
    directly from /jtag?force=1 so every connected cable is still visible.
    """
    cables = []
    if isinstance(jtag_data, dict):
        cables = jtag_data.get("cables", []) or []
    instances = []
    for idx, cable in enumerate(cables, start=1):
        board = infer_board_from_cable_gui(cable, boards)
        b = (boards or {}).get(board, {})
        instances.append({
            "instance_id": f"JTAG-{idx}",
            "board": board,
            "enabled": b.get("enabled", True) if board != "Unknown" else False,
            "jtag_detected": True,
            "available": True if board != "Unknown" else False,
            "busy": False,
            "busy_seconds_elapsed": 0,
            "busy_seconds_remaining": 0,
            "physical_status": b.get("physical_status", "unknown"),
            "power_state": b.get("power_state", "unknown"),
            "detected_cable": cable,
            "quartus_family": b.get("quartus_family", ""),
            "jtag_device_index": str(b.get("jtag_device_index", "")),
            "raw_index": idx,
            "source": "GUI derived from /jtag?force=1",
            "lock_key": f"{board}::{cable}" if board != "Unknown" else "",
        })
    return instances



def _dedupe_lines(lines, max_lines=30):
    seen = set()
    out = []
    for line in lines:
        s = str(line).strip()
        if not s:
            continue
        if s in seen:
            continue
        seen.add(s)
        out.append(s)
        if len(out) >= max_lines:
            break
    return out


def _extract_quartus_error_summary(data):
    """
    Pull only the useful Quartus lines from program_result stdout/stderr.
    This avoids flooding the Terminal with hundreds of repeated JSON/stdout lines.
    """
    texts = []

    def collect(obj):
        if isinstance(obj, dict):
            for key in ("stdout", "stderr", "message", "error"):
                val = obj.get(key)
                if isinstance(val, str) and val.strip():
                    texts.append(val)
            for val in obj.values():
                collect(val)
        elif isinstance(obj, list):
            for val in obj:
                collect(val)

    collect(data)

    interesting = []
    for text in texts:
        for line in str(text).splitlines():
            line = line.strip()
            if not line:
                continue
            low = line.lower()
            if (
                line.startswith("Error")
                or "error (" in low
                or "operation failed" in low
                or "unsuccessful" in low
                or "using programming cable" in low
                or "using programming file" in low
                or "command:" in low
                or "started programmer operation" in low
                or "ended programmer operation" in low
            ):
                interesting.append(line)
    return _dedupe_lines(interesting, max_lines=35)


def print_deploy_summary(data, label="[RESULT] Deploy result"):
    print(label)

    ai = data.get("ai_result", {}) if isinstance(data, dict) else {}
    program = data.get("program_result", {}) if isinstance(data, dict) else {}

    selected = (
        data.get("selected_board")
        or data.get("ai_selected_board")
        or ai.get("selected_board")
        or "None"
    )
    success = data.get("success", ai.get("success", False))
    confidence = ai.get("confidence", "")
    required = ai.get("required_features", [])
    reason = ai.get("reason", "")
    remote_sof = data.get("remote_sof", "")
    command = program.get("command", "")
    returncode = program.get("returncode", "")

    print("-" * 64)
    print(f"Success: {success}")
    print(f"Selected board: {selected}")
    if confidence:
        print(f"AI confidence: {confidence}")
    if required:
        print("Required features: " + ", ".join(map(str, required)))
    if reason:
        print(f"Reason: {reason}")
    selected_cable = data.get("selected_jtag_cable") or ai.get("selected_jtag_cable") or ""
    selected_instance = data.get("selected_instance_id") or ai.get("selected_instance_id") or ""
    if selected_instance:
        print(f"Selected instance: {selected_instance}")
    if selected_cable:
        print(f"Selected JTAG cable: {selected_cable}")
    if remote_sof:
        print(f"Remote SOF: {remote_sof}")
    test_minutes = data.get("test_minutes", "")
    test_timer = data.get("test_timer", {}) or {}
    if test_minutes != "":
        print(f"Student test time: {test_minutes} minutes")
    if test_timer:
        print(f"Test timer ends at: {test_timer.get('test_end_at', '')}")
    if command:
        print(f"Quartus command: {command}")
    if returncode != "":
        print(f"Quartus return code: {returncode}")

    errors = _extract_quartus_error_summary(data)
    if errors:
        print("\nQuartus/JTAG important output:")
        for line in errors:
            print(f"  {line}")

    fallback_attempts = data.get("fallback_attempts") or []
    if fallback_attempts:
        print("\nAdditional programming attempts:")
        for attempt in fallback_attempts:
            board = attempt.get("board", "")
            reason = attempt.get("reason", "")
            result = attempt.get("result", {}) or {}
            rc = result.get("returncode", "")
            ok = result.get("success", "")
            print(f"  board={board} success={ok} returncode={rc} reason={reason}")

    released = data.get("released") or {}
    if released:
        print(f"\nBoard released: {released.get('board', selected)} reason={released.get('reason', '')}")

    print("-" * 64)
    print("Tip: If programming fails, provide the correct .sof for the selected physical FPGA.")


def print_queue_submit_summary(data, label="[RESULT] Queue job"):
    print(label)
    job = data.get("job", {}) if isinstance(data, dict) else {}
    job_id = data.get("job_id") or job.get("job_id", "")
    board = data.get("requested_board") or job.get("requested_board") or job.get("selected_board") or "Auto"
    status = data.get("status") or job.get("status") or ""
    print("-" * 64)
    print(f"Queued job: {job_id}")
    if status:
        print(f"Status: {status}")
    print(f"Requested board: {board}")
    if job.get("planned_instance_id"):
        print(f"Assigned slot: {job.get('planned_instance_id')} | {job.get('planned_board')} | {job.get('planned_jtag_cable', '')}")
        print(f"Queue notice: {job.get('queue_position_message', '')}")
        print(f"Wait ETA: {fmt_seconds(job.get('wait_seconds', 0))}")
    if job.get("teacher_override"):
        print(f"Teacher override: bumped job {job.get('override_victim_job_id', '')} from {job.get('override_instance_id', '')}")
    print(f"Computer/user: {local_gui_user()}")
    print(f"Queue role: {priority_role_label(queue_priority_var.get())}")
    print(f"Student test time: {get_test_minutes_value()} minutes")
    print(f"Major: {current_pi_major()}")
    print("Source mode: queue / Raspberry Pi" + (" / Classic Mode" if str(job.get("source_mode", "")).lower() == "classic_mode" else ""))
    print("-" * 64)


def selected_queue_job_id():
    sel = queue_tree.selection()
    if not sel:
        return ""
    vals = queue_tree.item(sel[0], "values")
    return vals[0] if vals else ""


def selected_queue_job_student():
    sel = queue_tree.selection()
    if not sel:
        return ""
    vals = queue_tree.item(sel[0], "values")
    # Queue columns keep student/user at index 4.
    return vals[4] if len(vals) > 4 else ""


def queue_job_is_mine(job_id, job):
    """
    Display ownership memory.

    Cancel authority still requires the creator token, but the Mine column should not
    turn to "no" after a job is completed/cancelled and the token is removed.
    """
    if job_id in created_job_tokens:
        return True
    me = str(local_gui_user() or "").strip().lower()
    if not me:
        return False
    candidates = [
        job.get("student", ""),
        job.get("client_hostname", ""),
        job.get("user", ""),
        job.get("creator", ""),
    ]
    return any(str(x or "").strip().lower() == me for x in candidates)


def queue_job_status_raw(job_id):
    return str(queue_job_status_cache.get(job_id, "") or "").strip().lower()


def queue_job_is_cancelable(job_id):
    # v4.25: allow cancel when this GUI has the token OR the live queue row
    # clearly belongs to this same computer/user.  This fixes false "you did not
    # create this job" after running a newly extracted GUI folder or restarting.
    return bool(
        job_id
        and (job_id in created_job_tokens or bool(queue_job_mine_cache.get(job_id)))
        and queue_job_status_raw(job_id) in ACTIVE_CANCEL_STATUSES
        and job_id not in pending_cancel_job_ids
    )


def selected_queue_job_ids():
    ids = []
    try:
        for item in queue_tree.selection():
            vals = queue_tree.item(item, "values")
            if vals and vals[0]:
                ids.append(vals[0])
    except Exception:
        pass
    return ids


def selected_cancellable_job_ids(use_remembered=True):
    ids = selected_queue_job_ids()
    if not ids and use_remembered and remembered_queue_job_id:
        ids = [remembered_queue_job_id]
    clean = []
    seen = set()
    for jid in ids:
        if jid in seen:
            continue
        seen.add(jid)
        if queue_job_is_cancelable(jid):
            clean.append(jid)
    return clean


def now_compact_string():
    try:
        return datetime.datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
    except Exception:
        return ""


def refresh_queue_local_only():
    """Draw local pending rows immediately without waiting for Raspberry Pi response."""
    def draw():
        try:
            existing = set()
            for item in queue_tree.get_children():
                vals = queue_tree.item(item, "values")
                if vals and vals[0]:
                    existing.add(vals[0])
            for job_id, job in list(local_pending_queue_jobs.items()):
                if job_id in existing:
                    continue
                queue_tree.insert("", 0, values=(
                    job_id,
                    localize_yes_no(True),
                    localize_status_text(job.get("status", "sending")),
                    localize_role_text(job.get("priority_label", "Student")),
                    job.get("student") or job.get("client_hostname", ""),
                    job.get("major", ""),
                    localize_mode_text(job.get("kind", "upload")),
                    job.get("planned_instance_id", tr("sending", "enviando")),
                    "",
                    "",
                    f"{job.get('test_minutes', '')} min" if job.get("test_minutes", "") != "" else "",
                    job.get("created_at", ""),
                    "",
                    "",
                ))
            queue_status_var.set(tr("Queue: sending job to Raspberry Pi...", "Cola: enviando job al Raspberry Pi..."))
        except Exception:
            pass
    try:
        root.after(0, draw)
    except Exception:
        pass


def add_local_pending_queue_job(mode, requested_board, role, test_minutes, major="Otros"):
    """Add a local temporary queue row immediately, before upload/network finishes."""
    temp_id = f"local-{int(time.time() * 1000) % 100000000:08d}"
    local_pending_queue_jobs[temp_id] = {
        "job_id": temp_id,
        "mine": True,
        "status": "sending",
        "priority_label": role or "Student",
        "student": local_gui_user(),
        "client_hostname": local_gui_user(),
        "major": major or "Otros",
        "kind": mode or "upload",
        "planned_board": requested_board or "Auto",
        "planned_instance_id": tr("sending", "enviando"),
        "wait_seconds": 0,
        "jtag_instance": "",
        "test_minutes": test_minutes,
        "created_at": now_compact_string(),
        "started_at": "",
        "remaining_seconds": 0,
        "message": tr("Uploading/sending job to Raspberry Pi...", "Subiendo/enviando job al Raspberry Pi..."),
        "local_pending": True,
    }
    try:
        selected_ai_var.set(tr(f"Sending job to queue: {temp_id}", f"Enviando job a cola: {temp_id}"))
    except Exception:
        pass
    refresh_queue_local_only()
    return temp_id


def remove_local_pending_queue_job(temp_id):
    if temp_id and temp_id in local_pending_queue_jobs:
        local_pending_queue_jobs.pop(temp_id, None)
        refresh_queue_local_only()


def run_thread(fn, label="Procesando..."):
    # This function is called from button callbacks and live-refresh supervisors.
    # Keep network and SSH work off the Tk thread, but avoid creating unlimited
    # daemon threads if many students repeatedly click refresh/queue actions.
    root.after(0, pi_progress_begin)
    if label not in (None, "", "None"):
        print(f"[INFO] {label}")

    def wrapper():
        try:
            fn()
        except Exception as e:
            print(f"[FAIL] {e}")
            print(traceback.format_exc())
            try:
                if "Connection" in type(e).__name__ or "refused" in str(e).lower() or "max retries" in str(e).lower():
                    root.after(0, lambda: pi_status_var.set("Disconnected"))
                    root.after(0, lambda: queue_status_var.set(tr(
                        "Pi controller offline. Restart RUN_PI_CONTROLLER.sh on the Raspberry Pi.",
                        "Controlador Pi desconectado. Reinicia RUN_PI_CONTROLLER.sh en el Raspberry Pi."
                    )))
            except Exception:
                pass
        finally:
            root.after(0, pi_progress_end)

    submit_background(wrapper)


def test_pi_connection():
    """Test the authenticated Raspberry Pi API endpoint used by the GUI.

    The controller root URL is protected and a normal browser request will return
    HTTP 401 because browsers do not add the Pi API key automatically.  The GUI
    must test /status and send X-API-Key on every request.
    """
    def task():
        endpoint = f"{pi_base_url()}/status"
        data = api_get("/status", timeout=8)

        if not isinstance(data, dict):
            raise RuntimeError(f"Unexpected response from {endpoint}: expected JSON object")
        if data.get("success") is not True:
            detail = data.get("error") or data.get("message") or "controller returned success=false"
            raise RuntimeError(f"Pi connection test failed: {detail}")

        def set_connected():
            global auto_refresh_enabled, live_queue_enabled, live_jtag_enabled
            auto_refresh_enabled = True
            live_queue_enabled = True
            live_jtag_enabled = True
            pi_status_var.set("Conectado")
            jobs_live_var.set(tr(
                f"Live Queue: ON, every {LIVE_QUEUE_SECONDS}s",
                f"Cola en vivo: ACTIVA cada {LIVE_QUEUE_SECONDS}s",
            ))
            jtag_live_var.set(tr(
                f"Live JTAG: ON, every {LIVE_JTAG_SECONDS}s",
                f"JTAG en vivo: ACTIVO cada {LIVE_JTAG_SECONDS}s",
            ))

        root.after(0, set_connected)
        print(f"[OK] Authenticated Pi API connected: {endpoint}")
        print(f"Controller: {data.get('controller_name', data.get('controller', 'unknown'))}")
        print(f"Dry run: {data.get('dry_run', 'unknown')} | GPIO: {data.get('use_gpio', 'unknown')}")
        refresh_queue(silent=True)
        refresh_boards(silent=True)

    run_thread(task, "Probando conexión autenticada al Raspberry Pi")


def refresh_jtag():
    def task():
        data = api_get("/jtag?force=1", timeout=40)
        if not data.get("success", True):
            print("[FAIL] JTAG no está configurado o no se pudo detectar:")
            print(json.dumps(data, indent=2, ensure_ascii=False))
            return
        if not data.get("server_host"):
            print("[WARN] JTAG respondió sin server_host. Revisa la configuración privada del Quartus server en la Raspberry Pi.")
        print("[OK] JTAG detectado por el servidor:")
        print(json.dumps(data, indent=2, ensure_ascii=False))
        refresh_boards(silent=True)

    run_thread(task, "Actualizando JTAG desde Quartus server")


def refresh_boards(silent=False):
    def task():
        # Silent live polling reads /boards so Busy Time updates every second without forcing a full JTAG scan.
        # Manual refresh still uses /boards?force=1.
        data = api_get("/boards" if silent else "/boards?force=1", timeout=(1, AUTO_JTAG_TIMEOUT_SECONDS) if silent else 45)
        boards = data.get("boards", {})
        instances = data.get("raw_jtag_instances") or data.get("board_instances") or []
        fallback_note = ""

        # If the controller returned only catalog rows, instances can be missing or only
        # contain generic USB-Blaster config names. Use /jtag directly to show every cable.
        if not instances and not silent:
            try:
                jtag_data = api_get("/jtag?force=1", timeout=45)
                instances = build_instances_from_jtag_cables(jtag_data, boards)
                data["jtag"] = jtag_data
                data["raw_jtag_instances"] = instances
                fallback_note = " | GUI derived rows from /jtag?force=1"
            except Exception as e:
                fallback_note = f" | GUI /jtag derivation failed: {e}"

        # If old controller only returned one catalog row per family with jtag_detected=no,
        # do not show the misleading USB-Blaster config entries when actual /jtag cables exist.
        if (not silent) and len(instances) <= len(boards) and all(not inst.get("jtag_detected") for inst in instances):
            try:
                jtag_data = api_get("/jtag?force=1", timeout=45)
                raw_instances = build_instances_from_jtag_cables(jtag_data, boards)
                if raw_instances:
                    instances = raw_instances
                    data["jtag"] = jtag_data
                    data["raw_jtag_instances"] = instances
                    fallback_note = " | GUI used raw /jtag cable rows"
            except Exception as e:
                fallback_note = f" | GUI raw /jtag replacement failed: {e}"

        # Backward compatibility: if still no instances, build from boards/detected_cables.
        if not instances:
            for name, b in boards.items():
                detected_cables = b.get("detected_cables") or []
                if not detected_cables and b.get("jtag_detected"):
                    detected_cables = [b.get("detected_cable") or b.get("jtag_cable", "")]
                for idx, cable in enumerate([c for c in detected_cables if c], start=1):
                    instances.append({
                        "instance_id": f"{name}-{idx}",
                        "board": name,
                        "enabled": b.get("enabled", False),
                        "jtag_detected": True,
                        "available": b.get("available", False),
                        "busy": b.get("busy", False),
                        "busy_seconds_elapsed": b.get("busy_seconds_elapsed", 0),
                        "busy_seconds_remaining": b.get("busy_seconds_remaining", 0),
                        "physical_status": b.get("physical_status", "unknown"),
                        "power_state": b.get("power_state", "unknown"),
                        "detected_cable": cable,
                        "quartus_family": b.get("quartus_family", ""),
                    })

        def update_ui():
            global requested_board_value_map
            names, requested_board_value_map = build_force_board_options(instances, boards)
            if len(names) == 1:
                for name, b in boards.items():
                    if b.get("jtag_detected"):
                        label = f"Any {name}"
                        if label not in requested_board_value_map:
                            names.append(label)
                            requested_board_value_map[label] = name

            new_rows = [(
                inst.get("instance_id", ""),
                inst.get("board", ""),
                localize_enabled_text(inst.get("enabled", False)),
                # Show the same countdown as the queue Remain column.
                # The old value used busy_seconds_elapsed, which counted up and
                # made the JTAG table disagree with the queue remaining time.
                fmt_seconds(inst.get("busy_seconds_remaining", 0)),
                inst.get("detected_cable", ""),
                inst.get("quartus_family", ""),
            ) for inst in instances]

            restore_key = ""
            try:
                restore_key = remembered_jtag_selection.get("instance", "")
            except Exception:
                restore_key = ""
            treeview_sync_by_key(board_tree, new_rows, key_index=0, restore_key=restore_key)

            requested_board_combo.config(values=names)
            if requested_board_var.get() not in names:
                requested_board_var.set("Auto")
            source = data.get("real_time_source", "quartus_pgm -l")
            ts = data.get("timestamp", "")
            count = len(instances)
            jtag_info = data.get("jtag", {}) or {}
            cache_note = "cached" if jtag_info.get("cache_used") else "fresh"
            age = jtag_info.get("cache_age_seconds", 0)
            ui_var_set_if_changed(board_source_var, f"Source: {source} | Physical JTAG boards shown: {count} | Last read: {ts} | {cache_note}, age {age}s{fallback_note}")
            ui_var_set_if_changed(jtag_live_var, f"Live JTAG: {count} physical board(s) shown{fallback_note}")

        root.after(0, update_ui)

    if silent:
        global jtag_refresh_fail_count
        try:
            task()
            jtag_refresh_fail_count = 0
        except Exception as e:
            jtag_refresh_fail_count += 1
            try:
                root.after(0, lambda: jtag_live_var.set(tr(
                    f"Live JTAG: waiting for Pi /boards... failures={jtag_refresh_fail_count}",
                    f"JTAG en vivo: esperando Pi /boards... fallos={jtag_refresh_fail_count}"
                )))
            except Exception:
                pass
            if jtag_refresh_fail_count <= 3 or jtag_refresh_fail_count % 10 == 0:
                try:
                    sys.__stdout__.write(f"[AUTO-REFRESH JTAG skipped x{jtag_refresh_fail_count}] {e}\n")
                except Exception:
                    pass
    else:
        run_thread(task, "Actualizando boards disponibles")


def apply_queue_data_to_ui(data, source="poll"):
    """Render queue JSON from /queue or /stream/queue."""
    global remembered_queue_job_id, queue_refresh_fail_count, queue_stream_last_update_ts
    global reported_terminal_job_ids
    try:
        queued = data.get("queued_jobs", [])
        running = data.get("running_jobs") or []
        current = data.get("current_job")
        recent = data.get("recent_jobs", [])

        selected_before_refresh = selected_queue_job_id() or remembered_queue_job_id
        queue_job_status_cache.clear()
        queue_job_mine_cache.clear()

        rows = []
        if local_pending_queue_jobs:
            rows.extend(list(local_pending_queue_jobs.values()))

        if running:
            rows.extend(running)
        elif current:
            rows.append(current)
        rows.extend(queued)

        seen = {r.get("job_id") for r in rows if r.get("job_id")}
        for r in recent[:12]:
            rid = r.get("job_id")
            if rid and rid not in seen:
                rows.append(r)
                seen.add(rid)

        rows = list(dedupe_rows_by_key(rows, key="job_id"))

        new_values = []
        for job in rows:
            job_id = job.get("job_id", "")
            status = job.get("status", "")
            planned_slot = job.get("planned_instance_id") or ""
            wait_eta = fmt_seconds(job.get("wait_seconds", job.get("remaining_seconds", 0))) if status == "queued" else ""
            is_mine = queue_job_is_mine(job_id, job)
            if job_id:
                queue_job_status_cache[job_id] = status
                queue_job_mine_cache[job_id] = is_mine

            status_l = str(status or "").lower()
            if job_id and is_mine and status_l == "failed" and job_id not in reported_terminal_job_ids:
                reported_terminal_job_ids.add(job_id)
                reason = str(job.get("failure_reason") or job.get("message") or "Programming job failed")
                print(f"[JOB FAILED] {job_id}: {reason}")
                try:
                    selected_ai_var.set(f"Failed {job_id}: {reason}"[:300])
                    queue_status_var.set(f"Failed {job_id}: {reason}"[:300])
                except Exception:
                    pass

            new_values.append((
                job_id,
                localize_yes_no(is_mine),
                localize_status_text(status),
                localize_role_text(priority_role_display(job.get("priority_label") or job.get("priority_role") or job.get("priority", "Student"))),
                job.get("student") or job.get("client_hostname", ""),
                job.get("major", ""),
                localize_mode_text(job.get("kind", job.get("mode", ""))),
                planned_slot,
                wait_eta,
                job.get("jtag_instance") or job.get("selected_instance_id") or "",
                f"{job.get('test_minutes', '')} min" if job.get("test_minutes", "") != "" else "",
                job.get("created_at", ""),
                job.get("started_at", ""),
                fmt_seconds(job.get("remaining_seconds", 0)),
            ))

        item_by_job_id = treeview_sync_by_key(queue_tree, new_values, key_index=0, restore_key=selected_before_refresh)

        if selected_before_refresh and selected_before_refresh in item_by_job_id:
            try:
                remembered_queue_job_id = selected_before_refresh
                ui_var_set_if_changed(queue_focus_var, tr(f"Selected job: {remembered_queue_job_id}", f"Job seleccionado: {remembered_queue_job_id}"))
            except Exception:
                pass
        elif remembered_queue_job_id and remembered_queue_job_id not in item_by_job_id:
            remembered_queue_job_id = ""
            ui_var_set_if_changed(queue_focus_var, tr("Selected job: none", "Job seleccionado: ninguno"))

        qlen = data.get("queue_length", 0)
        running_count = data.get("running_count", len([j for j in running if j.get("status") == "running"]))
        analyzing_count = data.get("analyzing_count", len([j for j in running if j.get("status") == "analyzing"]))
        testing_count = data.get("testing_count", len([j for j in running if j.get("status") == "testing"]))
        queue_refresh_fail_count = 0
        queue_stream_last_update_ts = time.time()
        mode_label = "stream" if source == "stream" else "poll"
        recent_failed_count = len([j for j in recent if str(j.get("status") or "").lower() == "failed"])
        if lang_is_es():
            current_text = f" | Ejecutando: {running_count}" if running_count else ""
            analyzing_text = f" | Analizando IA: {analyzing_count}" if analyzing_count else ""
            testing_text = f" | Probando: {testing_count}" if testing_count else ""
            failed_text = f" | Fallidos recientes: {recent_failed_count}" if recent_failed_count else ""
            ui_var_set_if_changed(queue_status_var, f"Cola: {qlen} pendientes{analyzing_text}{current_text}{testing_text}{failed_text}")
            ui_var_set_if_changed(jobs_live_var, f"Cola en vivo: {mode_label} | pendientes: {qlen} | analizando IA: {analyzing_count} | ejecutando: {running_count} | probando: {testing_count}{failed_text}")
        else:
            current_text = f" | Running: {running_count}" if running_count else ""
            analyzing_text = f" | AI analyzing: {analyzing_count}" if analyzing_count else ""
            testing_text = f" | Testing: {testing_count}" if testing_count else ""
            failed_text = f" | Recent failed: {recent_failed_count}" if recent_failed_count else ""
            ui_var_set_if_changed(queue_status_var, f"Queue: {qlen} pending{analyzing_text}{current_text}{testing_text}{failed_text}")
            ui_var_set_if_changed(jobs_live_var, f"Live Queue: {mode_label} | pending: {qlen} | AI analyzing: {analyzing_count} | running: {running_count} | testing: {testing_count}{failed_text}")
        update_cancel_button_state()
    except Exception as e:
        try:
            sys.__stdout__.write(f"[QUEUE UI render skipped] {e}\n")
        except Exception:
            pass


# Queue streams and manual polls can arrive faster than Tk can repaint the
# Treeview.  Keep only the newest snapshot pending for the UI thread; this
# preserves the visible final state while avoiding a backlog of stale redraws.
_QUEUE_UI_PENDING = {"data": None, "source": "poll"}
_QUEUE_UI_LOCK = threading.Lock()
_QUEUE_UI_SCHEDULED = False


def schedule_queue_ui_update(data, source="poll"):
    global _QUEUE_UI_SCHEDULED
    try:
        with _QUEUE_UI_LOCK:
            _QUEUE_UI_PENDING["data"] = data
            _QUEUE_UI_PENDING["source"] = source
            if _QUEUE_UI_SCHEDULED:
                return
            _QUEUE_UI_SCHEDULED = True
        root.after(0, flush_queue_ui_update)
    except Exception:
        try:
            sys.__stdout__.write("[QUEUE UI schedule failed]\n")
        except Exception:
            pass


def flush_queue_ui_update():
    global _QUEUE_UI_SCHEDULED
    try:
        with _QUEUE_UI_LOCK:
            data = _QUEUE_UI_PENDING.get("data")
            source = _QUEUE_UI_PENDING.get("source", "poll")
            _QUEUE_UI_PENDING["data"] = None
            _QUEUE_UI_SCHEDULED = False
        if data is not None:
            apply_queue_data_to_ui(data, source=source)
        with _QUEUE_UI_LOCK:
            if _QUEUE_UI_PENDING.get("data") is not None and not _QUEUE_UI_SCHEDULED:
                _QUEUE_UI_SCHEDULED = True
                root.after(0, flush_queue_ui_update)
    except Exception as e:
        try:
            sys.__stdout__.write(f"[QUEUE UI flush skipped] {e}\n")
        except Exception:
            pass
        with _QUEUE_UI_LOCK:
            _QUEUE_UI_SCHEDULED = False




def refresh_queue(silent=False):
    def task():
        data = api_get("/queue?force=1", timeout=30)
        schedule_queue_ui_update(data, source="poll")
        if not silent:
            print("[OK] Estado de la cola:")
            print(json.dumps(data, indent=2, ensure_ascii=False))

    if silent:
        global queue_refresh_fail_count
        try:
            task()
            queue_refresh_fail_count = 0
        except Exception as e:
            queue_refresh_fail_count += 1
            try:
                root.after(0, lambda: (
                    pi_status_var.set("Pi desconectado"),
                    jobs_live_var.set(tr(
                        f"Live Queue: waiting for Pi /queue... failures={queue_refresh_fail_count}",
                        f"Cola en vivo: esperando Pi /queue... fallos={queue_refresh_fail_count}"
                    ))
                ))
            except Exception:
                pass
            if queue_refresh_fail_count <= 3 or queue_refresh_fail_count % 10 == 0:
                try:
                    sys.__stdout__.write(f"[AUTO-REFRESH queue skipped x{queue_refresh_fail_count}] {e}\n")
                except Exception:
                    pass
    else:
        run_thread(task, "Actualizando cola de programación")


def queue_stream_worker():
    """Background realtime queue stream using one persistent HTTP connection."""
    global queue_stream_running, queue_stream_fail_count, queue_refresh_fail_count
    try:
        while auto_refresh_enabled and live_queue_enabled and queue_stream_enabled:
            try:
                for data in api_stream_queue(timeout=(3, 45)):
                    if not (auto_refresh_enabled and live_queue_enabled and queue_stream_enabled):
                        break
                    queue_stream_fail_count = 0
                    queue_refresh_fail_count = 0
                    schedule_queue_ui_update(data, source="stream")
            except Exception as e:
                queue_stream_fail_count += 1
                if queue_stream_fail_count <= 3 or queue_stream_fail_count % 10 == 0:
                    try:
                        sys.__stdout__.write(f"[QUEUE STREAM reconnect x{queue_stream_fail_count}] {e}\n")
                    except Exception:
                        pass
                try:
                    root.after(0, lambda: jobs_live_var.set(tr(
                        f"Live Queue stream reconnecting... failures={queue_stream_fail_count}",
                        f"Cola en vivo reconectando... fallos={queue_stream_fail_count}"
                    )))
                except Exception:
                    pass
                time.sleep(min(AUTO_BACKOFF_MAX_SECONDS, 1 + queue_stream_fail_count))
    finally:
        with queue_stream_start_lock:
            queue_stream_running = False


def start_queue_stream_worker_once():
    """Start exactly one SSE worker; the flag is set before Thread.start()."""
    global queue_stream_running
    with queue_stream_start_lock:
        if queue_stream_running:
            return False
        queue_stream_running = True
    threading.Thread(target=queue_stream_worker, daemon=True, name="queue_sse_stream").start()
    return True


def selected_queue_job_status():
    sel = queue_tree.selection()
    if not sel:
        return ""
    vals = queue_tree.item(sel[0], "values")
    return vals[2] if len(vals) > 2 else ""


def update_cancel_button_state(event=None):
    """Enable one cancel button for either one selected job or multiple selected jobs."""
    global remembered_queue_job_id
    try:
        selected_ids = selected_queue_job_ids()
        job_id = selected_queue_job_id()
        if job_id:
            remembered_queue_job_id = job_id
            queue_focus_var.set(tr(f"Selected job: {job_id}", f"Job seleccionado: {job_id}"))
        elif remembered_queue_job_id:
            ui_var_set_if_changed(queue_focus_var, tr(f"Selected job: {remembered_queue_job_id}", f"Job seleccionado: {remembered_queue_job_id}"))
        else:
            ui_var_set_if_changed(queue_focus_var, tr("Selected job: none", "Job seleccionado: ninguno"))

        selected_cancelable = selected_cancellable_job_ids(use_remembered=False)
        active_id = job_id or remembered_queue_job_id
        single_cancelable = bool(active_id and queue_job_is_cancelable(active_id))

        can_cancel = bool(selected_cancelable or single_cancelable)
        btn_cancel_job.configure(state=("normal" if can_cancel else "disabled"))

        if len(selected_cancelable) >= 2:
            queue_cancel_hint_var.set(tr(
                f"Ready to cancel {len(selected_cancelable)} selected jobs.",
                f"Listo para cancelar {len(selected_cancelable)} jobs seleccionados."
            ))
        elif single_cancelable:
            queue_cancel_hint_var.set(tr(
                f"Ready to cancel your active job: {active_id}",
                f"Listo para cancelar tu job activo: {active_id}"
            ))
        elif active_id:
            status = queue_job_status_raw(active_id)
            if status in ("completed", "cancelled", "failed"):
                queue_cancel_hint_var.set(tr(
                    f"Cancel disabled: job {active_id} is already {status}.",
                    f"Cancelar deshabilitado: el job {active_id} ya está {localize_status_text(status)}."
                ))
            elif active_id not in created_job_tokens:
                queue_cancel_hint_var.set(tr(
                    "Cancel disabled: this job does not match this GUI/user.",
                    "Cancelar deshabilitado: este job no coincide con esta GUI/usuario."
                ))
            elif active_id in pending_cancel_job_ids:
                queue_cancel_hint_var.set(tr(
                    f"Cancel already pending for job {active_id}.",
                    f"Cancelación ya pendiente para job {active_id}."
                ))
            else:
                queue_cancel_hint_var.set(tr(
                    "Cancel disabled for this job state.",
                    "Cancelar deshabilitado para este estado del job."
                ))
        else:
            queue_cancel_hint_var.set(tr(
                "Select one or more of your own queued/testing jobs to enable cancel.",
                "Selecciona uno o más de tus jobs en cola/probando para habilitar cancelar."
            ))
    except Exception:
        pass


def cancel_jobs_by_ids(job_ids):
    """Submit one batch cancel request in the background and update the GUI without freezing."""
    job_ids = [jid for jid in job_ids if queue_job_is_cancelable(jid)]
    if not job_ids:
        print(tr("[FAIL] No active creator-owned queued/testing jobs selected to cancel.", "[FAIL] No hay jobs activos propios en cola/probando seleccionados para cancelar."))
        update_cancel_button_state()
        return

    for jid in job_ids:
        pending_cancel_job_ids.add(jid)
    update_cancel_button_state()

    payload = {
        "explicit_cancel": True,
        "student": local_gui_user(),
        "client_hostname": local_gui_user(),
        "student_ip": get_local_ip(),
        "client_token": GUI_CLIENT_TOKEN,
        "jobs": [
            {"job_id": jid, "cancel_token": created_job_tokens.get(jid, ""), "client_token": GUI_CLIENT_TOKEN}
            for jid in job_ids
        ],
    }

    def task():
        try:
            data = api_post_json("/queue/cancel_batch", payload, timeout=60)
            results = data.get("results", [])
            ok_count = 0
            fail_count = 0
            for r in results:
                jid = r.get("job_id", "")
                if r.get("success"):
                    ok_count += 1
                    created_job_tokens.pop(jid, None)
                else:
                    fail_count += 1
                    print(f"[FAIL] Cancel {jid}: {r.get('error') or r.get('reason') or r.get('message', '')}")
                pending_cancel_job_ids.discard(jid)

            save_queue_tokens(created_job_tokens)
            root.after(0, lambda: selected_ai_var.set(tr(
                f"Cancel complete: {ok_count} cancelled, {fail_count} failed",
                f"Cancelación completa: {ok_count} cancelados, {fail_count} fallaron"
            )))
            print(tr(
                f"[OK] Cancel complete: {ok_count} cancelled, {fail_count} failed",
                f"[OK] Cancelación completa: {ok_count} cancelados, {fail_count} fallaron"
            ))
            refresh_queue(silent=True)
            refresh_boards(silent=True)
        except Exception as e:
            for jid in job_ids:
                pending_cancel_job_ids.discard(jid)
            print(f"[FAIL] Batch cancel failed: {e}")
        finally:
            root.after(0, update_cancel_button_state)

    run_thread(task, tr(f"Cancelling {len(job_ids)} job(s)", f"Cancelando {len(job_ids)} job(s)"))


def cancel_selected_queue_job():
    """
    One cancel button:
    - If multiple selected jobs are cancellable, cancel them together using batch endpoint.
    - If one job is selected/remembered and cancellable, cancel that one.
    """
    selected_ids = selected_cancellable_job_ids(use_remembered=False)
    if selected_ids:
        cancel_jobs_by_ids(selected_ids)
        return

    job_id = selected_queue_job_id() or remembered_queue_job_id
    if not job_id:
        print(tr("[FAIL] Select your active job in the queue, then click Cancel Job(s).", "[FAIL] Selecciona tu job activo en la cola y luego haz clic en Cancelar job(s)."))
        return
    if not queue_job_is_cancelable(job_id):
        status = queue_job_status_raw(job_id)
        print(tr(
            f"[FAIL] Cancel disabled for {job_id}. Status={status or 'unknown'}",
            f"[FAIL] Cancelar deshabilitado para {job_id}. Estado={localize_status_text(status or 'unknown')}"
        ))
        update_cancel_button_state()
        return
    cancel_jobs_by_ids([job_id])




def queue_ai(role_override=None, source_mode="raspberry_pi_tab"):
    requested = board_request_value()
    v_path = verilog_path_var.get().strip()
    q_path = qsf_path_var.get().strip()
    s_path = sof_path_var.get().strip()

    if not v_path:
        print("[FAIL] Selecciona un .v/.sv local o escribe un path del servidor.")
        return
    if not s_path:
        print("[FAIL] Selecciona un .sof local o escribe un path del servidor.")
        return

    action_name = "Classic Mode Program FPGA" if str(source_mode).lower() == "classic_mode" else "Queue Program"
    if not ensure_pi_connected_or_prompt(action_name):
        return

    v_local = is_local_file(v_path)
    q_local = is_local_file(q_path) if q_path else False
    s_local = is_local_file(s_path)
    signature_fields = build_submission_signature_fields(v_path, s_path, q_path)
    qsf_text = ""
    if q_path and q_local:
        try:
            with open(q_path, "r", encoding="utf-8", errors="ignore") as f:
                qsf_text = f.read()
        except Exception as e:
            print(f"[WARN] Could not read QSF for AI accuracy: {e}")
    elif q_path:
        # Server-side QSF path; the Pi will try to read it if the path is allowed.
        qsf_text = ""
    submission_signature = signature_fields.get("submission_signature", "")
    global LAST_ACCEPTED_SUBMISSION_SIGNATURE
    # v4.32: do not block solely because this GUI accepted the same .v/.sof before.
    # A cancelled/completed/failed job must allow the same files to be submitted again.
    # The backend still blocks true active duplicates, and in-flight protection still
    # catches accidental double-clicks while the request is being created.
    if submission_signature and not try_mark_submission_in_flight(submission_signature):
        print("[BLOCKED] Duplicate submit ignored: this same unchanged .v/.sof pair is already being queued. Change/rebuild the .v or .sof before adding another job.")
        root.after(0, lambda: selected_ai_var.set("Duplicate blocked: same .v/.sof already queued"))
        refresh_queue(silent=True)
        return
    priority_role = priority_role_label(role_override or queue_priority_var.get() or "Student")
    if role_override in ("Teacher", "Background"):
        print(f"[TERMINAL QUEUE] Submitting job as {role_override}.")
    priority_value = priority_role_value(priority_role)
    test_minutes_value = get_test_minutes_value()

    if v_local and s_local:
        queue_mode = "upload"
    elif v_local and not s_local:
        queue_mode = "local_verilog_server_sof"
    elif (not v_local) and s_local:
        queue_mode = "server_verilog_local_sof"
    else:
        queue_mode = "server_paths"

    # Local row appears immediately even before the first API request returns.
    selected_major = current_pi_major()
    pending_id = add_local_pending_queue_job(queue_mode, requested, priority_role, test_minutes_value, selected_major)

    common_fields = {
        "requested_board": requested,
        "client_hostname": local_gui_user(),
        "student_ip": get_local_ip(),
        "priority": str(priority_value),
        "priority_label": priority_role,
        "priority_role": priority_role,
        "student": local_gui_user(),
        "major": selected_major,
        "source_mode": source_mode,
        "submit_mode": source_mode,
        "test_minutes": str(test_minutes_value),
    }
    if q_path:
        common_fields["qsf_path"] = q_path
        common_fields["qsf_filename"] = os.path.basename(q_path)
    if qsf_text:
        common_fields["qsf_text"] = qsf_text
    common_fields.update({k: str(v) for k, v in signature_fields.items()})
    common_json = {
        "requested_board": requested or None,
        "client_hostname": local_gui_user(),
        "student_ip": get_local_ip(),
        "priority": priority_value,
        "priority_label": priority_role,
        "priority_role": priority_role,
        "student": local_gui_user(),
        "major": selected_major,
        "source_mode": source_mode,
        "submit_mode": source_mode,
        "test_minutes": test_minutes_value,
        "client_token": GUI_CLIENT_TOKEN,
    }
    if q_path:
        common_json["qsf_path"] = q_path
        common_json["qsf_filename"] = os.path.basename(q_path)
    if qsf_text:
        common_json["qsf_text"] = qsf_text
    common_json.update(signature_fields)

    def remember_job_token(data):
        job_id = data.get("job_id") or data.get("job", {}).get("job_id", "")
        cancel_token = data.get("cancel_token", "")
        if job_id and cancel_token:
            created_job_tokens[job_id] = cancel_token
            save_queue_tokens(created_job_tokens)
        return job_id

    def finish(data, label, temp_id=pending_id):
        global LAST_ACCEPTED_SUBMISSION_SIGNATURE
        remove_local_pending_queue_job(temp_id)
        job_id = remember_job_token(data)
        # v4.32: do not remember this signature as permanently blocked.
        # Cancelled/completed jobs are allowed to be resubmitted with the same files.
        if data.get("duplicate_existing"):
            msg = data.get("message") or f"Duplicate blocked; existing active job: {job_id}"
            print(f"[BLOCKED] {msg}")
            if job_id:
                root.after(0, lambda: selected_ai_var.set(f"Duplicate blocked: active job {job_id}"))
        elif job_id:
            root.after(0, lambda: selected_ai_var.set(f"Queued: {job_id}"))
        print_queue_submit_summary(data, label)
        refresh_queue(silent=False)

    def fail_prequeued_upload(job_id, error_text):
        if not job_id:
            return
        try:
            api_post_json(f"/queue/{job_id}/upload_failed", {
                "reason": str(error_text),
                "student": local_gui_user(),
                "major": current_pi_major(),
                "client_hostname": local_gui_user(),
                "student_ip": get_local_ip(),
            }, timeout=10)
        except Exception:
            pass

    def mark_pi_offline_submit_failed(error_text, temp_id=pending_id):
        """Clear the optimistic local-* row when the Pi API is down.

        v4.33: local-* is only a GUI placeholder.  If /queue/prequeue_upload never
        reaches the Raspberry Pi, no real server job exists, so the GUI must not
        keep saying "Sending job to queue: local-...".
        """
        remove_local_pending_queue_job(temp_id)
        err_l = str(error_text).lower()
        is_connection_error = ("connection refused" in err_l or "winerror 10061" in err_l or "actively refused" in err_l or "failed to establish a new connection" in err_l)
        is_server_error = ("500 server error" in err_l or "internal server error" in err_l or "traceback" in err_l)
        if is_connection_error:
            msg = tr(
                "Pi URL refused the connection; job was NOT queued. The GUI is probably using the wrong NetBird IP, or RUN_PI_CONTROLLER.sh is not running on the Raspberry Pi.",
                "La URL del Pi rechazó la conexión; el job NO fue encolado. Probablemente la GUI usa la IP NetBird incorrecta, o RUN_PI_CONTROLLER.sh no está corriendo en el Raspberry Pi."
            )
            pi_state = "Disconnected"
            tag = "OFFLINE"
        elif is_server_error:
            msg = tr(
                "Pi API is connected, but the upload endpoint returned a server error. The job was not queued for programming. Check the Raspberry Pi controller terminal for the Python traceback.",
                "La API del Pi está conectada, pero el endpoint de subida devolvió un error del servidor. El job no fue encolado para programar. Revisa el traceback en la terminal del controlador del Raspberry Pi."
            )
            pi_state = "Connected - server error"
            tag = "SERVER ERROR"
        else:
            msg = tr(
                "Pi request failed; job was NOT queued. Check the Raspberry Pi controller terminal and network connection.",
                "La petición al Pi falló; el job NO fue encolado. Revisa la terminal del controlador del Raspberry Pi y la conexión de red."
            )
            pi_state = "Check Pi"
            tag = "PI REQUEST FAILED"
        try:
            root.after(0, lambda: selected_ai_var.set(msg))
            root.after(0, lambda: queue_status_var.set(msg))
            root.after(0, lambda: pi_status_var.set(pi_state))
        except Exception:
            pass
        print(f"[{tag}] {msg}")
        print(f"[{tag} DETAIL] {error_text}")

    def prequeue_then_upload(files, fields, prequeue_payload, label):
        """
        Two-phase queue submit:
        1. Register job immediately in server queue.
        2. Upload large files into that existing job.
        """
        job_id = ""
        try:
            pre = api_post_json("/queue/prequeue_upload", prequeue_payload, timeout=15)
            if not isinstance(pre, dict):
                raise RuntimeError("Pi returned an invalid prequeue response; upload was not started.")

            # v4.42: never continue to /queue/<job_id>/upload_files unless the
            # prequeue response is successful and contains a real job_id.  Previous
            # builds could receive a fair-share/block/reject response without a
            # job_id, then build the bad URL /queue//upload_files and show a false
            # "Pi offline" error.
            job_id = remember_job_token(pre)
            remove_local_pending_queue_job(pending_id)
            print_queue_submit_summary(pre, "[RESULT] Job registrado inmediatamente en la cola:")

            if not pre.get("success", False):
                existing = pre.get("existing_job_id") or (pre.get("active_job_ids") or [""])[0] or job_id
                msg = pre.get("error") or pre.get("message") or "Pi rejected the queue request before upload started."
                print(f"[BLOCKED] Queue request rejected before file upload: {msg}")
                if existing:
                    print(f"[BLOCKED] Existing active job: {existing}")
                root.after(0, lambda m=msg, e=existing: selected_ai_var.set((m + (f" Active job: {e}" if e else ""))[:240]))
                refresh_queue(silent=False)
                return

            if not job_id:
                msg = "Pi did not return a job_id from /queue/prequeue_upload; upload was stopped to avoid /queue//upload_files."
                print(f"[FAIL] {msg}")
                root.after(0, lambda: selected_ai_var.set(msg))
                refresh_queue(silent=False)
                return

            if job_id:
                root.after(0, lambda: selected_ai_var.set(f"Receiving upload: {job_id}"))
            if pre.get("duplicate_existing"):
                print("[BLOCKED] Existing active job found for this unchanged .v/.sof pair; upload skipped.")
                root.after(0, lambda: selected_ai_var.set(f"Duplicate blocked: active job {job_id}"))
                refresh_queue(silent=False)
                return
            # v3.95: do not force a queue refresh between prequeue and upload_files.
            # The stream/live queue will show the receiving row; avoiding this extra
            # /queue call removes a race with upload recovery/reconcile.

            try:
                data = api_post_files(f"/queue/{job_id}/upload_files", fields, files, timeout=900)
            except Exception as upload_error:
                print(f"[WARN] upload_files first attempt failed for {job_id}: {upload_error}")
                try:
                    detail = api_get(f"/queue/{job_id}", timeout=10)
                    print("[DEBUG] Server job detail after upload_files failure:")
                    print(json.dumps(detail, indent=2, ensure_ascii=False))
                    # v4.41: If the Pi already accepted the upload and promoted the
                    # job to queued/running/testing/completed, do not retry the same
                    # multipart upload and do not mark the Pi offline. This was a
                    # race between instant dispatch and the GUI retry path.
                    djob = detail.get("job", {}) if isinstance(detail, dict) else {}
                    dst = str(djob.get("status", "")).lower()
                    already_accepted = bool(djob.get("upload_files_attached")) or bool(djob.get("stage_sof_active_path")) or bool(djob.get("sof_local_path"))
                    if dst in ("queued", "running", "testing", "completed") and already_accepted:
                        data = {
                            "success": True,
                            "job_id": job_id,
                            "status": djob.get("status", dst),
                            "idempotent_upload": True,
                            "upload_already_accepted": True,
                            "message": "Upload already accepted by Pi; job is already " + str(djob.get("status", dst)),
                            "job": djob,
                        }
                        finish(data, label, temp_id="")
                        return
                except Exception as detail_error:
                    print(f"[DEBUG] Could not read server job detail: {detail_error}")
                time.sleep(0.5)
                data = api_post_files(f"/queue/{job_id}/upload_files", fields, files, timeout=900)
            finish(data, label, temp_id="")
        except Exception as e:
            mark_pi_offline_submit_failed(e, pending_id)
            fail_prequeued_upload(job_id, e)
            refresh_queue(silent=True)
            raise

    def submit_with_pending(submit_fn, label):
        try:
            data = submit_fn()
            finish(data, label)
        except Exception as e:
            mark_pi_offline_submit_failed(e, pending_id)
            raise

    # Large local SOF uploads now use prequeue, so the job appears in the real server
    # queue immediately instead of after the multipart upload completes.
    if v_local and s_local:
        prequeue_payload = dict(common_json)
        prequeue_payload.update({
            "kind": "upload",
            "filename": os.path.basename(v_path),
            "verilog_filename": os.path.basename(v_path),
            "sof_filename": os.path.basename(s_path),
            "qsf_filename": os.path.basename(q_path) if q_path else "",
        })
        def task():
            try:
                upload_files = {"verilog_file": v_path, "sof_file": s_path}
                if q_path and q_local:
                    upload_files["qsf_file"] = q_path
                prequeue_then_upload(
                    upload_files,
                    common_fields,
                    prequeue_payload,
                    "[RESULT] Job encolado usando .qpf/.v/.qsf/.sof locales:" if qpf_path_var.get().strip() else "[RESULT] Job encolado usando .v/.qsf/.sof locales:"
                )
            finally:
                clear_submission_in_flight(submission_signature)
        run_thread(task, "Registrando cola y subiendo .v/.sof locales")
        return

    if v_local and not s_local:
        with open(v_path, "r", encoding="utf-8", errors="ignore") as f:
            verilog_code = f.read()
        payload = dict(common_json)
        payload.update({"filename": os.path.basename(v_path), "verilog_code": verilog_code, "sof_path": s_path})
        if qsf_text:
            payload["qsf_text"] = qsf_text
        elif q_path:
            payload["qsf_path"] = q_path
        def task():
            try:
                submit_with_pending(
                    lambda: api_post_json("/queue/deploy", payload, timeout=120),
                    "[RESULT] Job encolado usando Verilog local + SOF en servidor:"
                )
            finally:
                clear_submission_in_flight(submission_signature)
        run_thread(task, "Encolando job con Verilog local + SOF en servidor")
        return

    if (not v_local) and s_local:
        fields = dict(common_fields)
        fields["verilog_path"] = v_path
        prequeue_payload = dict(common_json)
        prequeue_payload.update({
            "kind": "server_verilog_local_sof",
            "filename": os.path.basename(v_path) if os.path.basename(v_path) else "server_design.v",
            "verilog_path": v_path,
            "sof_filename": os.path.basename(s_path),
            "qsf_filename": os.path.basename(q_path) if q_path else "",
        })
        def task():
            try:
                upload_files = {"sof_file": s_path}
                if q_path and q_local:
                    upload_files["qsf_file"] = q_path
                prequeue_then_upload(
                    upload_files,
                    fields,
                    prequeue_payload,
                    "[RESULT] Job encolado usando Verilog/QSF en servidor + SOF local:"
                )
            finally:
                clear_submission_in_flight(submission_signature)
        run_thread(task, "Registrando cola y subiendo SOF local")
        return

    payload = dict(common_json)
    payload.update({"verilog_path": v_path, "sof_path": s_path})
    if q_path:
        payload["qsf_path"] = q_path
    if qsf_text:
        payload["qsf_text"] = qsf_text
    def task():
        try:
            submit_with_pending(
                lambda: api_post_json("/queue/deploy", payload, timeout=120),
                "[RESULT] Job encolado usando paths del servidor:"
            )
        finally:
            clear_submission_in_flight(submission_signature)
    run_thread(task, "Encolando job con paths del servidor")

def list_server_projects():
    def task():
        display_value = requested_board_var.get()
        request_value = board_request_value()
        family = "pro" if ("agilex" in display_value.lower() or "de10" in display_value.lower() or "agilex" in str(request_value).lower() or "de10" in str(request_value).lower()) else "standard"
        data = api_get(f"/server/projects?family={family}&limit=50", timeout=90)
        print(f"[OK] Proyectos encontrados en servidor ({family}):")
        print(json.dumps(data, indent=2, ensure_ascii=False))
        projects = data.get("projects", [])
        if projects:
            # Auto-fill the first project as a convenience; user can overwrite.
            first = projects[0]
            root.after(0, lambda: (
                verilog_path_var.set(first.get("verilog_path", "")),
                sof_path_var.set(first.get("sof_path", ""))
            ))
            print("[INFO] Se llenó el primer proyecto encontrado. Puedes cambiar los paths o usar Examinar para archivos locales.")
    run_thread(task, "Listando proyectos .v/.sof existentes en el servidor")


def is_local_file(path):
    return bool(path) and os.path.exists(path)



def local_file_identity(path):
    """Fast file identity that changes when the selected file is edited/replaced."""
    try:
        p = os.path.abspath(path)
        st = os.stat(p)
        return {
            "path": p,
            "name": os.path.basename(p),
            "size": int(st.st_size),
            "mtime_ns": int(getattr(st, "st_mtime_ns", int(st.st_mtime * 1_000_000_000))),
        }
    except Exception:
        return {"path": str(path or ""), "name": os.path.basename(str(path or "")), "size": "", "mtime_ns": ""}


def compute_submission_signature(v_path, s_path, qsf_path=""):
    """Duplicate-guard key: same selected files = same key; edit/replace any file = new key."""
    v_local = is_local_file(v_path)
    s_local = is_local_file(s_path)
    q_local = is_local_file(qsf_path) if qsf_path else False
    payload = {
        "verilog": local_file_identity(v_path) if v_local else {"server_path": str(v_path or "")},
        "sof": local_file_identity(s_path) if s_local else {"server_path": str(s_path or "")},
        "qsf": local_file_identity(qsf_path) if q_local else {"server_path": str(qsf_path or "")},
    }
    raw = json.dumps(payload, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(raw.encode("utf-8", errors="ignore")).hexdigest(), payload


def build_submission_signature_fields(v_path, s_path, qsf_path=""):
    sig, payload = compute_submission_signature(v_path, s_path, qsf_path)
    out = {"submission_signature": sig, "file_pair_signature": sig}
    v = payload.get("verilog", {}) or {}
    s = payload.get("sof", {}) or {}
    q = payload.get("qsf", {}) or {}
    out.update({
        "verilog_client_path": v.get("path") or v.get("server_path") or str(v_path or ""),
        "sof_client_path": s.get("path") or s.get("server_path") or str(s_path or ""),
        "qsf_client_path": q.get("path") or q.get("server_path") or str(qsf_path or ""),
        "verilog_client_size": str(v.get("size", "")),
        "sof_client_size": str(s.get("size", "")),
        "qsf_client_size": str(q.get("size", "")),
        "verilog_client_mtime_ns": str(v.get("mtime_ns", "")),
        "sof_client_mtime_ns": str(s.get("mtime_ns", "")),
        "qsf_client_mtime_ns": str(q.get("mtime_ns", "")),
    })
    out["verilog_file_signature"] = hashlib.sha256(json.dumps(v, sort_keys=True, ensure_ascii=False).encode("utf-8", errors="ignore")).hexdigest()
    out["sof_file_signature"] = hashlib.sha256(json.dumps(s, sort_keys=True, ensure_ascii=False).encode("utf-8", errors="ignore")).hexdigest()
    out["qsf_file_signature"] = hashlib.sha256(json.dumps(q, sort_keys=True, ensure_ascii=False).encode("utf-8", errors="ignore")).hexdigest()
    return out


def try_mark_submission_in_flight(signature):
    with IN_FLIGHT_SUBMISSION_LOCK:
        if signature in IN_FLIGHT_SUBMISSION_SIGNATURES:
            return False
        IN_FLIGHT_SUBMISSION_SIGNATURES.add(signature)
        return True


def clear_submission_in_flight(signature):
    with IN_FLIGHT_SUBMISSION_LOCK:
        IN_FLIGHT_SUBMISSION_SIGNATURES.discard(signature)


def get_test_minutes_value():
    raw = (test_minutes_var.get() or "5").strip()
    try:
        minutes = int(float(raw))
    except Exception:
        minutes = 5
    if minutes < 0:
        minutes = 0
    if minutes > 60:
        minutes = 60
    # Normalize the UI value so users see the clamped value.
    try:
        test_minutes_var.set(str(minutes))
    except Exception:
        pass
    return minutes


def analyze_verilog():
    requested = board_request_value()
    path = verilog_path_var.get().strip()
    qsf_path = qsf_path_var.get().strip()
    if not path:
        print("[FAIL] Selecciona un .v/.sv local o escribe un path del servidor.")
        return

    payload = {
        "requested_board": requested or None,
        "force_refresh": True,
    }
    if is_local_file(path):
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            payload["verilog_code"] = f.read()
        payload["filename"] = os.path.basename(path)
        label = "Analizando Verilog/QSF local con Raspberry Pi AI" if qsf_path else "Analizando Verilog local con Raspberry Pi AI"
    else:
        payload["verilog_path"] = path
        label = "Analizando Verilog/QSF desde path del servidor" if qsf_path else "Analizando Verilog desde path del servidor"

    if qsf_path:
        if is_local_file(qsf_path):
            try:
                with open(qsf_path, "r", encoding="utf-8", errors="ignore") as f:
                    payload["qsf_text"] = f.read()
                payload["qsf_filename"] = os.path.basename(qsf_path)
            except Exception as e:
                print(f"[WARN] Could not read QSF: {e}")
        else:
            payload["qsf_path"] = qsf_path

    def task():
        data = api_post_json("/ai/select_board", payload, timeout=90)
        board = data.get("selected_board") or "None"
        confidence = data.get("confidence") or "unknown"
        root.after(0, lambda: selected_ai_var.set(f"AI: {board} ({confidence})"))
        print("[OK] Selección AI del Raspberry Pi:")
        print(json.dumps(data, indent=2, ensure_ascii=False))
        refresh_boards(silent=True)

    run_thread(task, label)



def deploy_ai():
    """
    Direct programming was removed in v3.67.
    Keep this compatibility wrapper so any old shortcut routes to the queue.
    """
    print("[INFO] Direct programming was removed. Using Queue Program instead so all jobs follow fairness/timer rules.")
    queue_ai()


def get_local_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


# Pi buttons - queue-only flow.
# JTAG refresh lives in the JTAG Real-Time page.
# Job cancel lives in the Jobs Queue page.
btn_queue = register_text(ttk.Button(pi_button_frame, text="", command=lambda: queue_ai()), "Queue Program", "Encolar programa")
btn_queue.grid(row=0, column=0, padx=3)

def al_cerrar():
    try:
        if _api_session is not None:
            _api_session.close()
    except Exception:
        pass
    try:
        GUI_BG_EXECUTOR.shutdown(wait=False, cancel_futures=True)
    except Exception:
        pass
    sys.stdout = sys.__stdout__
    sys.stderr = sys.__stderr__
    root.destroy()


auto_refresh_enabled = False

def startup_auto_connect():
    """Automatically test Pi and start live queue/JTAG updates when the GUI opens."""
    def task():
        global auto_refresh_enabled
        try:
            data = api_get("/status", timeout=5)
            auto_refresh_enabled = True
            def ok_ui():
                global live_queue_enabled, live_jtag_enabled
                live_queue_enabled = True
                live_jtag_enabled = True
                pi_status_var.set("Conectado")
                selected_ai_var.set("Pi connected; live JTAG + queue active")
                jobs_live_var.set(tr(f"Live Queue: ON, every {LIVE_QUEUE_SECONDS}s", f"Cola en vivo: ACTIVA cada {LIVE_QUEUE_SECONDS}s"))
                jtag_live_var.set(tr(f"Live JTAG: ON, every {LIVE_JTAG_SECONDS}s", f"JTAG en vivo: ACTIVO cada {LIVE_JTAG_SECONDS}s"))
            root.after(0, ok_ui)
            # Do not print full /status JSON to terminal.
            refresh_queue(silent=True)
            refresh_boards(silent=True)
        except Exception as e:
            auto_refresh_enabled = False
            root.after(0, lambda: (pi_status_var.set("No conectado"), jobs_live_var.set(tr("Live Queue: OFF", "Cola en vivo: APAGADA")), jtag_live_var.set(tr("Live JTAG: OFF", "JTAG en vivo: APAGADO"))))
            print(f"[WARN] Auto Test Pi falló. Revisa Pi API URL o usa Test Pi manualmente: {e}")
    submit_background(task)


def live_queue_loop():
    global live_queue_polling
    if auto_refresh_enabled and live_queue_enabled:
        if queue_stream_enabled:
            # One persistent realtime stream replaces repeated /queue polling.
            start_queue_stream_worker_once()
        elif not live_queue_polling:
            live_queue_polling = True
            def poll_queue():
                global live_queue_polling
                try:
                    refresh_queue(silent=True)
                finally:
                    live_queue_polling = False
            submit_background(poll_queue)

    # The stream worker owns the update rate; this loop only supervises it.
    fail = int(queue_refresh_fail_count or queue_stream_fail_count or 0)
    delay = 0.25 if queue_stream_enabled else (LIVE_QUEUE_SECONDS if fail < 3 else min(AUTO_BACKOFF_MAX_SECONDS, 3 + fail // 3))
    root.after(int(delay * 1000), live_queue_loop)


def live_jtag_loop():
    global live_jtag_polling
    if auto_refresh_enabled and live_jtag_enabled and not live_jtag_polling:
        live_jtag_polling = True
        def poll_jtag():
            global live_jtag_polling
            try:
                refresh_boards(silent=True)
            finally:
                live_jtag_polling = False
        submit_background(poll_jtag)

    fail = int(jtag_refresh_fail_count or 0)
    delay = LIVE_JTAG_SECONDS if fail < 3 else min(AUTO_BACKOFF_MAX_SECONDS, 3 + fail // 3)
    root.after(int(delay * 1000), live_jtag_loop)


print("[OK] GUI v4.44 lista: low-latency queue sync active; JTAG prewarm daemon keeps cables ready.")
print("[INFO] AI - Select encola en un click. Cancel Selected Job solo funciona para jobs creados por esta GUI.")
print("[INFO] Live Queue event-driven low-latency stream (~50 ms server wake) y Live JTAG cache cada 8s.")
print("[INFO] Flujo: GUI → Raspberry Pi AI/HAT → Quartus server → JTAG existente.")
apply_language()
show_sidebar_page("pi")
root.after(800, startup_auto_connect)
live_queue_loop()
live_jtag_loop()
root.protocol("WM_DELETE_WINDOW", al_cerrar)
root.mainloop()
