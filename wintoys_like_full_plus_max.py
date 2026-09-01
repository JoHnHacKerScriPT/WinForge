# wintoys_like_full_plus.py  (app: "WinForge")
# Copy & paste THIS WHOLE FILE (it replaces the previous one), then run:
#   pip install pyside6 psutil
#   python wintoys_like_full_plus.py
#
# Keep app_icon.ico / app_icon.png in the SAME folder as this script.
#
# Features:
#   - Async runner (so SFC/DISM/etc won't freeze UI)
#   - Services manager (start/stop + refresh)  [Admin required for most]
#   - Startup entries viewer + Enable/Disable (Task-Manager-style, reversible)
#   - Power plans (list/set active)            [may require admin depending on policy]
#   - Windows Update control: Automatic / Security-only / Fully disabled [Admin]
#   - Windows Update reset helpers (safe wrappers) [Admin]
#   - Privacy & Telemetry center (toggles + service control) [Admin for system-wide]
#   - Apps: install (.exe/.msi or winget) and uninstall installed software
#   - USB Creator: write any .iso to a USB drive to make it bootable [Admin]
#   - Polished, modern dark UI theme with the app's own logo
#
# Security notes:
# - No command is ever run through a shell string (no shell=True anywhere),
#     so user-provided paths/text can't inject extra shell commands.
#   - Destructive actions (uninstall, USB format/write, disabling Windows
#     Update) always ask for explicit confirmation first.
#   - Admin-only actions check privileges before running and offer to
#     relaunch elevated rather than silently failing.
#
# Windows-focused (10/11). On non-Windows, many features are disabled.

import os
import sys
import time
import shutil
import platform
import subprocess
import tempfile
from dataclasses import dataclass
from typing import List, Tuple, Optional, Dict

import psutil

from PySide6.QtCore import Qt, QTimer, QObject, Signal, QThread, QSize
from PySide6.QtGui import QColor, QFont, QIcon, QPixmap
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QHBoxLayout, QVBoxLayout,
    QListWidget, QListWidgetItem, QLabel, QPushButton, QMessageBox,
    QCheckBox, QTextEdit, QLineEdit, QTableWidget, QTableWidgetItem,
    QHeaderView, QGroupBox, QGridLayout, QComboBox, QScrollArea,
    QFrame, QGraphicsDropShadowEffect, QSizePolicy, QProgressBar,
    QFileDialog, QRadioButton, QButtonGroup, QDialog,
    QAbstractItemView, QInputDialog
)


def hex_to_rgb(h: str) -> Tuple[int, int, int]:
    h = h.lstrip("#")
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))  # type: ignore


def get_windows_accent_color() -> Optional[str]:
    """Reads the user's current Windows accent color as a #RRGGBB string.
    Returns None on any failure (non-Windows, missing key, etc.) — this is
    a nice-to-have visual touch, so we fail quietly and keep the default
    WinForge purple instead of erroring out."""
    if not IS_WINDOWS:
        return None
    try:
        val = reg_get_dword(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\DWM", "AccentColor", 0)
        if not val:
            return None
        # AccentColor is stored as 0xAABBGGRR
        r = val & 0xFF
        g = (val >> 8) & 0xFF
        b = (val >> 16) & 0xFF
        return f"#{r:02X}{g:02X}{b:02X}"
    except Exception:
        return None


def apply_shadow(widget: QWidget, blur: int = 26, dy: int = 8, alpha: int = 130, color: str = "#7C5CFC"):
    """Attach a soft drop shadow to give a widget visual depth ('glow' when color is an accent)."""
    r, g, b = hex_to_rgb(color)
    eff = QGraphicsDropShadowEffect(widget)
    eff.setBlurRadius(blur)
    eff.setOffset(0, dy)
    eff.setColor(QColor(r, g, b, alpha))
    widget.setGraphicsEffect(eff)

IS_WINDOWS = (os.name == "nt")
if IS_WINDOWS:
    import ctypes
    import winreg

# Where to look for app_icon.ico / app_icon.png:
#   - running as a normal .py script: next to this file
#   - running as a PyInstaller --onefile .exe: PyInstaller extracts bundled
#     data (via --add-data) into a temp folder pointed to by sys._MEIPASS,
#     while sys.executable is the real .exe's own folder — so we check both,
#     and prefer whichever actually has the file (handles a user copying
#     just the .exe around without app_icon.ico next to it).
if getattr(sys, "frozen", False):
    _CANDIDATE_DIRS = [
        os.path.dirname(os.path.abspath(sys.executable)),
        getattr(sys, "_MEIPASS", ""),
    ]
else:
    _CANDIDATE_DIRS = [os.path.dirname(os.path.abspath(__file__))]

APP_DIR = _CANDIDATE_DIRS[0]
APP_ICON_PATH = ""
for _dir in _CANDIDATE_DIRS:
    if not _dir:
        continue
    for _name in ("app_icon.ico", "app_icon.png"):
        _candidate = os.path.join(_dir, _name)
        if os.path.isfile(_candidate):
            APP_ICON_PATH = _candidate
            break
    if APP_ICON_PATH:
        break
if not APP_ICON_PATH:
    APP_ICON_PATH = os.path.join(APP_DIR, "app_icon.ico")  # fallback path (may not exist)


def find_resource(*relative_parts) -> str:
    """Find a bundled resource (e.g. a sidebar icon PNG) in whichever
    candidate directory actually has it — same search strategy as
    APP_ICON_PATH, but reusable for any file we ship alongside the app."""
    for _dir in _CANDIDATE_DIRS:
        if not _dir:
            continue
        candidate = os.path.join(_dir, *relative_parts)
        if os.path.isfile(candidate):
            return candidate
    return ""


def load_json_override(filename: str, default):
    """Loads an optional external JSON config file (next to the app) to
    override a default Python value, without needing to touch the source —
    the same lightweight 'modular config' idea used for custom privacy
    toggles, applied to a few more static lists (DNS presets, runtimes,
    known startup programs) so they're editable by anyone, safely, with the
    hardcoded default as a guaranteed fallback on any parse error."""
    path = find_resource(filename)
    if not path:
        return default
    try:
        import json
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


# Separate high-res PNG path, used anywhere we render the logo as a QPixmap
# (e.g. the sidebar badge). QPixmap(app_icon.ico) would pick just ONE small
# embedded frame from the multi-size ICO (often 16-32px) and get blurry when
# scaled up — loading the big PNG and scaling DOWN avoids that entirely.
APP_LOGO_PNG_PATH = find_resource("app_icon.png")


# Custom per-page sidebar icons (PNG, transparent-ish dark background to
# match the app theme). Falls back to an emoji glyph (see PAGE_ICONS) if the
# file isn't found, so the app still works if page_icons/ wasn't shipped.
PAGE_ICON_FILES = {
    "Dashboard": "dashboard.png",
    "Tweaks": "tweaks.png",
    "Actions": "actions.png",
    "Maintenance": "maintenance.png",
    "Network": "network.png",
    "Repair": "repair.png",
    "Services": "services.png",
    "Startup": "startup.png",
    "Power": "power.png",
    "Windows Update": "windows_update.png",
    "Privacy & Telemetry": "privacy.png",
    "Apps": "apps.png",
    "USB Creator": "usb_creator.png",
    "System Info": "system_info.png",
}


# -------------------------
# Core helpers
# -------------------------

def human_bytes(n: int) -> str:
    units = ["B", "KB", "MB", "GB", "TB", "PB"]
    x = float(n)
    for u in units:
        if x < 1024:
            return f"{x:.1f} {u}"
        x /= 1024
    return f"{x:.1f} EB"


_GPU_NAME_CACHE: Optional[str] = None
_GPU_HAS_NVIDIA_SMI: Optional[bool] = None


def _shorten_gpu_name(name: str) -> str:
    name = name.strip()
    for prefix in ("NVIDIA ", "AMD ", "Intel(R) ", "Intel "):
        if name.startswith(prefix):
            name = name[len(prefix):]
            break
    return name if len(name) <= 26 else name[:24] + "…"


def get_gpu_stats() -> Tuple[str, Optional[float], Optional[float], Optional[float]]:
    """Returns (name, usage_percent, vram_used_gb, vram_total_gb) — the last
    three are None when live usage isn't obtainable (non-NVIDIA GPUs don't
    have a universal equivalent to nvidia-smi built into Windows)."""
    global _GPU_NAME_CACHE, _GPU_HAS_NVIDIA_SMI
    if not IS_WINDOWS:
        return ("N/A", None, None, None)

    if _GPU_HAS_NVIDIA_SMI is None:
        try:
            code, out, err = run_cmd(["nvidia-smi", "--query-gpu=name", "--format=csv,noheader"])
            _GPU_HAS_NVIDIA_SMI = (code == 0 and bool(out.strip()))
        except Exception:
            _GPU_HAS_NVIDIA_SMI = False

    if _GPU_HAS_NVIDIA_SMI:
        try:
            code, out, err = run_cmd([
                "nvidia-smi",
                "--query-gpu=name,utilization.gpu,memory.used,memory.total",
                "--format=csv,noheader,nounits",
            ])
            if code == 0 and out.strip():
                parts = [p.strip() for p in out.strip().splitlines()[0].split(",")]
                if len(parts) >= 4:
                    name = _shorten_gpu_name(parts[0])
                    usage = float(parts[1])
                    used_gb = float(parts[2]) / 1024
                    total_gb = float(parts[3]) / 1024
                    return (name, usage, used_gb, total_gb)
        except Exception:
            pass

    if _GPU_NAME_CACHE is None:
        try:
            code, out, err = run_powershell(
                "(Get-CimInstance Win32_VideoController | Select-Object -First 1 -ExpandProperty Name)"
            )
            _GPU_NAME_CACHE = _shorten_gpu_name(out) if (out or "").strip() else "Unknown GPU"
        except Exception:
            _GPU_NAME_CACHE = "Unknown GPU"

    return (_GPU_NAME_CACHE, None, None, None)


def run_cmd(cmd: List[str]) -> Tuple[int, str, str]:
    """Run a command safely, without shell. Returns (code, stdout, stderr)."""
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, shell=False)
        return p.returncode, (p.stdout or "").strip(), (p.stderr or "").strip()
    except Exception as e:
        return 1, "", str(e)


def run_powershell(ps: str) -> Tuple[int, str, str]:
    if not IS_WINDOWS:
        return 1, "", "PowerShell available only on Windows (in this app)."
    return run_cmd(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps])


def ps_quote(value: str) -> str:
    """Safely quote a string for embedding inside a PowerShell single-quoted
    literal, to avoid command/argument injection when we build -Command
    strings from user-chosen paths (e.g. an ISO file picked via dialog)."""
    return "'" + str(value).replace("'", "''") + "'"


def parse_windows_command_line(command: str) -> Optional[List[str]]:
    """Parse a Windows command line using the same rules as Windows itself."""
    if not IS_WINDOWS:
        return None
    argc = ctypes.c_int()
    command_line_to_argv = ctypes.windll.shell32.CommandLineToArgvW
    command_line_to_argv.argtypes = [ctypes.c_wchar_p, ctypes.POINTER(ctypes.c_int)]
    command_line_to_argv.restype = ctypes.POINTER(ctypes.c_wchar_p)
    argv = command_line_to_argv(command, ctypes.byref(argc))
    if not argv:
        return None
    try:
        return [argv[i] for i in range(argc.value)]
    finally:
        ctypes.windll.kernel32.LocalFree(ctypes.cast(argv, ctypes.c_void_p))


def shell_open(target: str, params: str = "") -> Tuple[bool, str]:
    """Open a file, folder, URL, or shell/control-panel target (like
    devmgmt.msc, services.msc, control, ms-settings:) exactly the way the
    Windows 'Run' dialog does, via ShellExecuteW.

    subprocess.run(["devmgmt.msc"]) fails with WinError 193 ("%1 is not a
    valid Win32 application") because .msc files aren't executables — they
    need to be handed to the shell (which knows to launch them via mmc.exe),
    not spawned directly as a process. ShellExecuteW does exactly that.
    """
    if not IS_WINDOWS:
        return False, "Windows only."
    try:
        shell_execute = ctypes.windll.shell32.ShellExecuteW
        shell_execute.argtypes = [
            ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_wchar_p,
            ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_int,
        ]
        shell_execute.restype = ctypes.c_void_p
        ret = shell_execute(None, "open", target, params, None, 1)
        code = int(ret)
        if code <= 32:
            return False, f"ShellExecute error code {code}"
        return True, ""
    except Exception as e:
        return False, str(e)


def is_admin() -> bool:
    if not IS_WINDOWS:
        return False
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def elevate_self():
    """Relaunch current script/exe as Admin (Windows only) and exit."""
    if not IS_WINDOWS:
        raise RuntimeError("Elevation is Windows-only.")
    if is_admin():
        return
    # When frozen (PyInstaller .exe), sys.argv[0] is the exe itself, so we
    # only need to re-pass the remaining arguments, not the exe path again.
    extra_args = sys.argv[1:] if getattr(sys, "frozen", False) else sys.argv
    params = subprocess.list2cmdline(extra_args)
    shell_execute = ctypes.windll.shell32.ShellExecuteW
    shell_execute.argtypes = [
        ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_wchar_p,
        ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_int,
    ]
    shell_execute.restype = ctypes.c_void_p
    ret = shell_execute(None, "runas", sys.executable, params, None, 1)
    if int(ret) <= 32:
        return False
    sys.exit(0)


# -------------------------
# Action History & Undo — full timeline
# -------------------------
# Each bulk-apply action (Tweaks apply, Privacy preset/apply) records an
# entry with a unique id, timestamp, description, and an undo callable.
# Unlike a single-level "undo last", entries stay visible after being
# undone (marked done->undone) so the History view is a real timeline, and
# any earlier entry can be individually rolled back, not just the latest.

_HISTORY_STACK: List[dict] = []
_HISTORY_MAX = 30
_HISTORY_NEXT_ID = 1


def record_history(description: str, undo_fn):
    global _HISTORY_NEXT_ID
    entry = {
        "id": _HISTORY_NEXT_ID,
        "time": time.strftime("%H:%M:%S"),
        "description": description,
        "undo": undo_fn,
        "undone": False,
    }
    _HISTORY_NEXT_ID += 1
    _HISTORY_STACK.append(entry)
    while len(_HISTORY_STACK) > _HISTORY_MAX:
        _HISTORY_STACK.pop(0)


def get_history_entries() -> List[dict]:
    """Most recent first."""
    return list(reversed(_HISTORY_STACK))


def peek_history_description() -> Optional[str]:
    for e in reversed(_HISTORY_STACK):
        if not e["undone"]:
            return e["description"]
    return None


def undo_last_history_entry() -> Tuple[bool, str, Optional[str]]:
    """Undoes the most recent not-yet-undone entry. Returns (ok, error, description)."""
    for e in reversed(_HISTORY_STACK):
        if not e["undone"]:
            try:
                e["undo"]()
                e["undone"] = True
                return True, "", e["description"]
            except Exception as ex:
                return False, str(ex), e["description"]
    return False, "No recent change to undo.", None


def undo_history_entry_by_id(entry_id: int) -> Tuple[bool, str, Optional[str]]:
    for e in _HISTORY_STACK:
        if e["id"] == entry_id:
            if e["undone"]:
                return False, "Already undone.", e["description"]
            try:
                e["undo"]()
                e["undone"] = True
                return True, "", e["description"]
            except Exception as ex:
                return False, str(ex), e["description"]
    return False, "Entry not found.", None


class Logger:
    """Terminal-style log panel. Colors build trust: the user can see at a
    glance what succeeded (green), failed (red), needs attention (yellow),
    or is just an informational/command line (gray/cyan)."""
    COLORS = {
        "info": "#9AA5C0",
        "success": "#4ADE80",
        "error": "#F87171",
        "warning": "#FBBF24",
        "command": "#60A5FA",
    }

    def __init__(self, widget: QTextEdit):
        self.widget = widget

    def log(self, msg: str, level: str = "info"):
        ts = time.strftime("%H:%M:%S")
        color = self.COLORS.get(level, self.COLORS["info"])
        safe_msg = (msg.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))
        self.widget.append(
            f'<span style="color:#5A6688;">[{ts}]</span> '
            f'<span style="color:{color};">{safe_msg}</span>'
        )

    def success(self, msg: str):
        self.log(msg, "success")

    def error(self, msg: str):
        self.log(msg, "error")

    def warning(self, msg: str):
        self.log(msg, "warning")

    def command(self, msg: str):
        self.log(msg, "command")


# -------------------------
# Async runner (no UI freeze)
# -------------------------

class CmdWorker(QObject):
    line = Signal(str)
    done = Signal(int, str, str)

    def __init__(self, cmd: List[str]):
        super().__init__()
        self.cmd = cmd

    def run(self):
        # For simplicity we capture output at end (reliable).
        # You can extend this to stream output if you want.
        code, out, err = run_cmd(self.cmd)
        self.done.emit(code, out, err)




_ACTIVE_THREADS = set()


def run_cmd_async(parent: QWidget, logger: Logger, cmd: List[str], title: str):
    from PySide6.QtWidgets import QProgressDialog
    thread = QThread(parent)
    worker = CmdWorker(cmd)
    worker.moveToThread(thread)

    progress = QProgressDialog(f"{title}…", None, 0, 0, parent)
    progress.setWindowTitle(title)
    progress.setWindowModality(Qt.WindowModal)
    progress.setMinimumDuration(0)
    progress.setCancelButton(None)
    progress.setAutoClose(False)
    progress.setAutoReset(False)
    progress.show()

    def on_done(code: int, out: str, err: str):
        progress.close()
        if code == 0:
            logger.success(f"{title}: exit={code} ✓")
        else:
            logger.error(f"{title}: exit={code} ✗")
        if out:
            logger.log(out)
        if err:
            logger.warning(err) if code == 0 else logger.error(err)
        if code == 0:
            QMessageBox.information(parent, title, "Done (see Log).")
        else:
            QMessageBox.warning(parent, title, f"The command failed (exit code {code}). See the Log.")
        thread.quit()
        thread.wait()

    thread.started.connect(worker.run)
    worker.done.connect(on_done, Qt.QueuedConnection)
    thread.finished.connect(worker.deleteLater)
    thread.finished.connect(thread.deleteLater)
    thread.finished.connect(lambda: _ACTIVE_THREADS.discard(thread))
    _ACTIVE_THREADS.add(thread)
    thread.start()
    logger.command(f"{title}: started -> {' '.join(cmd)}")


class UsbBurnWorker(QObject):
    """Formats a USB disk and copies a bootable ISO onto it (Rufus-like flow):
    diskpart clean/partition/format FAT32/active/assign -> mount ISO ->
    robocopy contents -> (if a Windows install.wim exceeds the FAT32 4GB
    file-size limit) split it with DISM -> dismount ISO."""
    line = Signal(str)
    done = Signal(bool, str)

    def __init__(self, iso_path: str, disk_number: int, disk_name: str = "", disk_size: int = 0):
        super().__init__()
        self.iso_path = iso_path
        self.disk_number = disk_number
        self.disk_name = disk_name
        self.disk_size = disk_size

    def _drive_letters(self) -> set:
        code, out, err = run_powershell("(Get-PSDrive -PSProvider FileSystem).Name")
        if code != 0:
            return set()
        return {l.strip() for l in out.splitlines() if l.strip() and len(l.strip()) == 1}

    def _disk_drive_letters(self) -> set:
        ps = (
            f"Get-Partition -DiskNumber {self.disk_number} -ErrorAction SilentlyContinue | "
            "Get-Volume -ErrorAction SilentlyContinue | "
            "Where-Object DriveLetter | Select-Object -ExpandProperty DriveLetter"
        )
        code, out, err = run_powershell(ps)
        if code != 0:
            return set()
        return {l.strip() for l in out.splitlines() if l.strip() and len(l.strip()) == 1}

    def _iso_drive_letters(self) -> set:
        ps = (
            f"Get-DiskImage -ImagePath {ps_quote(self.iso_path)} -ErrorAction SilentlyContinue | "
            "Get-Disk | Get-Partition | Get-Volume | "
            "Where-Object DriveLetter | Select-Object -ExpandProperty DriveLetter"
        )
        code, out, err = run_powershell(ps)
        if code != 0:
            return set()
        return {l.strip() for l in out.splitlines() if l.strip() and len(l.strip()) == 1}

    def run(self):
        iso_mounted = False
        try:
            if not isinstance(self.disk_number, int) or self.disk_number < 0:
                self.done.emit(False, "Invalid disk number.")
                return
            current_disks = list_usb_disks()
            matching_disk = next((d for d in current_disks if d.number == self.disk_number), None)
            if (matching_disk is None or
                    (self.disk_name and matching_disk.name != self.disk_name) or
                    (self.disk_size and matching_disk.size_bytes != self.disk_size)):
                self.done.emit(False, "The selected USB changed or is no longer available. Refresh the list.")
                return

            self.line.emit(f"Formatting disk {self.disk_number} (FAT32)…")
            diskpart_script = (
                f"select disk {self.disk_number}\n"
                "clean\n"
                "create partition primary\n"
                "format fs=fat32 quick\n"
                "active\n"
                "assign\n"
                "exit\n"
            )
            fd, script_path = tempfile.mkstemp(prefix="winforge_diskpart_", suffix=".txt")
            try:
                os.close(fd)
            except OSError:
                pass
            try:
                with open(script_path, "w", encoding="utf-8") as f:
                    f.write(diskpart_script)
                code, out, err = run_cmd(["diskpart", "/s", script_path])
            finally:
                try:
                    os.remove(script_path)
                except OSError:
                    pass
            if out:
                self.line.emit(out)
            if err:
                self.line.emit(err)
            if code != 0:
                self.done.emit(False, f"diskpart failed (exit code {code}).")
                return

            usb_letters = sorted(self._disk_drive_letters())
            if not usb_letters:
                self.done.emit(False, "Could not detect the USB drive letter after formatting.")
                return
            usb_letter = usb_letters[0]
            self.line.emit(f"The USB is ready at {usb_letter}:")

            self.line.emit("Mounting the ISO…")
            code, out, err = run_powershell(f"Mount-DiskImage -ImagePath {ps_quote(self.iso_path)} | Out-Null")
            if code != 0:
                self.done.emit(False, f"Failed to mount the ISO: {err or out}")
                return
            iso_mounted = True
            time.sleep(1.5)
            iso_letters = sorted(self._iso_drive_letters())
            if not iso_letters:
                run_powershell(f"Dismount-DiskImage -ImagePath {ps_quote(self.iso_path)} | Out-Null")
                self.done.emit(False, "Could not detect the ISO drive letter after mounting.")
                return
            iso_letter = iso_letters[0]
            self.line.emit(f"The ISO was mounted at {iso_letter}:")

            wim_path = f"{iso_letter}:\\sources\\install.wim"
            large_wim = os.path.isfile(wim_path) and os.path.getsize(wim_path) > 4_000_000_000

            if large_wim:
                self.line.emit(
                    "install.wim exceeds 4GB (FAT32 limit) — splitting it with DISM…"
                )
                usb_sources = f"{usb_letter}:\\sources"
                os.makedirs(usb_sources, exist_ok=True)
                swm_path = f"{usb_sources}\\install.swm"
                code, out, err = run_cmd([
                    "Dism", "/Split-Image",
                    f"/ImageFile:{wim_path}",
                    f"/SWMFile:{swm_path}",
                    "/FileSize:4000",
                ])
                if out:
                    self.line.emit(out)
                if code != 0:
                    run_powershell(f"Dismount-DiskImage -ImagePath {ps_quote(self.iso_path)} | Out-Null")
                    self.done.emit(False, f"Failed to split install.wim: {err or out}")
                    return
                self.line.emit("Copying the remaining files to the USB…")
                code, out, err = run_cmd([
                    "robocopy", f"{iso_letter}:\\", f"{usb_letter}:\\",
                    "/E", "/R:1", "/W:1", "/XF", "install.wim",
                ])
            else:
                self.line.emit("Copying files to the USB (this can take several minutes)…")
                code, out, err = run_cmd([
                    "robocopy", f"{iso_letter}:\\", f"{usb_letter}:\\", "/E", "/R:1", "/W:1",
                ])
            if out:
                self.line.emit(out)
            # robocopy: 0-7 = success variants, 8+ = failure
            if code >= 8:
                run_powershell(f"Dismount-DiskImage -ImagePath {ps_quote(self.iso_path)} | Out-Null")
                self.done.emit(False, f"robocopy failed (exit code {code}).")
                return

            self.line.emit("Dismounting the ISO…")
            run_powershell(f"Dismount-DiskImage -ImagePath {ps_quote(self.iso_path)} | Out-Null")
            iso_mounted = False

            self.done.emit(True, f"✅ The bootable USB was created successfully at {usb_letter}:")
        except Exception as e:
            if iso_mounted:
                run_powershell(f"Dismount-DiskImage -ImagePath {ps_quote(self.iso_path)} | Out-Null")
            self.done.emit(False, str(e))


# -------------------------
# Windows Registry helpers (Tweaks)
# -------------------------

def reg_set_dword(root, path: str, name: str, value: int):
    if not IS_WINDOWS:
        raise RuntimeError("Registry tweaks are Windows-only.")
    with winreg.CreateKeyEx(root, path, 0, access=winreg.KEY_SET_VALUE) as key:
        winreg.SetValueEx(key, name, 0, winreg.REG_DWORD, int(value))


def reg_get_dword(root, path: str, name: str, default: int = 0) -> int:
    if not IS_WINDOWS:
        return default
    try:
        with winreg.OpenKey(root, path, 0, access=winreg.KEY_READ) as key:
            val, regtype = winreg.QueryValueEx(key, name)
        if regtype == winreg.REG_DWORD:
            return int(val)
        return default
    except (FileNotFoundError, OSError):
        return default


def reg_set_string(root, path: str, name: str, value: str):
    if not IS_WINDOWS:
        raise RuntimeError("Registry tweaks are Windows-only.")
    with winreg.CreateKeyEx(root, path, 0, access=winreg.KEY_SET_VALUE) as key:
        winreg.SetValueEx(key, name, 0, winreg.REG_SZ, str(value))


def reg_get_string(root, path: str, name: str, default: str = "") -> str:
    if not IS_WINDOWS:
        return default
    try:
        with winreg.OpenKey(root, path, 0, access=winreg.KEY_READ) as key:
            val, regtype = winreg.QueryValueEx(key, name)
        if regtype == winreg.REG_SZ:
            return str(val)
        return default
    except (FileNotFoundError, OSError):
        return default


# -------------------------
# Installed apps (registry)
# -------------------------

@dataclass
class InstalledApp:
    name: str
    version: str
    publisher: str
    uninstall_string: str = ""
    quiet_uninstall_string: str = ""


def read_installed_apps() -> List[InstalledApp]:
    if not IS_WINDOWS:
        return []

    paths = [
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"),
        (winreg.HKEY_CURRENT_USER, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
    ]

    apps: List[InstalledApp] = []
    for root, base in paths:
        try:
            key = winreg.OpenKey(root, base, 0, winreg.KEY_READ)
        except OSError:
            continue

        i = 0
        while True:
            try:
                sub = winreg.EnumKey(key, i)
                i += 1
                skey = winreg.OpenKey(root, base + "\\" + sub, 0, winreg.KEY_READ)

                try:
                    name, _ = winreg.QueryValueEx(skey, "DisplayName")
                except OSError:
                    winreg.CloseKey(skey)
                    continue

                # Skip entries that are just OS patches/updates, not real apps
                try:
                    system_component, _ = winreg.QueryValueEx(skey, "SystemComponent")
                    if int(system_component) == 1:
                        winreg.CloseKey(skey)
                        continue
                except OSError:
                    pass

                version = ""
                publisher = ""
                uninstall_string = ""
                quiet_uninstall_string = ""
                try:
                    version, _ = winreg.QueryValueEx(skey, "DisplayVersion")
                except OSError:
                    pass
                try:
                    publisher, _ = winreg.QueryValueEx(skey, "Publisher")
                except OSError:
                    pass
                try:
                    uninstall_string, _ = winreg.QueryValueEx(skey, "UninstallString")
                except OSError:
                    pass
                try:
                    quiet_uninstall_string, _ = winreg.QueryValueEx(skey, "QuietUninstallString")
                except OSError:
                    pass

                apps.append(InstalledApp(
                    str(name), str(version), str(publisher),
                    str(uninstall_string), str(quiet_uninstall_string),
                ))
                winreg.CloseKey(skey)
            except OSError:
                break

        winreg.CloseKey(key)

    uniq = {}
    for a in apps:
        uniq[(a.name, a.version, a.publisher)] = a
    apps = list(uniq.values())
    apps.sort(key=lambda x: x.name.lower())
    return apps


# -------------------------
# Startup entries (registry + folders)
# -------------------------

@dataclass
class StartupEntry:
    location: str
    name: str
    command: str
    enabled: bool = True
    kind: str = "run"          # "run" (Run key), "run32" (WOW6432 Run key), "folder", "runonce" (no toggle)
    approved_root: int = 0     # winreg root to use for the StartupApproved lookup/write
    approved_subkey: str = ""  # "Run" | "Run32" | "StartupFolder"


STARTUP_APPROVED_BASE = r"Software\Microsoft\Windows\CurrentVersion\Explorer\StartupApproved"


def _startup_approved_enabled(root, subkey: str, value_name: str) -> bool:
    """Read the StartupApproved binary flag Task Manager uses. First byte
    0x02 = enabled, 0x03 = disabled. Missing entry defaults to enabled."""
    try:
        key = winreg.OpenKey(root, STARTUP_APPROVED_BASE + "\\" + subkey, 0, winreg.KEY_READ)
        data, _ = winreg.QueryValueEx(key, value_name)
        winreg.CloseKey(key)
        if isinstance(data, (bytes, bytearray)) and len(data) >= 1:
            return data[0] != 0x03
        return True
    except OSError:
        return True


def set_startup_enabled(entry: "StartupEntry", enabled: bool) -> Tuple[bool, str]:
    """Enable/disable a startup entry the same way Task Manager's Startup tab
    does: writes a 12-byte StartupApproved flag instead of deleting anything,
    so the change is easily reversible."""
    if not IS_WINDOWS:
        return False, "Windows only."
    if entry.kind == "runonce" or not entry.approved_subkey:
        return False, "This entry type does not support enable/disable."
    try:
        flag = bytes([0x02 if enabled else 0x03]) + bytes(11)
        path = STARTUP_APPROVED_BASE + "\\" + entry.approved_subkey
        key = winreg.CreateKeyEx(entry.approved_root, path, 0, winreg.KEY_SET_VALUE)
        winreg.SetValueEx(key, entry.name, 0, winreg.REG_BINARY, flag)
        winreg.CloseKey(key)
        return True, ""
    except Exception as e:
        return False, str(e)


def read_startup_entries() -> List[StartupEntry]:
    if not IS_WINDOWS:
        return []

    entries: List[StartupEntry] = []

    # label, root, path, kind, approved_subkey
    reg_locations = [
        ("HKCU Run", winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Run", "run", "Run"),
        ("HKCU RunOnce", winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\RunOnce", "runonce", ""),
        ("HKLM Run", winreg.HKEY_LOCAL_MACHINE, r"Software\Microsoft\Windows\CurrentVersion\Run", "run", "Run"),
        ("HKLM RunOnce", winreg.HKEY_LOCAL_MACHINE, r"Software\Microsoft\Windows\CurrentVersion\RunOnce", "runonce", ""),
        ("HKLM WOW6432 Run", winreg.HKEY_LOCAL_MACHINE, r"Software\WOW6432Node\Microsoft\Windows\CurrentVersion\Run", "run32", "Run32"),
    ]

    for label, root, path, kind, approved_subkey in reg_locations:
        try:
            key = winreg.OpenKey(root, path, 0, winreg.KEY_READ)
        except OSError:
            continue
        try:
            i = 0
            while True:
                try:
                    name, val, _ = winreg.EnumValue(key, i)
                    i += 1
                    enabled = True
                    if approved_subkey:
                        enabled = _startup_approved_enabled(root, approved_subkey, str(name))
                    entries.append(StartupEntry(
                        label, str(name), str(val), enabled, kind, root, approved_subkey,
                    ))
                except OSError:
                    break
        finally:
            winreg.CloseKey(key)

    # Startup folders — Task Manager stores their on/off flag under HKCU,
    # keyed by filename, regardless of whether it's the user or common folder.
    startup_folders = []
    try:
        startup_folders.append(("User Startup Folder", os.path.join(os.environ["APPDATA"], r"Microsoft\Windows\Start Menu\Programs\Startup")))
    except Exception:
        pass
    try:
        startup_folders.append(("Common Startup Folder", os.path.join(os.environ["PROGRAMDATA"], r"Microsoft\Windows\Start Menu\Programs\Startup")))
    except Exception:
        pass

    for label, folder in startup_folders:
        if folder and os.path.isdir(folder):
            for fn in os.listdir(folder):
                enabled = _startup_approved_enabled(winreg.HKEY_CURRENT_USER, "StartupFolder", fn)
                entries.append(StartupEntry(
                    label, fn, os.path.join(folder, fn), enabled,
                    "folder", winreg.HKEY_CURRENT_USER, "StartupFolder",
                ))

    entries.sort(key=lambda e: (e.location, e.name.lower()))
    return entries


# -------------------------
# Services (via sc.exe)
# -------------------------

@dataclass
class ServiceInfo:
    name: str
    display_name: str
    state: str


def list_services() -> List[ServiceInfo]:
    if not IS_WINDOWS:
        return []
    # PowerShell gives us display name + status in a stable way
    ps = r"Get-Service | Sort-Object DisplayName | Select-Object Name,DisplayName,Status | ConvertTo-Csv -NoTypeInformation"
    code, out, err = run_powershell(ps)
    if code != 0 or not out:
        return []
    lines = out.splitlines()
    # CSV: "Name","DisplayName","Status"
    services: List[ServiceInfo] = []
    import csv
    for line in lines[1:]:
        parts = next(csv.reader([line]), [])
        if len(parts) >= 3:
            name = parts[0]
            disp = parts[1]
            status = parts[2]
            services.append(ServiceInfo(name, disp, status))
    return services


def get_service_state(name: str) -> Tuple[str, str]:
    """Returns (state, start_mode) for a Windows service, e.g. ('RUNNING', 'AUTO')."""
    if not IS_WINDOWS:
        return "UNKNOWN", "UNKNOWN"
    state = "UNKNOWN"
    start_mode = "UNKNOWN"
    code, out, err = run_cmd(["sc", "query", name])
    if code == 0:
        for line in out.splitlines():
            if "STATE" in line:
                parts = line.split()
                if parts:
                    state = parts[-1]
    code2, out2, err2 = run_cmd(["sc", "qc", name])
    if code2 == 0:
        for line in out2.splitlines():
            if "START_TYPE" in line:
                if "DISABLED" in line:
                    start_mode = "DISABLED"
                elif "DEMAND_START" in line or "MANUAL" in line:
                    start_mode = "MANUAL"
                elif "AUTO_START" in line or "AUTOMATIC" in line:
                    start_mode = "AUTO"
                else:
                    start_mode = line.split(":")[-1].strip()
    return state, start_mode


def set_service_enabled(name: str, enabled: bool) -> Tuple[int, str, str]:
    """Enable (start_type=demand + start) or disable (stop + start_type=disabled) a service."""
    if not IS_WINDOWS:
        return 1, "", "Windows only"
    if enabled:
        code, out, err = run_cmd(["sc", "config", name, "start=", "demand"])
        if code != 0:
            return code, out, err
        return run_cmd(["sc", "start", name])
    else:
        stop_code, stop_out, stop_err = run_cmd(["sc", "stop", name])
        if stop_code != 0 and "not started" not in (stop_err or "").lower():
            return stop_code, stop_out, stop_err
        return run_cmd(["sc", "config", name, "start=", "disabled"])


# -------------------------
# USB drives (for bootable USB creator)
# -------------------------

@dataclass
class UsbDisk:
    number: int
    name: str
    size_bytes: int


def list_usb_disks() -> List["UsbDisk"]:
    """List removable USB disks (whole physical disks, not volumes) via PowerShell."""
    if not IS_WINDOWS:
        return []
    ps = (
        "Get-Disk | Where-Object { $_.BusType -eq 'USB' } | "
        "Select-Object Number, FriendlyName, Size | ConvertTo-Csv -NoTypeInformation"
    )
    code, out, err = run_powershell(ps)
    disks: List[UsbDisk] = []
    if code != 0 or not out:
        return disks
    lines = [l for l in out.splitlines() if l.strip()]
    if len(lines) < 2:
        return disks
    import csv
    for line in lines[1:]:
        parts = [p.strip() for p in next(csv.reader([line]), [])]
        if len(parts) < 3:
            continue
        try:
            num = int(parts[0])
            size = int(parts[2])
        except ValueError:
            continue
        disks.append(UsbDisk(num, parts[1] or f"Disk {num}", size))
    return disks


# -------------------------
# Power plans
# -------------------------

@dataclass
class PowerPlan:
    guid: str
    name: str
    active: bool


def list_power_plans() -> List[PowerPlan]:
    if not IS_WINDOWS:
        return []
    code, out, err = run_cmd(["powercfg", "/list"])
    if code != 0:
        return []
    plans: List[PowerPlan] = []
    for line in out.splitlines():
        line = line.strip()
        # Example: "Power Scheme GUID: xxxx-...  (Balanced) *"
        if "Power Scheme GUID:" in line:
            active = line.endswith("*")
            # Extract GUID and (name)
            try:
                guid_part = line.split("Power Scheme GUID:")[1].strip()
                guid = guid_part.split("  ")[0].strip()
                name = ""
                if "(" in line and ")" in line:
                    name = line.split("(", 1)[1].rsplit(")", 1)[0]
                plans.append(PowerPlan(guid=guid, name=name or guid, active=active))
            except Exception:
                continue
    return plans


def set_power_plan(guid: str) -> Tuple[int, str, str]:
    if not IS_WINDOWS:
        return 1, "", "Windows only"
    return run_cmd(["powercfg", "/setactive", guid])


# -------------------------
# Windows Update control
# -------------------------
# Modes:
#   "auto"          -> Windows-managed defaults (no policy override)
#   "security_only" -> defer feature/upgrade updates as long as possible,
#                       keep receiving quality/security updates normally
#   "disabled"      -> fully stop automatic updates (policy + service)

WU_POLICY_PATH = r"SOFTWARE\Policies\Microsoft\Windows\WindowsUpdate"
WU_AU_POLICY_PATH = r"SOFTWARE\Policies\Microsoft\Windows\WindowsUpdate\AU"


def get_windows_update_mode() -> str:
    if not IS_WINDOWS:
        return "unknown"
    no_auto = reg_get_dword(winreg.HKEY_LOCAL_MACHINE, WU_AU_POLICY_PATH, "NoAutoUpdate", 0)
    if no_auto == 1:
        return "disabled"
    defer = reg_get_dword(winreg.HKEY_LOCAL_MACHINE, WU_POLICY_PATH, "DeferFeatureUpdates", 0)
    if defer == 1:
        return "security_only"
    return "auto"



def apply_windows_update_mode(mode: str) -> Tuple[bool, str]:
    """Best-effort control of Windows Update via the same policy registry
    values Group Policy / Intune use. On Windows Home editions some policies
    may be partially ignored by the OS since there's no local Group Policy
    engine — the registry values are still set, but full enforcement isn't
    guaranteed outside Pro/Enterprise/Education."""
    if not IS_WINDOWS:
        return False, "Windows only."
    try:
        def run_service_command(args: List[str], allowed_codes=()):
            code, out, err = run_cmd(args)
            if code != 0 and code not in allowed_codes:
                raise RuntimeError(err or out or f"Command failed with code {code}: {' '.join(args)}")

        if mode == "auto":
            reg_set_dword(winreg.HKEY_LOCAL_MACHINE, WU_AU_POLICY_PATH, "NoAutoUpdate", 0)
            reg_set_dword(winreg.HKEY_LOCAL_MACHINE, WU_POLICY_PATH, "DeferFeatureUpdates", 0)
            run_service_command(["sc", "config", "wuauserv", "start=", "auto"])
            run_service_command(["sc", "start", "wuauserv"], {1056})
        elif mode == "security_only":
            reg_set_dword(winreg.HKEY_LOCAL_MACHINE, WU_AU_POLICY_PATH, "NoAutoUpdate", 0)
            reg_set_dword(winreg.HKEY_LOCAL_MACHINE, WU_POLICY_PATH, "DeferFeatureUpdates", 1)
            reg_set_dword(winreg.HKEY_LOCAL_MACHINE, WU_POLICY_PATH, "DeferFeatureUpdatesPeriodInDays", 365)
            reg_set_dword(winreg.HKEY_LOCAL_MACHINE, WU_POLICY_PATH, "DeferQualityUpdates", 0)
            run_service_command(["sc", "config", "wuauserv", "start=", "auto"])
            run_service_command(["sc", "start", "wuauserv"], {1056})
        elif mode == "disabled":
            reg_set_dword(winreg.HKEY_LOCAL_MACHINE, WU_AU_POLICY_PATH, "NoAutoUpdate", 1)
            run_service_command(["sc", "stop", "wuauserv"], {1062})
            run_service_command(["sc", "config", "wuauserv", "start=", "disabled"])
        else:
            return False, f"Unknown mode: {mode}"
        return True, ""
    except Exception as e:
        return False, str(e)


# -------------------------
# System Restore Points (Safety Net)
# -------------------------

def create_restore_point(description: str) -> Tuple[bool, str]:
    """Creates a System Restore Point via PowerShell Checkpoint-Computer.
    Requires System Protection to be enabled on the system drive — if it
    isn't, Windows returns a clear error message we pass through as-is."""
    if not IS_WINDOWS:
        return False, "Windows only."
    ps = (
        f"Checkpoint-Computer -Description {ps_quote(description)} "
        "-RestorePointType MODIFY_SETTINGS -ErrorAction Stop; 'OK'"
    )
    code, out, err = run_powershell(ps)
    if code == 0 and "OK" in (out or ""):
        return True, ""
    return False, (err or out or f"Unknown error (exit code {code}).")


def list_restore_points() -> List[Tuple[str, str]]:
    """Returns (creation_time, description) tuples, most recent first."""
    if not IS_WINDOWS:
        return []
    ps = (
        "Get-ComputerRestorePoint | Sort-Object -Property SequenceNumber -Descending | "
        "Select-Object -First 10 CreationTime, Description | ConvertTo-Csv -NoTypeInformation"
    )
    code, out, err = run_powershell(ps)
    points: List[Tuple[str, str]] = []
    if code != 0 or not out:
        return points
    lines = [l for l in out.splitlines() if l.strip()]
    for line in lines[1:]:
        parts = [p.strip().strip('"') for p in line.split(",", 1)]
        if len(parts) == 2:
            points.append((parts[0], parts[1]))
    return points


# -------------------------
# UI Pages
# -------------------------

class StatCard(QFrame):
    """A small glowing metric card used on the Dashboard."""
    def __init__(self, icon: str, title: str, accent: str = "#7C5CFC", show_bar: bool = True, icon_file: str = ""):
        super().__init__()
        self.setObjectName("statCard")
        self.setMinimumHeight(112)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(16, 14, 16, 14)
        lay.setSpacing(8)

        top = QHBoxLayout()
        icon_path = find_resource("dashboard_icons", icon_file) if icon_file else ""
        if icon_path:
            icon_lbl = QLabel()
            icon_lbl.setFixedSize(40, 40)
            icon_lbl.setAlignment(Qt.AlignCenter)
            pix = QPixmap(icon_path).scaled(40, 40, Qt.KeepAspectRatio, Qt.SmoothTransformation)
            icon_lbl.setPixmap(pix)
        else:
            icon_lbl = QLabel(icon)
            icon_lbl.setFixedSize(40, 40)
            icon_lbl.setAlignment(Qt.AlignCenter)
            icon_lbl.setStyleSheet(
                f"background-color: rgba({','.join(str(c) for c in hex_to_rgb(accent))},0.18);"
                f"color: {accent}; border-radius: 11px; font-size: 15pt;"
            )
        title_lbl = QLabel(title)
        title_lbl.setStyleSheet("color:#8892A6; font-weight:600; letter-spacing:0.3px;")
        top.addWidget(icon_lbl)
        top.addWidget(title_lbl, 1)
        lay.addLayout(top)

        self.value_lbl = QLabel("–")
        self.value_lbl.setStyleSheet("font-size:15pt; font-weight:700; color:#F3F5FA;")
        self.value_lbl.setWordWrap(True)
        lay.addWidget(self.value_lbl)

        self.show_bar = show_bar
        if show_bar:
            self.bar = QProgressBar()
            self.bar.setRange(0, 100)
            self.bar.setTextVisible(False)
            self.bar.setFixedHeight(6)
            self.bar.setStyleSheet(
                "QProgressBar{background:#1b2333; border-radius:3px;}"
                f"QProgressBar::chunk{{background: {accent}; border-radius:3px;}}"
            )
            lay.addWidget(self.bar)

        apply_shadow(self, blur=22, dy=6, alpha=70, color=accent)

    def set_value(self, text: str, percent: Optional[float] = None):
        self.value_lbl.setText(text)
        if self.show_bar and percent is not None:
            self.bar.setValue(max(0, min(100, int(percent))))


class PageDashboard(QWidget):
    """Live system health + top processes with a premium command-center UI."""
    def __init__(self, logger: Logger):
        super().__init__()
        self.logger = logger
        self._process_rows = []

        layout = QVBoxLayout(self)
        layout.setSpacing(12)
        intro = QLabel(
            "<p style='color:#9AA5C0;'>Live view of CPU, RAM, disk, GPU, network, and the heaviest processes.</p>"
        )
        layout.addWidget(intro)

        cards_row = QHBoxLayout()
        cards_row.setSpacing(12)
        self.card_cpu = StatCard("⚡", "CPU USAGE", accent="#7C5CFC", icon_file="cpu_usage.png")
        self.card_ram = StatCard("🧠", "MEMORY", accent="#4EA1FF", icon_file="memory.png")
        self.card_disk = StatCard("💾", "DISK", accent="#34D399", icon_file="disk.png")
        self.card_gpu = StatCard("🎮", "GPU", accent="#22D3EE", icon_file="gpu.png")
        self.card_net = StatCard("🌐", "NETWORK", accent="#FBBF24", show_bar=False, icon_file="network.png")
        self.card_uptime = StatCard("⏱️", "UPTIME", accent="#F472B6", show_bar=False, icon_file="uptime.png")
        for c in (self.card_cpu, self.card_ram, self.card_disk, self.card_gpu, self.card_net, self.card_uptime):
            cards_row.addWidget(c)
        layout.addLayout(cards_row)

        health = QFrame()
        health.setObjectName("healthStrip")
        health_row = QHBoxLayout(health)
        health_row.setContentsMargins(16, 10, 16, 10)
        health_row.setSpacing(18)
        self.health_state = QLabel("●  Checking health…")
        self.health_state.setObjectName("healthValue")
        self.health_cpu = QLabel("CPU —")
        self.health_ram = QLabel("RAM —")
        self.health_disk = QLabel("Disk —")
        self.health_os = QLabel("Windows —")
        for w in (self.health_state, self.health_cpu, self.health_ram, self.health_disk, self.health_os):
            w.setObjectName("healthMetric")
            health_row.addWidget(w)
        health_row.addStretch(1)
        layout.addWidget(health)

        secstrip = QFrame()
        secstrip.setObjectName("healthStrip")
        sec_row = QHBoxLayout(secstrip)
        sec_row.setContentsMargins(16, 10, 16, 10)
        sec_row.setSpacing(18)
        self.sec_telemetry = QLabel("🛡️  Telemetry —")
        self.sec_telemetry.setObjectName("healthMetric")
        self.sec_restore = QLabel("💾  Restore Point —")
        self.sec_restore.setObjectName("healthMetric")
        sec_row.addWidget(self.sec_telemetry)
        sec_row.addWidget(self.sec_restore)
        sec_row.addStretch(1)
        layout.addWidget(secstrip)
        QTimer.singleShot(300, self.refresh_security_status)

        table_header = QHBoxLayout()
        title = QLabel("<h3 style='color:#F3F5FA;'>🔥 Top Processes</h3>")
        table_header.addWidget(title)
        table_header.addStretch(1)
        self.process_search = QLineEdit()
        self.process_search.setObjectName("compactSearch")
        self.process_search.setPlaceholderText("🔎  Search process…")
        self.process_search.setFixedWidth(220)
        self.process_search.textChanged.connect(self.apply_process_filter)
        table_header.addWidget(self.process_search)
        layout.addLayout(table_header)

        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(["Name", "PID", "CPU%", "RAM", "Status"])
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        for col in (1, 2, 3, 4):
            self.table.horizontalHeader().setSectionResizeMode(col, QHeaderView.ResizeToContents)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.setAlternatingRowColors(True)
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(32)
        self.table.setSortingEnabled(True)
        layout.addWidget(self.table, 1)

        btns = QHBoxLayout()
        self.btn_refresh = QPushButton("↻  Refresh now")
        self.btn_refresh.setObjectName("btnSecondary")
        self.btn_kill = QPushButton("✕  Kill selected")
        self.btn_kill.setObjectName("btnDanger")
        self.btn_clear_search = QPushButton("Clear search")
        self.btn_clear_search.setObjectName("btnGhost")
        btns.addWidget(self.btn_refresh)
        btns.addWidget(self.btn_kill)
        btns.addWidget(self.btn_clear_search)
        btns.addStretch(1)
        layout.addLayout(btns)

        self.btn_refresh.clicked.connect(self.refresh)
        self.btn_kill.clicked.connect(self.kill_selected)
        self.btn_clear_search.clicked.connect(self.process_search.clear)

        self.timer = QTimer(self)
        self.timer.setInterval(1500)
        self.timer.timeout.connect(self.refresh)
        self.timer.start()

        self._last_net = psutil.net_io_counters()
        self._last_net_t = time.time()
        psutil.cpu_percent(interval=None)
        self.refresh()

    def refresh_security_status(self):
        if not IS_WINDOWS:
            self.sec_telemetry.setText("🛡️  Telemetry — N/A")
            self.sec_restore.setText("💾  Restore Point — N/A")
            return
        try:
            off_count = 0
            for t in TELEMETRY_TOGGLES:
                off = t.off_value if isinstance(t.off_value, int) else 0
                val = reg_get_dword(t.root, t.path, t.name, default=off)
                if val != t.on_value:
                    off_count += 1
            total = len(TELEMETRY_TOGGLES) or 1
            pct = round(100 * off_count / total)
            self.sec_telemetry.setText(f"🛡️  Telemetry: {pct}% blocked ({off_count}/{total})")
        except Exception:
            self.sec_telemetry.setText("🛡️  Telemetry — ?")

        try:
            points = list_restore_points()
            if points:
                self.sec_restore.setText(f"💾  Last Restore Point: {points[0][0]}")
            else:
                self.sec_restore.setText("💾  Restore Point: none yet")
        except Exception:
            self.sec_restore.setText("💾  Restore Point — ?")

    def uptime_line(self) -> str:
        try:
            secs = int(time.time() - psutil.boot_time())
            d = secs // 86400
            h = (secs % 86400) // 3600
            m = (secs % 3600) // 60
            return f"{d}d {h}h {m}m"
        except Exception:
            return "N/A"

    def _health_text(self, cpu: float, ram: float, disk_percent: Optional[float]) -> tuple[str, str]:
        pressure = max(cpu, ram, float(disk_percent or 0))
        if pressure >= 92:
            return "●  System under heavy load", "#FF8686"
        if pressure >= 80:
            return "●  System load elevated", "#FFC078"
        return "●  System healthy", "#6EE7B7"

    def _render_process_rows(self, items):
        self.table.setSortingEnabled(False)
        self.table.setRowCount(len(items))
        for r, (c, name, pid, mem, create_time) in enumerate(items):
            self.table.setItem(r, 0, QTableWidgetItem(str(name)))
            self.table.setItem(r, 1, QTableWidgetItem(str(pid)))
            self.table.item(r, 1).setData(Qt.UserRole, create_time)
            cpu_item = QTableWidgetItem(f"{c:.1f}")
            cpu_item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
            self.table.setItem(r, 2, cpu_item)
            ram_item = QTableWidgetItem(human_bytes(mem))
            ram_item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
            self.table.setItem(r, 3, ram_item)
            status = QTableWidgetItem("Running")
            status.setForeground(QColor("#6EE7B7"))
            self.table.setItem(r, 4, status)
        self.table.setSortingEnabled(True)

    def apply_process_filter(self, q: str):
        q = (q or "").strip().lower()
        if not q:
            self._render_process_rows(self._process_rows)
            return
        filtered = [p for p in self._process_rows if q in str(p[1]).lower()]
        self._render_process_rows(filtered)

    def refresh(self):
        try:
            cpu = psutil.cpu_percent(interval=None)
            vm = psutil.virtual_memory()
            path = "C:\\" if IS_WINDOWS else "/"
            try:
                du = psutil.disk_usage(path)
                disk_line = f"{human_bytes(du.used)} / {human_bytes(du.total)} (free {human_bytes(du.free)})"
                disk_percent = du.percent
            except Exception:
                disk_line = "N/A"
                disk_percent = None

            now = time.time()
            net = psutil.net_io_counters()
            dt = max(0.001, now - self._last_net_t)
            sent_s = (net.bytes_sent - self._last_net.bytes_sent) / dt
            recv_s = (net.bytes_recv - self._last_net.bytes_recv) / dt
            self._last_net, self._last_net_t = net, now

            self.card_cpu.set_value(f"{cpu:.1f}%", percent=cpu)
            self.card_ram.set_value(f"{human_bytes(vm.used)} / {human_bytes(vm.total)}", percent=vm.percent)
            self.card_disk.set_value(disk_line, percent=disk_percent)

            gpu_name, gpu_usage, gpu_used_gb, gpu_total_gb = get_gpu_stats()
            if gpu_usage is not None:
                self.card_gpu.set_value(f"{gpu_usage:.0f}%  •  {gpu_name}\n{gpu_used_gb:.1f}/{gpu_total_gb:.1f} GB", percent=gpu_usage)
            else:
                self.card_gpu.set_value(gpu_name, percent=None)

            self.card_net.set_value(f"↑ {human_bytes(int(sent_s))}/s\n↓ {human_bytes(int(recv_s))}/s")
            self.card_uptime.set_value(self.uptime_line())

            state, state_color = self._health_text(cpu, vm.percent, disk_percent)
            self.health_state.setText(state)
            self.health_state.setStyleSheet(f"color:{state_color}; font-weight:800;")
            self.health_cpu.setText(f"CPU  {cpu:.1f}%")
            self.health_ram.setText(f"RAM  {vm.percent:.0f}%")
            self.health_disk.setText(f"Disk  {disk_percent:.0f}%" if disk_percent is not None else "Disk  —")
            self.health_os.setText(f"Windows  {platform.release() if IS_WINDOWS else platform.system()}")

            procs = []
            for p in psutil.process_iter(attrs=["pid", "name", "memory_info"]):
                try:
                    c = p.cpu_percent(interval=None)
                    mem = p.info["memory_info"].rss if p.info.get("memory_info") else 0
                    procs.append((c, p.info.get("name") or "?", p.info["pid"], mem, p.create_time()))
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    continue
            procs.sort(reverse=True, key=lambda x: x[0])
            self._process_rows = procs[:15]
            self.apply_process_filter(self.process_search.text())
        except Exception as e:
            self.logger.log(f"Dashboard refresh error: {e}")

    def kill_selected(self):
        row = self.table.currentRow()
        if row < 0:
            QMessageBox.information(self, "Kill", "Select a process row first.")
            return
        pid_item = self.table.item(row, 1)
        name_item = self.table.item(row, 0)
        if not pid_item:
            return
        try:
            pid = int(pid_item.text())
        except ValueError:
            QMessageBox.warning(self, "Kill", "Selected process has an invalid PID.")
            return
        name = name_item.text() if name_item else "process"
        expected_create_time = pid_item.data(Qt.UserRole)

        ok = QMessageBox.question(
            self, "Kill process",
            f"Kill {name} (PID {pid})?",
            QMessageBox.Yes | QMessageBox.No,
        )
        if ok != QMessageBox.Yes:
            return
        try:
            process = psutil.Process(pid)
            if expected_create_time is not None and process.create_time() != expected_create_time:
                QMessageBox.warning(self, "Kill", "The process changed. The list was refreshed.")
                self.refresh()
                return
            process.terminate()
            self.logger.log(f"Killed process PID {pid} ({name})")
            QMessageBox.information(self, "Kill", "Terminate sent. It may take a moment.")
        except psutil.AccessDenied:
            QMessageBox.warning(self, "Access denied", "Access denied. Try running the app as Admin.")
            self.logger.log(f"Kill denied for PID {pid}")
        except psutil.NoSuchProcess:
            QMessageBox.information(self, "Not found", "Process no longer exists.")
        except Exception as e:
            QMessageBox.critical(self, "Error", str(e))
            self.logger.log(f"Kill error: {e}")


RISK_LEVELS = {
    "safe": ("Safe", "#4ADE80", "rgba(74,222,128,0.14)"),
    "moderate": ("Moderate", "#FBBF24", "rgba(251,191,36,0.14)"),
    "advanced": ("Advanced", "#F87171", "rgba(248,113,113,0.14)"),
}


def risk_badge(level: str) -> QLabel:
    """Small colored [Safe]/[Moderate]/[Advanced] pill so users can judge
    risk before toggling a setting — a common ask for debloat-style tools
    where people fear breaking Windows without knowing which switches are
    actually risky."""
    text, color, bg = RISK_LEVELS.get(level, RISK_LEVELS["safe"])
    lbl = QLabel(text)
    lbl.setStyleSheet(
        f"color:{color}; background:{bg}; border-radius:8px; "
        "padding:2px 8px; font-weight:700; font-size:8pt;"
    )
    lbl.setAlignment(Qt.AlignCenter)
    return lbl


class PageTweaks(QWidget):
    """Explorer tweaks with category cards and pending-change tracking."""
    ADV_PATH = r"Software\Microsoft\Windows\CurrentVersion\Explorer\Advanced"

    def __init__(self, logger: Logger):
        super().__init__()
        self.logger = logger

        layout = QVBoxLayout(self)
        layout.setSpacing(12)

        header = QFrame()
        header.setObjectName("pageIntroCard")
        h = QHBoxLayout(header)
        h.setContentsMargins(16, 12, 16, 12)
        left = QVBoxLayout()
        title = QLabel("Explorer & Windows UI")
        title.setObjectName("sectionTitle")
        desc = QLabel("Explorer behavior settings — safe, reversible, and with a live preview of the state.")
        desc.setObjectName("sectionSubtitle")
        desc.setWordWrap(True)
        left.addWidget(title)
        left.addWidget(desc)
        h.addLayout(left, 1)
        self.pending = QLabel("0 changes pending")
        self.pending.setObjectName("pendingBadge")
        h.addWidget(self.pending)
        layout.addWidget(header)

        explorer_box = QGroupBox("Explorer")
        explorer_box.setObjectName("featureCard")
        grid = QGridLayout(explorer_box)
        grid.setContentsMargins(14, 18, 14, 14)
        grid.setHorizontalSpacing(16)
        grid.setVerticalSpacing(10)

        self.cb_hidden = QCheckBox("Show hidden files")
        self.cb_hidden.setToolTip("Show hidden files and folders.")
        self.cb_ext = QCheckBox("Show file extensions")
        self.cb_ext.setToolTip("Show .exe, .png, .txt etc. in file names.")
        self.cb_super_hidden = QCheckBox("Show protected OS files")
        self.cb_super_hidden.setToolTip("Show protected operating system files. Use with caution.")
        self.cb_full_path = QCheckBox("Show full path in title bar")
        self.cb_full_path.setToolTip("Show the full path in the Explorer title bar.")

        items = [
            (self.cb_hidden, "Show hidden files", "safe"),
            (self.cb_ext, "Full file extensions", "safe"),
            (self.cb_super_hidden, "Protected OS files", "advanced"),
            (self.cb_full_path, "Full path in title bar", "safe"),
        ]
        for row, (cb, label, risk) in enumerate(items):
            grid.addWidget(cb, row, 0)
            hint = QLabel(label)
            hint.setObjectName("microHint")
            grid.addWidget(hint, row, 1)
            grid.addWidget(risk_badge(risk), row, 2)
            cb.stateChanged.connect(self.update_pending)

        layout.addWidget(explorer_box)

        performance_box = QGroupBox("Workflow")
        performance_box.setObjectName("featureCard")
        p = QHBoxLayout(performance_box)
        p.setContentsMargins(14, 18, 14, 14)
        workflow = QLabel(
            "<b>What happens:</b> changes are written to HKCU and Explorer only restarts when you press Apply."
        )
        workflow.setObjectName("sectionSubtitle")
        workflow.setWordWrap(True)
        p.addWidget(workflow, 1)
        self.btn_reset = QPushButton("Reset to current")
        self.btn_reset.setObjectName("btnGhost")
        p.addWidget(self.btn_reset)
        layout.addWidget(performance_box)

        btns = QHBoxLayout()
        self.apply_btn = QPushButton("✨  Apply changes (restart Explorer)")
        self.apply_btn.setObjectName("btnPrimary")
        refresh_btn = QPushButton("↻  Refresh state")
        refresh_btn.setObjectName("btnSecondary")
        btns.addWidget(refresh_btn)
        btns.addStretch(1)
        btns.addWidget(self.apply_btn)
        layout.addLayout(btns)
        layout.addStretch(1)

        refresh_btn.clicked.connect(self.refresh)
        self.apply_btn.clicked.connect(self.apply)
        self.btn_reset.clicked.connect(self.refresh)
        self.refresh()

    def refresh(self):
        if not IS_WINDOWS:
            for cb in (self.cb_hidden, self.cb_ext, self.cb_super_hidden, self.cb_full_path):
                cb.setEnabled(False)
            self.pending.setText("Windows only")
            return

        hidden = reg_get_dword(winreg.HKEY_CURRENT_USER, self.ADV_PATH, "Hidden", 2)
        hide_ext = reg_get_dword(winreg.HKEY_CURRENT_USER, self.ADV_PATH, "HideFileExt", 1)
        super_hidden = reg_get_dword(winreg.HKEY_CURRENT_USER, self.ADV_PATH, "ShowSuperHidden", 0)
        full_path = reg_get_dword(winreg.HKEY_CURRENT_USER, self.ADV_PATH, "FullPathAddress", 0)

        for cb in (self.cb_hidden, self.cb_ext, self.cb_super_hidden, self.cb_full_path):
            cb.blockSignals(True)
        self.cb_hidden.setChecked(hidden == 1)
        self.cb_ext.setChecked(hide_ext == 0)
        self.cb_super_hidden.setChecked(super_hidden == 1)
        self.cb_full_path.setChecked(full_path == 1)
        for cb in (self.cb_hidden, self.cb_ext, self.cb_super_hidden, self.cb_full_path):
            cb.blockSignals(False)

        self.update_pending()
        self.logger.log("Tweaks: state refreshed.")

    def _desired_values(self):
        return {
            "Hidden": 1 if self.cb_hidden.isChecked() else 2,
            "HideFileExt": 0 if self.cb_ext.isChecked() else 1,
            "ShowSuperHidden": 1 if self.cb_super_hidden.isChecked() else 0,
            "FullPathAddress": 1 if self.cb_full_path.isChecked() else 0,
        }

    def _current_values(self):
        return {
            "Hidden": reg_get_dword(winreg.HKEY_CURRENT_USER, self.ADV_PATH, "Hidden", 2),
            "HideFileExt": reg_get_dword(winreg.HKEY_CURRENT_USER, self.ADV_PATH, "HideFileExt", 1),
            "ShowSuperHidden": reg_get_dword(winreg.HKEY_CURRENT_USER, self.ADV_PATH, "ShowSuperHidden", 0),
            "FullPathAddress": reg_get_dword(winreg.HKEY_CURRENT_USER, self.ADV_PATH, "FullPathAddress", 0),
        }

    def update_pending(self, *_args):
        if not IS_WINDOWS:
            return
        try:
            desired = self._desired_values()
            current = self._current_values()
            changed = sum(desired[k] != current.get(k) for k in desired)
        except Exception:
            changed = 0
        self.pending.setText(f"{changed} changes pending")
        self.pending.setProperty("pending", changed > 0)
        self.pending.style().unpolish(self.pending)
        self.pending.style().polish(self.pending)
        self.apply_btn.setEnabled(changed > 0)

    VISUAL_MOCKUPS = {
        "Hidden": ("📁 Folder  (semi-transparent, hidden)", "📁 Folder  (fully visible)"),
        "HideFileExt": ("🖼️  photo", "🖼️  photo.jpg"),
        "FullPathAddress": ("🗂️  Documents", "🗂️  C:\\Users\\You\\Documents"),
        "ShowSuperHidden": ("📁  (no system files)", "📁  (system files visible)"),
    }

    def _mockup_chip(self, text: str, color: str) -> QLabel:
        chip = QLabel(text)
        chip.setStyleSheet(
            f"background:#161c2b; color:{color}; border:1px solid #262f45; "
            "border-radius:8px; padding:6px 10px;"
        )
        return chip

    def _show_comparison_dialog(self, changed: dict, current: dict, names: dict, preview: dict) -> bool:
        dlg = QDialog(self)
        dlg.setWindowTitle("Compare changes — before / after")
        dlg.resize(640, 380)
        lay = QVBoxLayout(dlg)
        lay.addWidget(QLabel(f"<b>{len(changed)} change(s)</b> — Explorer will restart after applying."))

        table = QTableWidget(len(changed), 4)
        table.setHorizontalHeaderLabels(["Setting", "Current", "New", "What changes visually"])
        table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeToContents)
        table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeToContents)
        table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeToContents)
        table.horizontalHeader().setSectionResizeMode(3, QHeaderView.Stretch)
        table.setEditTriggers(QTableWidget.NoEditTriggers)
        for r, (k, v) in enumerate(changed.items()):
            table.setItem(r, 0, QTableWidgetItem(names.get(k, k)))
            table.setItem(r, 1, QTableWidgetItem(str(current.get(k))))
            new_item = QTableWidgetItem(str(v))
            new_item.setForeground(QColor("#4ADE80"))
            table.setItem(r, 2, new_item)
            table.setItem(r, 3, QTableWidgetItem(preview.get(k, "")))
        lay.addWidget(table, 1)

        mockup_keys = [k for k in changed if k in self.VISUAL_MOCKUPS]
        if mockup_keys:
            lay.addWidget(QLabel("<b>👁️  Visual preview</b>"))
            for k in mockup_keys:
                before_text, after_text = self.VISUAL_MOCKUPS[k]
                row = QHBoxLayout()
                row.addWidget(QLabel(f"{names.get(k, k)}:"))
                row.addWidget(self._mockup_chip(f"BEFORE: {before_text}", "#9AA5C0"))
                row.addWidget(QLabel("→"))
                row.addWidget(self._mockup_chip(f"AFTER: {after_text}", "#4ADE80"))
                row.addStretch(1)
                lay.addLayout(row)

        btns = QHBoxLayout()
        cancel_btn = QPushButton("Cancel")
        apply_btn = QPushButton("✔  Apply")
        apply_btn.setObjectName("btnPrimary")
        btns.addStretch(1)
        btns.addWidget(cancel_btn)
        btns.addWidget(apply_btn)
        lay.addLayout(btns)

        result = {"ok": False}

        def do_apply():
            result["ok"] = True
            dlg.accept()

        apply_btn.clicked.connect(do_apply)
        cancel_btn.clicked.connect(dlg.reject)
        dlg.exec()
        return result["ok"]

    def apply(self):
        if not IS_WINDOWS:
            return
        try:
            desired = self._desired_values()
            current = self._current_values()
            changed = {k: v for k, v in desired.items() if current.get(k) != v}
            if not changed:
                self.logger.log("Tweaks: no changes to apply.")
                return

            NAMES = {
                "Hidden": "Show hidden files", "HideFileExt": "Show file extensions",
                "ShowSuperHidden": "Show protected OS files", "FullPathAddress": "Show full path in title bar",
            }
            PREVIEW = {
                "Hidden": "Hidden files/folders become semi-transparent and visible in Explorer.",
                "HideFileExt": "File names show their extension (e.g. photo.jpg instead of photo).",
                "ShowSuperHidden": "Protected system files also appear — fuller-looking folders, be careful deleting things.",
                "FullPathAddress": "The window title shows the full path instead of just the folder name.",
            }

            if not self._show_comparison_dialog(changed, current, NAMES, PREVIEW):
                return

            previous = {k: current.get(k) for k in changed}

            def undo():
                for name, value in previous.items():
                    reg_set_dword(winreg.HKEY_CURRENT_USER, self.ADV_PATH, name, value)
                run_cmd(["taskkill", "/f", "/im", "explorer.exe"])
                run_cmd(["cmd", "/c", "start", "explorer.exe"])
                self.refresh()

            for name, value in changed.items():
                reg_set_dword(winreg.HKEY_CURRENT_USER, self.ADV_PATH, name, value)
            record_history(f"Tweaks: {', '.join(NAMES.get(k, k) for k in changed)}", undo)
            self.logger.log(f"Tweaks applied: {', '.join(changed.keys())}. Restarting Explorer...")
            run_cmd(["taskkill", "/f", "/im", "explorer.exe"])
            start_code, start_out, start_err = run_cmd(["cmd", "/c", "start", "explorer.exe"])
            self.refresh()
            if start_code == 0:
                QMessageBox.information(self, "Tweaks", f"Applied {len(changed)} change(s). Explorer restarted.")
            else:
                QMessageBox.warning(self, "Tweaks", start_err or "Changes applied, but Explorer restart failed.")
        except PermissionError:
            QMessageBox.warning(self, "Permissions", "Permission denied. Try running as Admin.")
            self.logger.log("Tweaks: PermissionError.")
        except Exception as e:
            QMessageBox.critical(self, "Error", str(e))
            self.logger.log(f"Tweaks error: {e}")


class ActionCard(QFrame):
    def __init__(self, icon: str, title: str, desc: str, accent: str = "#4EA1FF", icon_file: str = ""):
        super().__init__()
        self.setObjectName("actionCard")
        self.setProperty("accent", accent)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(12, 12, 12, 12)
        lay.setSpacing(12)

        icon_path = find_resource("action_icons", icon_file) if icon_file else ""
        if icon_path:
            icon_box = QLabel()
            icon_box.setFixedSize(44, 44)
            icon_box.setAlignment(Qt.AlignCenter)
            pix = QPixmap(icon_path).scaled(44, 44, Qt.KeepAspectRatio, Qt.SmoothTransformation)
            icon_box.setPixmap(pix)
        else:
            icon_box = QLabel(icon)
            icon_box.setObjectName("actionIcon")
            icon_box.setFixedSize(40, 40)
            icon_box.setAlignment(Qt.AlignCenter)
            icon_box.setStyleSheet(
                f"background:rgba({','.join(str(c) for c in hex_to_rgb(accent))},0.15);"
                f"color:{accent}; border-radius:12px; font-size:16pt;"
            )
        lay.addWidget(icon_box)
        text_col = QVBoxLayout()
        text_col.setSpacing(2)
        title_lbl = QLabel(title)
        title_lbl.setObjectName("actionTitle")
        desc_lbl = QLabel(desc)
        desc_lbl.setObjectName("actionDesc")
        desc_lbl.setWordWrap(True)
        text_col.addWidget(title_lbl)
        text_col.addWidget(desc_lbl)
        lay.addLayout(text_col, 1)
        arrow = QLabel("→")
        arrow.setObjectName("actionArrow")
        lay.addWidget(arrow)


class PageActions(QWidget):
    """Quick actions on the OS with compact command cards."""
    def __init__(self, logger: Logger):
        super().__init__()
        self.logger = logger

        layout = QVBoxLayout(self)
        layout.setSpacing(12)
        intro = QLabel(
            "<p style='color:#9AA5C0;'>Quick tools for everyday Windows tasks. Destructive actions are kept separate and ask for confirmation.</p>"
        )
        intro.setWordWrap(True)
        layout.addWidget(intro)

        tools_box = QGroupBox("System Tools")
        tools_box.setObjectName("featureCard")
        grid = QGridLayout(tools_box)
        grid.setContentsMargins(12, 18, 12, 12)
        grid.setHorizontalSpacing(12)
        grid.setVerticalSpacing(12)

        actions = [
            ("↻", "Restart Explorer", "Restarts the Windows shell.", "#4EA1FF", self.restart_explorer, "restart_explorer.png"),
            ("▣", "Task Manager", "Manage processes and applications.", "#34D399", lambda: self.open_app("taskmgr.exe", "Task Manager"), "task_manager.png"),
            ("⚙", "Settings", "Opens Windows Settings.", "#B9A9FF", lambda: self.open_app("ms-settings:", "Settings"), "settings.png"),
            ("⌘", "Control Panel", "Classic Windows controls.", "#7C5CFC", lambda: self.open_app("control.exe", "Control Panel"), "control_panel.png"),
            ("▱", "Device Manager", "Manage devices and drivers.", "#60A5FA", lambda: self.open_app("devmgmt.msc", "Device Manager"), "device_manager.png"),
            ("⚙", "Services MMC", "View and control services.", "#A78BFA", lambda: self.open_app("services.msc", "Services"), "services_mmc.png"),
            ("◉", "Resource Monitor", "Live CPU, disk, network, and memory detail.", "#22D3EE", lambda: self.open_app("resmon.exe", "Resource Monitor"), "resource_monitor.png"),
            ("▦", "Event Viewer", "Events and system diagnostics.", "#38BDF8", lambda: self.open_app("eventvwr.msc", "Event Viewer"), "event_viewer.png"),
            ("🛡", "Windows Security", "Opens the Windows Security center.", "#6EE7B7", lambda: self.open_app("windowsdefender:", "Windows Security"), "windows_security.png"),
        ]
        for idx, (icon, title, desc, accent, handler, icon_file) in enumerate(actions):
            card = ActionCard(icon, title, desc, accent, icon_file)
            btn = QPushButton("Open")
            btn.setObjectName("btnGhost")
            btn.clicked.connect(handler)
            card.layout().addWidget(btn)
            grid.addWidget(card, idx // 3, idx % 3)
        layout.addWidget(tools_box)

        power_box = QGroupBox("Power Options")
        power_box.setObjectName("dangerCard")
        pgrid = QGridLayout(power_box)
        pgrid.setContentsMargins(12, 18, 12, 12)
        pgrid.setHorizontalSpacing(12)

        restart = QPushButton("↻  Restart PC")
        restart.setObjectName("btnDangerLarge")
        shutdown = QPushButton("⏻  Shutdown PC")
        shutdown.setObjectName("btnDangerLarge")
        restart.clicked.connect(lambda: self.power("restart"))
        shutdown.clicked.connect(lambda: self.power("shutdown"))
        pgrid.addWidget(restart, 0, 0)
        pgrid.addWidget(shutdown, 0, 1)
        layout.addWidget(power_box)

        _warn_icon_path = find_resource("status_icons", "status_warning.png")
        _warn_icon_tag = f'<img src="{_warn_icon_path}" width="14" height="14"/>&nbsp;&nbsp;' if _warn_icon_path else "⚠️  "
        note = QLabel(
            _warn_icon_tag + "Restart/Shutdown close applications and cannot be undone once confirmed."
        )
        note.setObjectName("dangerNote")
        note.setWordWrap(True)
        layout.addWidget(note)
        layout.addStretch(1)

    def open_app(self, target: str, name: str):
        if not IS_WINDOWS:
            QMessageBox.information(self, name, "Windows only.")
            return
        ok, err = shell_open(target)
        self.logger.log(f"Open {name}: {'ok' if ok else 'FAILED — ' + err}")
        if not ok:
            QMessageBox.warning(self, "Open failed", f"Could not open {name}.\n{err}")

    def restart_explorer(self):
        if not IS_WINDOWS:
            QMessageBox.information(self, "Explorer", "Windows only.")
            return
        stop_code, stop_out, stop_err = run_cmd(["taskkill", "/f", "/im", "explorer.exe"])
        start_code, start_out, start_err = run_cmd(["cmd", "/c", "start", "explorer.exe"])
        self.logger.log("Explorer restarted.")
        if start_code == 0:
            QMessageBox.information(self, "Explorer", "Explorer restarted.")
        else:
            QMessageBox.warning(self, "Explorer", start_err or stop_err or "Explorer restart failed.")

    def power(self, mode: str):
        if not IS_WINDOWS:
            QMessageBox.information(self, "Power", "Windows only.")
            return
        if mode == "restart":
            ok = QMessageBox.question(self, "Restart", "Restart now?", QMessageBox.Yes | QMessageBox.No)
            if ok == QMessageBox.Yes:
                self.logger.log("Restarting PC...")
                code, out, err = run_cmd(["shutdown", "/r", "/t", "0"])
                if code != 0:
                    QMessageBox.warning(self, "Restart", err or out or "Restart command failed.")
        elif mode == "shutdown":
            ok = QMessageBox.question(self, "Shutdown", "Shutdown now?", QMessageBox.Yes | QMessageBox.No)
            if ok == QMessageBox.Yes:
                self.logger.log("Shutting down PC...")
                code, out, err = run_cmd(["shutdown", "/s", "/t", "0"])
                if code != 0:
                    QMessageBox.warning(self, "Shutdown", err or out or "Shutdown command failed.")


class PageMaintenance(QWidget):
    """Cleanup and maintenance tools."""
    def __init__(self, logger: Logger):
        super().__init__()
        self.logger = logger

        layout = QVBoxLayout(self)
        layout.setSpacing(14)
        intro = QLabel(
            "<p style='color:#9AA5C0;'>Cleanup, repair, and optimization — using built-in Windows tools.</p>"
        )
        intro.setWordWrap(True)
        layout.addWidget(intro)

        tools_box = QGroupBox("Maintenance Tools")
        grid = QGridLayout(tools_box)
        grid.setHorizontalSpacing(14)
        grid.setVerticalSpacing(14)

        tools = [
            ("🧹", "Clean Temp Files", "Cleans up the current user's temporary files.", "#4EA1FF", self.clean_temp, "disk_cleanup.png"),
            ("🗑", "Empty Recycle Bin", "Permanently deletes everything in the Recycle Bin.", "#F87171", self.empty_recycle_bin, "empty_recycle_bin.png"),
            ("🌐", "Flush DNS", "Clears the system's DNS cache.", "#34D399", self.flush_dns, "dism_repair.png"),
            ("↺", "System Restore", "Opens Windows System Restore.", "#A78BFA", self.open_system_restore, "system_restore.png"),
            ("♥", "Reliability Monitor", "System reliability and error history.", "#F472B6", self.open_reliability_monitor, "health_check.png"),
            ("⚡", "Optimize Drives", "Drive optimization/defragmentation.", "#FBBF24", self.open_optimize_drives, "optimize.png"),
        ]
        for idx, (icon, title, desc, accent, handler, icon_file) in enumerate(tools):
            card = ActionCard(icon, title, desc, accent, icon_file)
            btn = QPushButton("Run")
            btn.setObjectName("btnGhost")
            btn.clicked.connect(handler)
            card.layout().addWidget(btn)
            grid.addWidget(card, idx // 2, idx % 2)
        layout.addWidget(tools_box)

        adv_box = QGroupBox("Advanced Tools")
        adv_grid = QGridLayout(adv_box)
        adv_grid.setHorizontalSpacing(14)
        adv_grid.setVerticalSpacing(14)

        adv_tools = [
            ("📁", "Large File Finder", "Finds the 20 largest files (>500MB) on a chosen drive.", "#60A5FA", self.find_large_files, ""),
            ("🌍", "Browser Cache Cleaner", "Clears cache from Chrome, Edge, Firefox.", "#34D399", self.clean_browser_cache, "browser_cache.png"),
        ]
        for idx, (icon, title, desc, accent, handler, icon_file) in enumerate(adv_tools):
            card = ActionCard(icon, title, desc, accent, icon_file)
            btn = QPushButton("Open")
            btn.setObjectName("btnGhost")
            btn.clicked.connect(handler)
            card.layout().addWidget(btn)
            adv_grid.addWidget(card, idx // 2, idx % 2)
        layout.addWidget(adv_box)

        layout.addStretch(1)

    def clean_temp(self):
        temp = os.environ.get("TEMP") or os.environ.get("TMP") or ""
        if not temp or not os.path.isdir(temp):
            QMessageBox.warning(self, "Temp", "Temp folder not found.")
            return
        confirm = QMessageBox.question(
            self, "Clean Temp", f"Delete the contents of:\n\n{temp}\n\nContinue?",
            QMessageBox.Yes | QMessageBox.No,
        )
        if confirm != QMessageBox.Yes:
            return

        removed = 0
        failed = 0

        for name in os.listdir(temp):
            p = os.path.join(temp, name)
            try:
                if os.path.isdir(p):
                    shutil.rmtree(p, ignore_errors=False)
                else:
                    os.remove(p)
                removed += 1
            except Exception:
                failed += 1

        self.logger.success(f"Temp clean: removed={removed}, failed={failed}, path={temp}")
        QMessageBox.information(self, "Temp", f"Done.\nRemoved: {removed}\nFailed: {failed}")

    def empty_recycle_bin(self):
        if not IS_WINDOWS:
            QMessageBox.information(self, "Recycle Bin", "Windows only.")
            return
        confirm = QMessageBox.question(
            self, "Empty Recycle Bin", "Permanently delete all items in the Recycle Bin?",
            QMessageBox.Yes | QMessageBox.No,
        )
        if confirm != QMessageBox.Yes:
            return
        code, out, err = run_powershell("Clear-RecycleBin -Force -ErrorAction Stop; 'OK'")
        self.logger.log(f"Empty Recycle Bin: code={code} {out or err}".strip())
        if code == 0:
            self.logger.success("Recycle Bin emptied.")
            QMessageBox.information(self, "Recycle Bin", "Recycle Bin emptied.")
        else:
            self.logger.error(err or out or "Could not empty Recycle Bin.")
            QMessageBox.warning(self, "Recycle Bin", err or out or "Could not empty Recycle Bin.")

    def flush_dns(self):
        if not IS_WINDOWS:
            QMessageBox.information(self, "DNS", "Windows only.")
            return
        code, out, err = run_cmd(["ipconfig", "/flushdns"])
        self.logger.log(f"Flush DNS: code={code} {out or err}".strip())
        QMessageBox.information(self, "DNS", out or err or "Done.")

    def find_large_files(self):
        if not IS_WINDOWS:
            QMessageBox.information(self, "Large File Finder", "Windows only.")
            return
        folder = QFileDialog.getExistingDirectory(self, "Choose a folder/drive to scan", "C:\\")
        if not folder:
            return
        self.logger.command(f"Scanning for large files in {folder} ...")
        min_size = 500 * 1024 * 1024  # 500MB
        found = []
        try:
            for root, dirs, files in os.walk(folder):
                for fn in files:
                    p = os.path.join(root, fn)
                    try:
                        size = os.path.getsize(p)
                        if size >= min_size:
                            found.append((size, p))
                    except OSError:
                        continue
        except Exception as e:
            QMessageBox.warning(self, "Large File Finder", f"Scan error: {e}")
            return
        found.sort(reverse=True)
        top20 = found[:20]
        if not top20:
            QMessageBox.information(self, "Large File Finder", "No files >500MB were found.")
            return
        dlg = QDialog(self)
        dlg.setWindowTitle("Large Files Found")
        dlg.resize(700, 400)
        dlay = QVBoxLayout(dlg)
        table = QTableWidget(len(top20), 2)
        table.setHorizontalHeaderLabels(["Size", "Path"])
        table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        table.setEditTriggers(QTableWidget.NoEditTriggers)
        for r, (size, path) in enumerate(top20):
            table.setItem(r, 0, QTableWidgetItem(human_bytes(size)))
            table.setItem(r, 1, QTableWidgetItem(path))
        dlay.addWidget(table)
        close_btn = QPushButton("Close")
        close_btn.clicked.connect(dlg.accept)
        dlay.addWidget(close_btn)
        self.logger.success(f"Large File Finder: {len(top20)} files found.")
        dlg.exec()

    def clean_browser_cache(self):
        if not IS_WINDOWS:
            QMessageBox.information(self, "Browser Cache", "Windows only.")
            return
        local = os.environ.get("LOCALAPPDATA", "")
        targets = {
            "Chrome": os.path.join(local, r"Google\Chrome\User Data\Default\Cache"),
            "Edge": os.path.join(local, r"Microsoft\Edge\User Data\Default\Cache"),
            "Firefox": os.path.join(local, r"Mozilla\Firefox\Profiles"),
        }
        existing = {name: path for name, path in targets.items() if os.path.isdir(path)}
        if not existing:
            QMessageBox.information(self, "Browser Cache", "No cache folders for known browsers were found.")
            return
        confirm = QMessageBox.question(
            self, "Browser Cache",
            "Clear cache for: " + ", ".join(existing.keys()) + "\n\nContinue?",
            QMessageBox.Yes | QMessageBox.No,
        )
        if confirm != QMessageBox.Yes:
            return
        cleared, failed = 0, 0
        for name, path in existing.items():
            try:
                for entry in os.listdir(path):
                    p = os.path.join(path, entry)
                    try:
                        if os.path.isdir(p):
                            shutil.rmtree(p, ignore_errors=True)
                        else:
                            os.remove(p)
                        cleared += 1
                    except Exception:
                        failed += 1
            except Exception:
                failed += 1
        self.logger.success(f"Browser cache cleaned: {cleared} items removed, {failed} failed.")
        QMessageBox.information(self, "Browser Cache", f"Done.\nCleared: {cleared}\nFailed: {failed}")

    def open_system_restore(self):
        if not IS_WINDOWS:
            QMessageBox.information(self, "System Restore", "Windows only.")
            return
        ok, err = shell_open("rstrui.exe")
        self.logger.log(f"Open System Restore: {'ok' if ok else 'FAILED — ' + err}")
        if not ok:
            QMessageBox.warning(self, "System Restore", f"Could not open System Restore.\n{err}")

    def open_reliability_monitor(self):
        if not IS_WINDOWS:
            QMessageBox.information(self, "Reliability Monitor", "Windows only.")
            return
        ok, err = shell_open("perfmon.exe", "/rel")
        self.logger.log(f"Open Reliability Monitor: {'ok' if ok else 'FAILED — ' + err}")
        if not ok:
            QMessageBox.warning(self, "Reliability Monitor", f"Could not open Reliability Monitor.\n{err}")

    def open_optimize_drives(self):
        if not IS_WINDOWS:
            QMessageBox.information(self, "Optimize Drives", "Windows only.")
            return
        ok, err = shell_open("dfrgui.exe")
        self.logger.log(f"Open Optimize Drives: {'ok' if ok else 'FAILED — ' + err}")
        if not ok:
            QMessageBox.warning(self, "Optimize Drives", f"Could not open Optimize Drives.\n{err}")


class PageNetwork(QWidget):
    """Network diagnostics and resets."""
    DNS_PRESETS = load_json_override("dns_presets_custom.json", {
        "Cloudflare (1.1.1.1)": ["1.1.1.1", "1.0.0.1"],
        "Google (8.8.8.8)": ["8.8.8.8", "8.8.4.4"],
        "Quad9 (9.9.9.9)": ["9.9.9.9", "149.112.112.112"],
        "AdGuard DNS (ad-blocking)": ["94.140.14.14", "94.140.15.15"],
    })

    def __init__(self, logger: Logger):
        super().__init__()
        self.logger = logger

        layout = QVBoxLayout(self)
        layout.setSpacing(14)
        layout.addWidget(QLabel("<p style='color:#9AA5C0;'>Diagnostics and reset tools.</p>"))

        tools_box = QGroupBox("Network Tools")
        tools_box.setObjectName("featureCard")
        tools_lay = QVBoxLayout(tools_box)

        row1 = QHBoxLayout()
        self.btn_ip = QPushButton("Show IP config")
        self.btn_adapters = QPushButton("Open Network Adapters")
        row1.addWidget(self.btn_ip)
        row1.addWidget(self.btn_adapters)
        tools_lay.addLayout(row1)

        row2 = QHBoxLayout()
        self.ping_input = QLineEdit()
        self.ping_input.setPlaceholderText("Ping host (e.g. 8.8.8.8 or google.com)")
        self.btn_ping = QPushButton("Ping")
        row2.addWidget(self.ping_input)
        row2.addWidget(self.btn_ping)
        tools_lay.addLayout(row2)

        row3 = QHBoxLayout()
        self.btn_release = QPushButton("IP Release/Renew (Admin)")
        self.btn_winsock = QPushButton("Reset Winsock (Admin)")
        row3.addWidget(self.btn_release)
        row3.addWidget(self.btn_winsock)
        tools_lay.addLayout(row3)
        layout.addWidget(tools_box)

        dns_box = QGroupBox("🌐  1-Click DNS Switcher (Admin)")
        dns_lay = QHBoxLayout(dns_box)
        self.combo_dns = QComboBox()
        self.combo_dns.addItems(list(self.DNS_PRESETS.keys()))
        self.btn_dns_apply = QPushButton("Apply")
        self.btn_dns_apply.setObjectName("btnPrimary")
        self.btn_dns_reset = QPushButton("Reset to Automatic (DHCP)")
        self.btn_dns_reset.setObjectName("btnGhost")
        dns_lay.addWidget(self.combo_dns, 1)
        dns_lay.addWidget(self.btn_dns_apply)
        dns_lay.addWidget(self.btn_dns_reset)
        layout.addWidget(dns_box)

        wifi_box = QGroupBox("📶  Saved Wi-Fi Passwords")
        wifi_lay = QVBoxLayout(wifi_box)
        wifi_note = QLabel(
            "Shows the Wi-Fi passwords for networks THIS computer has connected to."
        )
        wifi_note.setObjectName("appSubtitle")
        wifi_note.setWordWrap(True)
        wifi_lay.addWidget(wifi_note)
        self.btn_wifi_scan = QPushButton("🔍  Show Saved Wi-Fi")
        wifi_lay.addWidget(self.btn_wifi_scan)
        self.wifi_table = QTableWidget(0, 2)
        self.wifi_table.setHorizontalHeaderLabels(["Network (SSID)", "Password"])
        self.wifi_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeToContents)
        self.wifi_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self.wifi_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.wifi_table.setMaximumHeight(150)
        wifi_lay.addWidget(self.wifi_table)
        wifi_hint = QLabel("🔒  Passwords are hidden by default — double-click one to reveal/hide it.")
        wifi_hint.setObjectName("appSubtitle")
        wifi_lay.addWidget(wifi_hint)
        layout.addWidget(wifi_box)

        layout.addStretch(1)

        self.btn_ip.clicked.connect(self.show_ip)
        self.btn_adapters.clicked.connect(self.open_adapters)
        self.btn_ping.clicked.connect(self.ping)
        self.btn_release.clicked.connect(self.release_renew)
        self.btn_winsock.clicked.connect(self.reset_winsock)
        self.btn_dns_apply.clicked.connect(self.apply_dns)
        self.btn_dns_reset.clicked.connect(self.reset_dns)
        self.btn_wifi_scan.clicked.connect(self.show_wifi_passwords)
        self.wifi_table.itemDoubleClicked.connect(self.toggle_wifi_password_visibility)

    def ensure_admin_or_offer(self) -> bool:
        if is_admin():
            return True
        r = QMessageBox.question(
            self, "Admin required",
            "This action requires Admin. Relaunch the app as Admin now?",
            QMessageBox.Yes | QMessageBox.No
        )
        if r == QMessageBox.Yes:
            self.logger.log("Elevation requested.")
            elevate_self()
        return False

    def show_ip(self):
        if not IS_WINDOWS:
            QMessageBox.information(self, "IP", "Windows only in this app.")
            return
        code, out, err = run_cmd(["ipconfig", "/all"])
        self.logger.log("ipconfig /all:")
        self.logger.log(out if out else err)
        QMessageBox.information(self, "IP", "Printed to Log.")

    def open_adapters(self):
        if not IS_WINDOWS:
            return
        run_cmd(["cmd", "/c", "start", "ncpa.cpl"])
        self.logger.log("Opened network adapters.")

    def ping(self):
        host = (self.ping_input.text() or "").strip() or "8.8.8.8"
        cmd = ["ping", "-n", "4", host] if IS_WINDOWS else ["ping", "-c", "4", host]
        code, out, err = run_cmd(cmd)
        self.logger.log(f"Ping {host}:")
        self.logger.log(out if out else err)
        QMessageBox.information(self, "Ping", "Printed to Log.")

    def release_renew(self):
        if not IS_WINDOWS:
            return
        if not self.ensure_admin_or_offer():
            return
        self.logger.log("Running ipconfig /release ...")
        release_code, release_out, release_err = run_cmd(["ipconfig", "/release"])
        self.logger.log("Running ipconfig /renew ...")
        code, out, err = run_cmd(["ipconfig", "/renew"])
        self.logger.log(out if out else err)
        if release_code == 0 and code == 0:
            QMessageBox.information(self, "IP", "Release/Renew finished (see Log).")
        else:
            QMessageBox.warning(self, "IP", release_err or err or "Release/Renew failed.")

    def reset_winsock(self):
        if not IS_WINDOWS:
            return
        if not self.ensure_admin_or_offer():
            return
        code, out, err = run_cmd(["netsh", "winsock", "reset"])
        self.logger.log(f"Winsock reset: {out or err}".strip())
        if code == 0:
            QMessageBox.information(self, "Winsock", "Reset done. A restart may be required.")
        else:
            QMessageBox.warning(self, "Winsock", err or out or "Winsock reset failed.")

    def _active_adapter_name(self) -> Optional[str]:
        code, out, err = run_powershell(
            "(Get-NetAdapter | Where-Object {$_.Status -eq 'Up'} | Select-Object -First 1 -ExpandProperty Name)"
        )
        name = (out or "").strip()
        return name or None

    def apply_dns(self):
        if not IS_WINDOWS:
            QMessageBox.information(self, "DNS", "Windows only.")
            return
        if not self.ensure_admin_or_offer():
            return
        adapter = self._active_adapter_name()
        if not adapter:
            QMessageBox.warning(self, "DNS", "No active network adapter was found.")
            return
        preset_name = self.combo_dns.currentText()
        primary, secondary = self.DNS_PRESETS[preset_name]
        confirm = QMessageBox.question(
            self, "DNS", f"Change DNS on adapter '{adapter}' to {preset_name} ({primary}, {secondary})?",
            QMessageBox.Yes | QMessageBox.No,
        )
        if confirm != QMessageBox.Yes:
            return
        self.logger.command(f"Set DNS on '{adapter}' -> {primary}, {secondary}")
        code1, out1, err1 = run_cmd(["netsh", "interface", "ip", "set", "dns", f"name={adapter}", "static", primary, "primary"])
        code2, out2, err2 = run_cmd(["netsh", "interface", "ip", "add", "dns", f"name={adapter}", secondary, "index=2"])
        if code1 == 0 and code2 == 0:
            self.logger.success(f"DNS set to {preset_name}.")
            QMessageBox.information(self, "DNS", f"DNS was set to {preset_name}.")
        else:
            self.logger.error((err1 or out1 or "") + " " + (err2 or out2 or ""))
            QMessageBox.warning(self, "DNS", "Failed to change DNS. See the Log.")

    def reset_dns(self):
        if not IS_WINDOWS:
            QMessageBox.information(self, "DNS", "Windows only.")
            return
        if not self.ensure_admin_or_offer():
            return
        adapter = self._active_adapter_name()
        if not adapter:
            QMessageBox.warning(self, "DNS", "No active network adapter was found.")
            return
        self.logger.command(f"Reset DNS on '{adapter}' -> DHCP")
        code, out, err = run_cmd(["netsh", "interface", "ip", "set", "dns", f"name={adapter}", "dhcp"])
        if code == 0:
            self.logger.success("DNS reset to automatic (DHCP).")
            QMessageBox.information(self, "DNS", "DNS was reset to automatic (DHCP).")
        else:
            self.logger.error(err or out or "DNS reset failed.")
            QMessageBox.warning(self, "DNS", err or out or "Failed to reset DNS.")

    def show_wifi_passwords(self):
        if not IS_WINDOWS:
            QMessageBox.information(self, "Wi-Fi", "Windows only.")
            return
        code, out, err = run_cmd(["netsh", "wlan", "show", "profiles"])
        if code != 0:
            QMessageBox.warning(self, "Wi-Fi", err or out or "Could not read Wi-Fi profiles.")
            return
        names = []
        for line in (out or "").splitlines():
            if "All User Profile" in line or "Προφίλ όλων των χρηστών" in line:
                parts = line.split(":", 1)
                if len(parts) == 2:
                    names.append(parts[1].strip())
        results = []
        for name in names:
            code2, out2, err2 = run_cmd(["netsh", "wlan", "show", "profile", f"name={name}", "key=clear"])
            password = "(none / open network)"
            for line in (out2 or "").splitlines():
                if "Key Content" in line or "Περιεχόμενο βασικού κλειδιού" in line:
                    parts = line.split(":", 1)
                    if len(parts) == 2:
                        password = parts[1].strip()
            results.append((name, password))

        self.wifi_table.setRowCount(len(results))
        for r, (name, pw) in enumerate(results):
            self.wifi_table.setItem(r, 0, QTableWidgetItem(name))
            has_password = pw != "(none / open network)"
            pw_item = QTableWidgetItem(self.WIFI_PASSWORD_MASK if has_password else pw)
            pw_item.setData(Qt.UserRole, pw)
            pw_item.setData(Qt.UserRole + 1, False)  # revealed?
            self.wifi_table.setItem(r, 1, pw_item)
        self.logger.log(f"Wi-Fi profiles found: {len(results)}")

    WIFI_PASSWORD_MASK = "••••••••"

    def toggle_wifi_password_visibility(self, item: QTableWidgetItem):
        """Double-click a Wi-Fi password cell to reveal/hide it — passwords
        stay masked by default so they don't show up in a screenshot or
        over-the-shoulder glance just from opening this page."""
        if item.column() != 1:
            return
        real_password = item.data(Qt.UserRole)
        if not real_password or real_password == "(none / open network)":
            return
        revealed = bool(item.data(Qt.UserRole + 1))
        item.setText(self.WIFI_PASSWORD_MASK if revealed else real_password)
        item.setData(Qt.UserRole + 1, not revealed)


class PageRepair(QWidget):
    """Repair tools (Admin) - async so it won't freeze."""
    def __init__(self, logger: Logger):
        super().__init__()
        self.logger = logger

        layout = QVBoxLayout(self)
        layout.setSpacing(14)
        layout.addWidget(QLabel(
            "<p style='color:#9AA5C0;'>System repair commands (usually Admin). Run asynchronously.</p>"
        ))

        layout.addSpacing(10)

        safety_box = QGroupBox("🛡️  Safety Net — before you do anything risky")
        safety_lay = QVBoxLayout(safety_box)
        safety_desc = QLabel(
            "Create a System Restore Point before tweaks or repairs, so you can "
            "roll Windows back to its current state if something goes wrong."
        )
        safety_desc.setObjectName("appSubtitle")
        safety_desc.setWordWrap(True)
        safety_lay.addWidget(safety_desc)

        rp_row = QHBoxLayout()
        self.btn_restore_point = QPushButton("💾  Create Restore Point Now")
        self.btn_restore_point.setObjectName("btnSuccess")
        self.btn_refresh_points = QPushButton("↻  Refresh List")
        rp_row.addWidget(self.btn_restore_point)
        rp_row.addWidget(self.btn_refresh_points)
        safety_lay.addLayout(rp_row)

        self.restore_points_list = QListWidget()
        self.restore_points_list.setMaximumHeight(110)
        safety_lay.addWidget(self.restore_points_list)

        layout.addWidget(safety_box)
        layout.addSpacing(20)

        repair_box = QGroupBox("Repair Tools")
        repair_box.setObjectName("featureCard")
        repair_lay = QVBoxLayout(repair_box)

        row1 = QHBoxLayout()
        self.btn_sfc = QPushButton("🩹  SFC /scannow (Admin)")
        self.btn_dism = QPushButton("🩺  DISM RestoreHealth (Admin)")
        row1.addWidget(self.btn_sfc)
        row1.addWidget(self.btn_dism)
        repair_lay.addLayout(row1)

        row2 = QHBoxLayout()
        self.btn_chkdsk = QPushButton("💽  Schedule CHKDSK on C: (Admin)")
        self.btn_wu = QPushButton("🔄  Open Windows Update")
        row2.addWidget(self.btn_chkdsk)
        row2.addWidget(self.btn_wu)
        repair_lay.addLayout(row2)

        row3 = QHBoxLayout()
        self.btn_wureset = QPushButton("♻️  Reset Windows Update Components (Admin)")
        self.btn_wureset.setObjectName("btnWarn")
        row3.addWidget(self.btn_wureset)
        row3.addStretch(1)
        repair_lay.addLayout(row3)
        layout.addWidget(repair_box)

        layout.addStretch(1)

        self.btn_sfc.clicked.connect(self.run_sfc)
        self.btn_dism.clicked.connect(self.run_dism)
        self.btn_chkdsk.clicked.connect(self.schedule_chkdsk)
        self.btn_wu.clicked.connect(self.open_windows_update)
        self.btn_wureset.clicked.connect(self.reset_windows_update_components)
        self.btn_restore_point.clicked.connect(self.create_restore_point_now)
        self.btn_refresh_points.clicked.connect(self.refresh_restore_points)

        self.refresh_restore_points()

    def create_restore_point_now(self):
        if not IS_WINDOWS:
            QMessageBox.information(self, "Restore Point", "Windows only.")
            return
        if not self.ensure_admin_or_offer():
            return
        desc, ok = QInputDialog.getText(
            self, "Restore Point", "Description (optional):",
            text=f"WinForge — {time.strftime('%Y-%m-%d %H:%M')}"
        )
        if not ok:
            return
        desc = desc.strip() or "WinForge manual restore point"
        self.logger.command(f"Creating restore point: {desc}")
        self.btn_restore_point.setEnabled(False)
        success, err = create_restore_point(desc)
        self.btn_restore_point.setEnabled(True)
        if success:
            self.logger.success("Restore point created.")
            QMessageBox.information(self, "Restore Point", "The Restore Point was created successfully.")
            self.refresh_restore_points()
        else:
            self.logger.error(f"Restore point failed: {err}")
            QMessageBox.warning(
                self, "Restore Point",
                f"Creation failed.\n\n{err}\n\n"
                "Common cause: System Protection is disabled for drive C:. "
                "Enable it via: Control Panel → System → System Protection."
            )

    def refresh_restore_points(self):
        if not IS_WINDOWS:
            return
        self.restore_points_list.clear()
        points = list_restore_points()
        if not points:
            self.restore_points_list.addItem("(No restore points found, or System Protection is not enabled)")
            return
        for creation_time, desc in points:
            self.restore_points_list.addItem(f"🕐  {creation_time}  —  {desc}")

    def open_windows_update(self):
        if not IS_WINDOWS:
            QMessageBox.information(self, "Windows Update", "Windows only.")
            return
        ok, err = shell_open("ms-settings:windowsupdate")
        if not ok:
            QMessageBox.warning(self, "Windows Update", f"Could not open Windows Update.\n{err}")

    def ensure_admin_or_offer(self) -> bool:
        if is_admin():
            return True
        r = QMessageBox.question(
            self, "Admin required",
            "This action requires Admin. Relaunch the app as Admin now?",
            QMessageBox.Yes | QMessageBox.No
        )
        if r == QMessageBox.Yes:
            self.logger.log("Elevation requested.")
            elevate_self()
        return False

    def run_sfc(self):
        if not IS_WINDOWS:
            QMessageBox.information(self, "SFC", "Windows only.")
            return
        if not self.ensure_admin_or_offer():
            return
        run_cmd_async(self, self.logger, ["sfc", "/scannow"], "SFC")

    def run_dism(self):
        if not IS_WINDOWS:
            QMessageBox.information(self, "DISM", "Windows only.")
            return
        if not self.ensure_admin_or_offer():
            return
        run_cmd_async(self, self.logger, ["DISM", "/Online", "/Cleanup-Image", "/RestoreHealth"], "DISM")

    def schedule_chkdsk(self):
        if not IS_WINDOWS:
            QMessageBox.information(self, "CHKDSK", "Windows only.")
            return
        if not self.ensure_admin_or_offer():
            return
        self.logger.log("Scheduling: chkdsk C: /f (next reboot)")
        run_cmd_async(self, self.logger, ["cmd", "/c", "echo Y|chkdsk C: /f"], "CHKDSK schedule")

    def reset_windows_update_components(self):
        if not IS_WINDOWS:
            return
        if not self.ensure_admin_or_offer():
            return
        # Safe-ish classic reset steps (no deletion of user data)
        # stops services, renames caches, restarts services
        cmds = [
            ["net", "stop", "wuauserv"],
            ["net", "stop", "bits"],
            ["net", "stop", "cryptsvc"],
            ["net", "stop", "msiserver"],
            ["cmd", "/c", "ren %systemroot%\\SoftwareDistribution SoftwareDistribution.old"],
            ["cmd", "/c", "ren %systemroot%\\System32\\catroot2 catroot2.old"],
            ["net", "start", "msiserver"],
            ["net", "start", "cryptsvc"],
            ["net", "start", "bits"],
            ["net", "start", "wuauserv"],
        ]
        self.logger.log("Windows Update reset: starting (see Log).")
        failed_steps = 0
        # run sequentially (fast enough)
        for c in cmds:
            code, out, err = run_cmd(c)
            self.logger.log(f"CMD: {' '.join(c)} -> {code}")
            if code != 0:
                failed_steps += 1
            if out:
                self.logger.log(out)
            if err:
                self.logger.log(err)
        if failed_steps:
            QMessageBox.warning(
                self, "Windows Update Reset",
                f"Completed with {failed_steps} failure(s) (see Log).",
            )
        else:
            QMessageBox.information(self, "Windows Update Reset", "Completed (see Log).")


class PageApps(QWidget):
    """Installed apps list (registry) with install/uninstall support."""
    def __init__(self, logger: Logger):
        super().__init__()
        self.logger = logger
        self.apps: List[InstalledApp] = []
        self._winget_checked = False
        self._winget_available = False

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(
            "<p style='color:#9AA5C0;'>Installed applications — install &amp; uninstall.</p>"
        ))

        self.search = QLineEdit()
        self.search.setPlaceholderText("🔎  Search applications…")
        layout.addWidget(self.search)

        self.table = QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels(["Name", "Version", "Publisher"])
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeToContents)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.setAlternatingRowColors(True)
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(30)
        layout.addWidget(self.table, 1)

        row1 = QHBoxLayout()
        self.refresh_btn = QPushButton("↻  Refresh")
        self.uninstall_btn = QPushButton("🗑️  Uninstall selected")
        self.uninstall_btn.setObjectName("btnDanger")
        self.quiet_check = QCheckBox("Silent uninstall, if supported")
        row1.addWidget(self.refresh_btn)
        row1.addWidget(self.uninstall_btn)
        row1.addWidget(self.quiet_check, 1)
        layout.addLayout(row1)

        install_box = QGroupBox("➕  Install New Application")
        install_layout = QVBoxLayout(install_box)

        file_row = QHBoxLayout()
        self.install_file_btn = QPushButton("📂  Choose .exe / .msi and Install")
        file_row.addWidget(self.install_file_btn)
        file_row.addStretch(1)
        install_layout.addLayout(file_row)

        winget_row = QHBoxLayout()
        self.winget_search = QLineEdit()
        self.winget_search.setPlaceholderText("Package name to install via winget (e.g. 'VLC', '7zip.7zip')…")
        self.winget_btn = QPushButton("⬇️  Install via winget")
        winget_row.addWidget(self.winget_search, 1)
        winget_row.addWidget(self.winget_btn)
        install_layout.addLayout(winget_row)
        self.winget_hint = QLabel("")
        self.winget_hint.setObjectName("appSubtitle")
        install_layout.addWidget(self.winget_hint)

        runtimes_row = QHBoxLayout()
        runtimes_label = QLabel("Required runtimes for games/applications:")
        runtimes_label.setObjectName("appSubtitle")
        self.btn_runtimes = QPushButton("📦  Install All Runtimes (VC++, DirectX, .NET)")
        self.btn_runtimes.setObjectName("btnPrimary")
        runtimes_row.addWidget(runtimes_label, 1)
        runtimes_row.addWidget(self.btn_runtimes)
        install_layout.addLayout(runtimes_row)

        layout.addWidget(install_box)
        layout.addSpacing(16)
        layout.addStretch(1)

        self.refresh_btn.clicked.connect(self.load)
        self.search.textChanged.connect(self.filter_list)
        self.uninstall_btn.clicked.connect(self.uninstall_selected)
        self.install_file_btn.clicked.connect(self.install_from_file)
        self.winget_btn.clicked.connect(self.install_via_winget)
        self.btn_runtimes.clicked.connect(self.install_all_runtimes)

        self.load()
        self.check_winget()

    RUNTIME_PACKAGES = load_json_override("runtime_packages_custom.json", [
        ["Microsoft.VCRedist.2015+.x64", "VC++ Redistributable 2015-2022 (x64)"],
        ["Microsoft.VCRedist.2015+.x86", "VC++ Redistributable 2015-2022 (x86)"],
        ["Microsoft.DotNet.DesktopRuntime.8", ".NET Desktop Runtime 8"],
        ["Microsoft.DirectX", "DirectX Runtime"],
    ])

    def install_all_runtimes(self):
        if not IS_WINDOWS or not self._winget_available:
            QMessageBox.information(
                self, "Runtimes",
                "winget is required. See the hint above if it wasn't detected."
            )
            return
        confirm = QMessageBox.question(
            self, "Runtimes",
            "Install all core runtimes (VC++ Redistributables, .NET Desktop Runtime, DirectX)?\n\n"
            "This can take several minutes.",
            QMessageBox.Yes | QMessageBox.No,
        )
        if confirm != QMessageBox.Yes:
            return
        for package_id, label in self.RUNTIME_PACKAGES:
            self.logger.command(f"winget install {package_id} ({label})")
            cmd = ["winget", "install", "--id", package_id, "-e", "--accept-package-agreements",
                   "--accept-source-agreements", "--silent"]
            run_cmd_async(self, self.logger, cmd, f"Install: {label}")

    # ---- data ----
    def load(self):
        self.apps = read_installed_apps()
        self.logger.log(f"Apps loaded: {len(self.apps)}")
        self.render(self.apps)

    def render(self, items: List[InstalledApp]):
        self.table.setRowCount(len(items))
        for r, a in enumerate(items):
            name_item = QTableWidgetItem(a.name)
            name_item.setData(Qt.UserRole, a)
            self.table.setItem(r, 0, name_item)
            self.table.setItem(r, 1, QTableWidgetItem(a.version))
            self.table.setItem(r, 2, QTableWidgetItem(a.publisher))

    def filter_list(self, q: str):
        q = (q or "").lower().strip()
        if not q:
            self.render(self.apps)
            return
        filtered = [a for a in self.apps if q in f"{a.name} {a.version} {a.publisher}".lower()]
        self.render(filtered)

    def selected_app(self) -> Optional[InstalledApp]:
        row = self.table.currentRow()
        if row < 0:
            return None
        item = self.table.item(row, 0)
        return item.data(Qt.UserRole) if item else None

    # ---- uninstall ----
    def uninstall_selected(self):
        if not IS_WINDOWS:
            QMessageBox.information(self, "Uninstall", "Windows only.")
            return
        app = self.selected_app()
        if not app:
            QMessageBox.information(self, "Uninstall", "First select an application from the list.")
            return

        cmdline = ""
        if self.quiet_check.isChecked() and app.quiet_uninstall_string:
            cmdline = app.quiet_uninstall_string
        elif app.uninstall_string:
            cmdline = app.uninstall_string
        elif app.quiet_uninstall_string:
            cmdline = app.quiet_uninstall_string

        if not cmdline:
            QMessageBox.warning(self, "Uninstall", "No uninstaller was found for this application.")
            return

        confirm = QMessageBox.question(
            self, "Confirm Uninstall",
            f"Uninstall the application:\n\n{app.name}\n\nThe uninstaller registered by "
            f"the package itself will be launched. Continue?",
            QMessageBox.Yes | QMessageBox.No,
        )
        if confirm != QMessageBox.Yes:
            return

        uninstall_args = parse_windows_command_line(cmdline)
        if not uninstall_args:
            QMessageBox.warning(self, "Uninstall", "Could not safely parse the uninstall command.")
            return
        run_cmd_async(self, self.logger, uninstall_args, f"Uninstall: {app.name}")
        QTimer.singleShot(4000, self.load)

    # ---- install from local file ----
    def install_from_file(self):
        if not IS_WINDOWS:
            QMessageBox.information(self, "Install", "Windows only.")
            return
        path, _ = QFileDialog.getOpenFileName(
            self, "Choose installer", "", "Installers (*.exe *.msi);;All files (*.*)"
        )
        if not path:
            return
        confirm = QMessageBox.question(
            self, "Confirm Installation",
            f"Start installation from:\n\n{path}\n\n"
            "Only run files from sources you trust. Continue?",
            QMessageBox.Yes | QMessageBox.No,
        )
        if confirm != QMessageBox.Yes:
            return

        if path.lower().endswith(".msi"):
            cmd = ["msiexec", "/i", path]
        else:
            cmd = [path]
        run_cmd_async(self, self.logger, cmd, f"Install: {os.path.basename(path)}")
        QTimer.singleShot(6000, self.load)

    # ---- install via winget ----
    def check_winget(self):
        if not IS_WINDOWS:
            self.winget_hint.setText("winget: unavailable (not Windows)")
            self.winget_btn.setEnabled(False)
            return
        code, out, err = run_cmd(["winget", "--version"])
        self._winget_available = (code == 0)
        self._winget_checked = True
        if self._winget_available:
            self.winget_hint.setText(f"winget detected ({out or 'ok'}).")
        else:
            self.winget_hint.setText("winget was not detected on this system — use 'Choose .exe/.msi' instead.")
            self.winget_btn.setEnabled(False)

    def install_via_winget(self):
        if not IS_WINDOWS or not self._winget_available:
            return
        query = self.winget_search.text().strip()
        if not query:
            QMessageBox.information(self, "winget", "First enter the package name.")
            return
        confirm = QMessageBox.question(
            self, "Confirm Installation",
            f"Install '{query}' via winget?",
            QMessageBox.Yes | QMessageBox.No,
        )
        if confirm != QMessageBox.Yes:
            return
        cmd = ["winget", "install", "--id", query, "-e", "--accept-package-agreements",
               "--accept-source-agreements"]
        # Fallback: if --id exact match fails, winget also accepts free text via -q,
        # but we keep this explicit and let the user retry with the exact winget id
        # shown by `winget search <name>` for best results.
        run_cmd_async(self, self.logger, cmd, f"winget install: {query}")
        QTimer.singleShot(8000, self.load)


KNOWN_STARTUP_PROGRAMS = {
    "onedrive": "Microsoft OneDrive file sync.",
    "dropbox": "Dropbox file sync.",
    "steam": "Steam client — background updates/friends.",
    "discord": "Discord client — messaging/calls.",
    "spotify": "Music player Spotify.",
    "skype": "Skype calling/messaging app.",
    "teams": "Microsoft Teams — work chat/video calls.",
    "zoom": "Zoom — video calls.",
    "cortana": "Windows digital assistant.",
    "adobe": "Adobe products background updater.",
    "realtek": "Audio driver utility.",
    "nvidia": "NVIDIA GeForce Experience / display driver utility.",
    "epicgameslauncher": "Epic Games Store client.",
    "battle.net": "Blizzard games client.",
    "corsair": "iCUE — software for Corsair peripherals.",
    "logitech": "Software for Logitech mouse/keyboard.",
    "cctray": "CCleaner background monitor.",
    "avast": "Antivirus Avast.",
    "mcafee": "Antivirus McAfee.",
}
# External file can ADD more entries (e.g. company-specific software)
# without touching the source — merged on top of the built-in defaults.
KNOWN_STARTUP_PROGRAMS.update(load_json_override("startup_programs_custom.json", {}))


def explain_startup_program(name: str, command: str) -> str:
    text = f"{name} {command}".lower()
    for key, explanation in KNOWN_STARTUP_PROGRAMS.items():
        if key in text:
            return explanation
    return "Unknown program — check the path if you don't recognize it."


class PageStartup(QWidget):
    """Startup entries viewer with enable/disable support (like Task Manager)."""
    def __init__(self, logger: Logger):
        super().__init__()
        self.logger = logger

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(
            "<p style='color:#9AA5C0;'>Startup entries — enable/disable without deleting.</p>"
        ))

        self.search = QLineEdit()
        self.search.setPlaceholderText("🔎  Search startup entries…")
        layout.addWidget(self.search)

        self.table = QTableWidget(0, 5)
        self.table.setIconSize(QSize(18, 18))
        self.table.setHorizontalHeaderLabels(["Status", "Location", "Name", "Command/Path", "What it does"])
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(3, QHeaderView.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(4, QHeaderView.Stretch)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.setAlternatingRowColors(True)
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(30)
        layout.addWidget(self.table, 1)

        btns = QHBoxLayout()
        self.btn_refresh = QPushButton("↻  Refresh")
        self.btn_enable = QPushButton("✅  Enable selected")
        self.btn_enable.setObjectName("btnSuccess")
        self.btn_disable = QPushButton("⛔  Disable selected")
        self.btn_disable.setObjectName("btnDanger")
        self.btn_open_user = QPushButton("📂  User Startup Folder")
        self.btn_open_common = QPushButton("📂  Common Startup Folder")
        btns.addWidget(self.btn_refresh)
        btns.addWidget(self.btn_enable)
        btns.addWidget(self.btn_disable)
        btns.addStretch(1)
        btns.addWidget(self.btn_open_user)
        btns.addWidget(self.btn_open_common)
        layout.addLayout(btns)

        hint = QLabel(
            "ℹ️ Disabling uses the same mechanism as Task Manager's "
            "Startup tab — nothing is deleted, so you can undo it anytime."
        )
        hint.setObjectName("appSubtitle")
        hint.setWordWrap(True)
        layout.addWidget(hint)

        self.entries: List[StartupEntry] = []
        self.btn_refresh.clicked.connect(self.load)
        self.search.textChanged.connect(self.apply_filter)
        self.btn_enable.clicked.connect(lambda: self.set_enabled(True))
        self.btn_disable.clicked.connect(lambda: self.set_enabled(False))

        self.btn_open_user.clicked.connect(self.open_user_startup)
        self.btn_open_common.clicked.connect(self.open_common_startup)

        self.load()

    def load(self):
        self.entries = read_startup_entries()
        self.logger.log(f"Startup entries loaded: {len(self.entries)}")
        self.render(self.entries)

    def render(self, items: List[StartupEntry]):
        self.table.setRowCount(len(items))
        enabled_icon = find_resource("status_icons", "status_enabled.png")
        disabled_icon = find_resource("status_icons", "status_disabled.png")
        for r, e in enumerate(items):
            if e.kind == "runonce":
                status_item = QTableWidgetItem("— (RunOnce)")
            else:
                status_item = QTableWidgetItem(" Enabled" if e.enabled else " Disabled")
                icon_path = enabled_icon if e.enabled else disabled_icon
                if icon_path:
                    status_item.setIcon(QIcon(icon_path))
            status_item.setData(Qt.UserRole, e)
            self.table.setItem(r, 0, status_item)
            self.table.setItem(r, 1, QTableWidgetItem(e.location))
            self.table.setItem(r, 2, QTableWidgetItem(e.name))
            self.table.setItem(r, 3, QTableWidgetItem(e.command))
            explain_item = QTableWidgetItem(explain_startup_program(e.name, e.command))
            explain_item.setForeground(QColor("#9AA5C0"))
            self.table.setItem(r, 4, explain_item)

    def apply_filter(self, q: str):
        q = (q or "").lower().strip()
        if not q:
            self.render(self.entries)
            return
        filtered = [e for e in self.entries if q in (e.location + " " + e.name + " " + e.command).lower()]
        self.render(filtered)

    def selected_entry(self) -> Optional[StartupEntry]:
        row = self.table.currentRow()
        if row < 0:
            return None
        item = self.table.item(row, 0)
        return item.data(Qt.UserRole) if item else None

    def set_enabled(self, enabled: bool):
        if not IS_WINDOWS:
            QMessageBox.information(self, "Startup", "Windows only.")
            return
        entry = self.selected_entry()
        if not entry:
            QMessageBox.information(self, "Startup", "First select an entry.")
            return
        if entry.kind == "runonce":
            QMessageBox.information(self, "Startup", "RunOnce entries do not support enable/disable.")
            return
        needs_admin = entry.approved_root == winreg.HKEY_LOCAL_MACHINE if IS_WINDOWS else False
        if needs_admin and not is_admin():
            QMessageBox.warning(self, "Admin Required",
                                 "This entry applies to all users — open the app as Administrator.")
            return
        ok, err = set_startup_enabled(entry, enabled)
        verb = "enabled" if enabled else "disabled"
        if ok:
            self.logger.log(f"Startup '{entry.name}' {verb}.")
        else:
            self.logger.log(f"Startup toggle failed for '{entry.name}': {err}")
            QMessageBox.warning(self, "Startup", f"Failed: {err}")
        self.load()

    def open_user_startup(self):
        if not IS_WINDOWS:
            return
        path = os.path.join(os.environ.get("APPDATA", ""), r"Microsoft\Windows\Start Menu\Programs\Startup")
        shell_open(path)
        self.logger.log("Opened user startup folder.")

    def open_common_startup(self):
        if not IS_WINDOWS:
            return
        path = os.path.join(os.environ.get("PROGRAMDATA", ""), r"Microsoft\Windows\Start Menu\Programs\Startup")
        shell_open(path)
        self.logger.log("Opened common startup folder.")


class PageServices(QWidget):
    """Basic service control (Start/Stop) via sc.exe (Admin often required)."""
    def __init__(self, logger: Logger):
        super().__init__()
        self.logger = logger

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(
            "<p style='color:#9AA5C0;'>View/Start/Stop Windows services.</p>"
        ))

        self.search = QLineEdit()
        self.search.setPlaceholderText("🔎  Search services… (display name or name)")
        layout.addWidget(self.search)

        self.table = QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels(["Name", "Display Name", "Status"])
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeToContents)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.table.setAlternatingRowColors(True)
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(30)
        layout.addWidget(self.table, 1)

        btns = QHBoxLayout()
        self.btn_refresh = QPushButton("↻  Refresh")
        self.btn_start = QPushButton("▶  Start selected")
        self.btn_stop = QPushButton("■  Stop selected")
        self.btn_start.setObjectName("btnSuccess")
        self.btn_stop.setObjectName("btnDanger")
        btns.addWidget(self.btn_refresh)
        btns.addWidget(self.btn_start)
        btns.addWidget(self.btn_stop)
        btns.addStretch(1)
        layout.addLayout(btns)

        self.services: List[ServiceInfo] = []
        self.btn_refresh.clicked.connect(self.load)
        self.search.textChanged.connect(self.apply_filter)
        self.btn_start.clicked.connect(lambda: self.control_selected("start"))
        self.btn_stop.clicked.connect(lambda: self.control_selected("stop"))

        self.load()

    def ensure_admin_or_offer(self) -> bool:
        if is_admin():
            return True
        r = QMessageBox.question(
            self, "Admin required",
            "This action requires Admin. Relaunch the app as Admin now?",
            QMessageBox.Yes | QMessageBox.No
        )
        if r == QMessageBox.Yes:
            self.logger.log("Elevation requested.")
            elevate_self()
        return False

    def load(self):
        if not IS_WINDOWS:
            self.logger.log("Services: Windows only.")
            return
        self.services = list_services()
        self.logger.log(f"Services loaded: {len(self.services)}")
        self.render(self.services)

    def render(self, items: List[ServiceInfo]):
        self.table.setRowCount(len(items))
        for r, s in enumerate(items):
            self.table.setItem(r, 0, QTableWidgetItem(s.name))
            self.table.setItem(r, 1, QTableWidgetItem(s.display_name))
            self.table.setItem(r, 2, QTableWidgetItem(s.state))

    def apply_filter(self, q: str):
        q = (q or "").lower().strip()
        if not q:
            self.render(self.services)
            return
        filtered = [s for s in self.services if q in (s.name + " " + s.display_name).lower()]
        self.render(filtered)

    def selected_service_name(self) -> Optional[str]:
        row = self.table.currentRow()
        if row < 0:
            return None
        it = self.table.item(row, 0)
        return it.text() if it else None

    def control_selected(self, action: str):
        name = self.selected_service_name()
        if not name:
            QMessageBox.information(self, "Service", "Select a service first.")
            return
        if not self.ensure_admin_or_offer():
            return

        if action == "start":
            run_cmd_async(self, self.logger, ["sc", "start", name], f"Start service {name}")
        else:
            run_cmd_async(self, self.logger, ["sc", "stop", name], f"Stop service {name}")


class PagePower(QWidget):
    """Power plans list + set active."""
    def __init__(self, logger: Logger):
        super().__init__()
        self.logger = logger
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("<p>Power plans (powercfg).</p>"))

        row = QHBoxLayout()
        self.combo = QComboBox()
        self.btn_refresh = QPushButton("Refresh")
        self.btn_set = QPushButton("Set active")
        row.addWidget(QLabel("Plan:"))
        row.addWidget(self.combo, 1)
        row.addWidget(self.btn_refresh)
        row.addWidget(self.btn_set)
        layout.addLayout(row)

        self.info = QLabel("")
        self.info.setTextInteractionFlags(Qt.TextSelectableByMouse)
        layout.addWidget(self.info)

        layout.addSpacing(22)

        booster_box = QGroupBox("🎮  Game Booster Mode")
        booster_box.setObjectName("featureCard")
        booster_lay = QVBoxLayout(booster_box)
        booster_desc = QLabel(
            "Enables the Ultimate Performance power plan, clears Standby Memory, and "
            "temporarily suspends heavy background services (Search, Print Spooler)."
        )
        booster_desc.setObjectName("appSubtitle")
        booster_desc.setWordWrap(True)
        booster_lay.addWidget(booster_desc)
        booster_row = QHBoxLayout()
        booster_row.setSpacing(10)
        self.btn_booster_on = QPushButton("🚀  Enable Booster")
        self.btn_booster_on.setObjectName("btnSuccess")
        self.btn_booster_on.setMinimumHeight(38)
        self.btn_booster_off = QPushButton("↩  Restore Normal Mode")
        self.btn_booster_off.setObjectName("btnGhost")
        self.btn_booster_off.setMinimumHeight(38)
        booster_row.addWidget(self.btn_booster_on)
        booster_row.addWidget(self.btn_booster_off)
        booster_lay.addLayout(booster_row)
        layout.addWidget(booster_box)

        layout.addStretch(1)

        self.plans: List[PowerPlan] = []
        self.btn_refresh.clicked.connect(self.load)
        self.btn_set.clicked.connect(self.set_active)
        self.btn_booster_on.clicked.connect(self.enable_game_booster)
        self.btn_booster_off.clicked.connect(self.disable_game_booster)

        self.load()

    def ensure_admin_or_offer(self) -> bool:
        if is_admin():
            return True
        r = QMessageBox.question(
            self, "Admin required",
            "This action requires Administrator privileges. Restart the app as Admin?",
            QMessageBox.Yes | QMessageBox.No
        )
        if r == QMessageBox.Yes:
            self.logger.log("Elevation requested.")
            elevate_self()
        return False

    BOOSTER_SERVICES = ["WSearch", "Spooler"]
    ULTIMATE_PERF_GUID = "e9a42b02-d5df-448d-aa00-03f14749eb61"

    def enable_game_booster(self):
        if not IS_WINDOWS:
            QMessageBox.information(self, "Game Booster", "Windows only.")
            return
        if not self.ensure_admin_or_offer():
            return
        self.logger.command("Game Booster: enabling Ultimate Performance plan...")
        run_cmd(["powercfg", "-duplicatescheme", self.ULTIMATE_PERF_GUID])
        code, out, err = run_cmd(["powercfg", "/setactive", self.ULTIMATE_PERF_GUID])
        if code != 0:
            self.logger.warning("Ultimate Performance plan not available — trying High Performance instead.")
            run_cmd(["powercfg", "/setactive", "8c5e7fda-e8bf-4a96-9a85-a6e23a8c635c"])

        self.logger.command("Game Booster: clearing standby memory...")
        run_cmd(["powershell", "-NoProfile", "-Command",
                  "Add-Type -TypeDefinition 'using System; using System.Runtime.InteropServices; "
                  "public class Memory { [DllImport(\"psapi.dll\")] public static extern bool EmptyWorkingSet(IntPtr h); }' "
                  "-ErrorAction SilentlyContinue"])

        for svc in self.BOOSTER_SERVICES:
            self.logger.command(f"Game Booster: stopping service {svc}...")
            run_cmd(["sc", "stop", svc])

        self.logger.success("Game Booster Mode is active.")
        QMessageBox.information(self, "Game Booster", "Game Booster Mode is active.")
        self.load()

    def disable_game_booster(self):
        if not IS_WINDOWS:
            QMessageBox.information(self, "Game Booster", "Windows only.")
            return
        if not self.ensure_admin_or_offer():
            return
        self.logger.command("Game Booster: restoring balanced plan and services...")
        run_cmd(["powercfg", "/setactive", "381b4222-f694-41f0-9685-ff5bb260df2e"])  # Balanced
        for svc in self.BOOSTER_SERVICES:
            run_cmd(["sc", "start", svc])
        self.logger.success("Restoring normal mode completed.")
        QMessageBox.information(self, "Game Booster", "Normal mode has been restored.")
        self.load()

    def load(self):
        if not IS_WINDOWS:
            self.info.setText("Windows only.")
            return
        self.plans = list_power_plans()
        self.combo.clear()
        active_name = None
        for p in self.plans:
            label = f"{p.name} ({p.guid})" + ("  *ACTIVE*" if p.active else "")
            self.combo.addItem(label, p.guid)
            if p.active:
                active_name = p.name
        self.info.setText(f"Found {len(self.plans)} plans. Active: {active_name or 'Unknown'}")
        self.logger.log(f"Power plans loaded: {len(self.plans)}")

    def set_active(self):
        if not IS_WINDOWS:
            return
        guid = self.combo.currentData()
        if not guid:
            return
        code, out, err = set_power_plan(guid)
        self.logger.log(f"Set power plan: {guid} -> code={code} {out or err}".strip())
        if code == 0:
            QMessageBox.information(self, "Power", "Power plan set. (See Log)")
            self.load()
        else:
            QMessageBox.warning(self, "Power", err or out or "Failed (See Log)")


class PageWindowsUpdate(QWidget):
    """Windows Update control: Automatic / Security-only / Fully disabled."""
    def __init__(self, logger: Logger):
        super().__init__()
        self.logger = logger

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(
            "<p style='color:#9AA5C0;'>Choose how much control you want over updates.</p>"
        ))

        self.status_label = QLabel("")
        self.status_label.setObjectName("appSubtitle")
        layout.addWidget(self.status_label)

        self.group = QButtonGroup(self)

        card_auto = self._mode_card(
            "auto", "🟢  Automatic (recommended)",
            "Windows manages updates normally — you get security and feature "
            "updates as soon as they're available.",
            "wu_auto.png",
        )
        card_security = self._mode_card(
            "security_only", "🛡️  Security updates only",
            "Feature updates are deferred as long as possible (up to 365 "
            "days), but security updates keep arriving normally.",
            "wu_security.png",
        )
        card_disabled = self._mode_card(
            "disabled", "⛔  Fully disabled",
            "Windows Update stops entirely (policy + service). Not recommended for "
            "long periods — it leaves the system without new security fixes.",
            "wu_disabled.png",
        )
        layout.addWidget(card_auto)
        layout.addWidget(card_security)
        layout.addWidget(card_disabled)

        btn_row = QHBoxLayout()
        self.btn_refresh = QPushButton("↻  Refresh")
        self.btn_apply = QPushButton("✅  Apply Selection")
        btn_row.addWidget(self.btn_refresh)
        btn_row.addStretch(1)
        btn_row.addWidget(self.btn_apply)
        layout.addLayout(btn_row)

        note = QLabel(
            "ℹ️ On Windows Home edition some policies may not be fully enforced "
            "without Group Policy — the registry values are still applied normally."
        )
        note.setObjectName("appSubtitle")
        note.setWordWrap(True)
        layout.addWidget(note)

        layout.addStretch(1)

        self.btn_refresh.clicked.connect(self.refresh_status)
        self.btn_apply.clicked.connect(self.apply_selected)
        self.refresh_status()

    def _mode_card(self, value: str, title: str, desc: str, icon_file: str = "") -> QGroupBox:
        card = QGroupBox()
        card.setObjectName("privacyCard")
        apply_shadow(card, blur=16, dy=3, alpha=90, color="#7C5CFC")
        row = QHBoxLayout(card)
        radio = QRadioButton()
        radio.setProperty("mode_value", value)
        self.group.addButton(radio)
        setattr(self, f"radio_{value}", radio)
        row.addWidget(radio)

        icon_path = find_resource("wu_icons", icon_file) if icon_file else ""
        if icon_path:
            icon_lbl = QLabel()
            icon_lbl.setFixedSize(56, 56)
            icon_lbl.setAlignment(Qt.AlignCenter)
            pix = QPixmap(icon_path).scaled(56, 56, Qt.KeepAspectRatio, Qt.SmoothTransformation)
            icon_lbl.setPixmap(pix)
            row.addWidget(icon_lbl)

        text_col = QVBoxLayout()
        t = QLabel(f"<b>{title}</b>")
        d = QLabel(desc)
        d.setObjectName("appSubtitle")
        d.setWordWrap(True)
        text_col.addWidget(t)
        text_col.addWidget(d)
        row.addLayout(text_col, 1)
        return card

    def ensure_admin_or_offer(self) -> bool:
        if is_admin():
            return True
        r = QMessageBox.question(
            self, "Admin required",
            "Controlling Windows Update requires Administrator privileges. Restart as Admin?",
            QMessageBox.Yes | QMessageBox.No
        )
        if r == QMessageBox.Yes:
            self.logger.log("Elevation requested.")
            elevate_self()
        return False

    def refresh_status(self):
        if not IS_WINDOWS:
            self.status_label.setText("Windows only.")
            return
        mode = get_windows_update_mode()
        labels = {"auto": "Automatic", "security_only": "Security only", "disabled": "Disabled"}
        self.status_label.setText(f"Current status: {labels.get(mode, mode)}")
        radio = getattr(self, f"radio_{mode}", None)
        if radio:
            radio.setChecked(True)
        self.logger.log(f"Windows Update mode: {mode}")

    def apply_selected(self):
        if not IS_WINDOWS:
            QMessageBox.information(self, "Windows Update", "Windows only.")
            return
        mode = None
        for name in ("auto", "security_only", "disabled"):
            radio = getattr(self, f"radio_{name}")
            if radio.isChecked():
                mode = name
                break
        if not mode:
            QMessageBox.information(self, "Windows Update", "First select a mode.")
            return
        if mode == "disabled":
            confirm = QMessageBox.question(
                self, "Confirm",
                "Fully disabling Windows Update also stops security updates. "
                "Are you sure you want to continue?",
                QMessageBox.Yes | QMessageBox.No,
            )
            if confirm != QMessageBox.Yes:
                return
        if not self.ensure_admin_or_offer():
            return
        ok, err = apply_windows_update_mode(mode)
        if ok:
            self.logger.log(f"Windows Update mode set to: {mode}")
            QMessageBox.information(self, "Windows Update", "The setting was applied.")
        else:
            self.logger.log(f"Windows Update mode change failed: {err}")
            QMessageBox.warning(self, "Windows Update", f"Failed: {err}")
        self.refresh_status()


class PageUsbCreator(QWidget):
    """Bootable USB creator (Rufus-like) — writes any .iso onto a USB drive."""
    def __init__(self, logger: Logger):
        super().__init__()
        self.logger = logger
        self.iso_path = ""
        self.disks: List[UsbDisk] = []
        self.thread: Optional[QThread] = None
        self.worker: Optional[UsbBurnWorker] = None

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(
            "<p style='color:#9AA5C0;'>Create a bootable USB from an .iso file — for any operating system.</p>"
        ))

        _usb_warn_icon = find_resource("status_icons", "status_warning.png")
        _usb_warn_tag = f'<img src="{_usb_warn_icon}" width="14" height="14"/>&nbsp;&nbsp;' if _usb_warn_icon else "⚠️  "
        warn = QLabel(
            _usb_warn_tag + "This process deletes ALL data on the USB stick you select. "
            "Make sure you've chosen the correct drive before continuing."
        )
        warn.setObjectName("dangerNote")
        warn.setWordWrap(True)
        layout.addWidget(warn)

        usb_box = QGroupBox("USB Setup")
        usb_box.setObjectName("featureCard")
        usb_lay = QVBoxLayout(usb_box)

        iso_row = QHBoxLayout()
        self.iso_field = QLineEdit()
        self.iso_field.setReadOnly(True)
        self.iso_field.setPlaceholderText("No .iso file selected…")
        self.btn_browse = QPushButton("📂  Choose ISO")
        iso_row.addWidget(self.iso_field, 1)
        iso_row.addWidget(self.btn_browse)
        usb_lay.addLayout(iso_row)

        disk_row = QHBoxLayout()
        disk_row.addWidget(QLabel("USB drive:"))
        self.disk_combo = QComboBox()
        self.btn_refresh_disks = QPushButton("↻  Refresh")
        disk_row.addWidget(self.disk_combo, 1)
        disk_row.addWidget(self.btn_refresh_disks)
        usb_lay.addLayout(disk_row)

        self.btn_create = QPushButton("🔥  Create Bootable USB")
        self.btn_create.setObjectName("btnDanger")
        self.btn_create.setMinimumHeight(38)
        usb_lay.addWidget(self.btn_create)
        layout.addWidget(usb_box)

        layout.addWidget(QLabel("<b>📜 Progress</b>"))
        self.progress_log = QTextEdit()
        self.progress_log.setObjectName("logBox")
        self.progress_log.setReadOnly(True)
        self.progress_log.setFixedHeight(210)
        layout.addWidget(self.progress_log)

        note = QLabel(
            "ℹ️ Uses diskpart (FAT32, so it boots on UEFI) and copies the ISO's "
            "contents. For Windows ISOs with install.wim &gt; 4GB, it's automatically split using DISM."
        )
        note.setObjectName("appSubtitle")
        note.setWordWrap(True)
        layout.addWidget(note)

        self.btn_browse.clicked.connect(self.browse_iso)
        self.btn_refresh_disks.clicked.connect(self.refresh_disks)
        self.btn_create.clicked.connect(self.start_burn)

        if IS_WINDOWS:
            self.refresh_disks()
        else:
            self.setEnabled(False)

    def browse_iso(self):
        path, _ = QFileDialog.getOpenFileName(self, "Choose ISO", "", "ISO images (*.iso)")
        if path:
            self.iso_path = path
            self.iso_field.setText(path)
            self.logger.log(f"ISO selected: {path}")

    def refresh_disks(self):
        if not IS_WINDOWS:
            return
        self.disks = list_usb_disks()
        self.disk_combo.clear()
        for d in self.disks:
            gb = d.size_bytes / (1024 ** 3)
            self.disk_combo.addItem(f"Disk {d.number}: {d.name} ({gb:.1f} GB)", d.number)
        self.logger.log(f"USB drives found: {len(self.disks)}")
        if not self.disks:
            self.disk_combo.addItem("No USB drive found", None)

    def start_burn(self):
        if not IS_WINDOWS:
            return
        if not self.iso_path or not os.path.isfile(self.iso_path):
            QMessageBox.information(self, "USB Creator", "First select an .iso file.")
            return
        disk_number = self.disk_combo.currentData()
        if disk_number is None:
            QMessageBox.information(self, "USB Creator", "No USB drive found. Connect one and press 'Refresh'.")
            return
        disk_label = self.disk_combo.currentText()
        selected_disk = next((d for d in self.disks if d.number == disk_number), None)
        if selected_disk is None:
            QMessageBox.information(self, "USB Creator", "The selected USB was not found. Press 'Refresh'.")
            return

        if not is_admin():
            QMessageBox.warning(
                self, "Admin Required",
                "Writing to USB requires Administrator privileges. Open the app as Admin.",
            )
            return

        text, ok = QInputDialog.getText(
            self, "Final Confirmation — irreversible",
            f"ALL data on:\n\n{disk_label}\n\nwill be deleted.\n\n"
            "Type ERASE (uppercase) to continue:",
        )
        if not ok or text.strip() != "ERASE":
            self.logger.log("USB burn cancelled (confirmation not matched).")
            return

        self.btn_create.setEnabled(False)
        self.progress_log.clear()
        self.logger.log(f"USB burn started: {disk_label} <- {self.iso_path}")

        self.thread = QThread(self)
        self.worker = UsbBurnWorker(
            self.iso_path, disk_number, selected_disk.name, selected_disk.size_bytes
        )
        self.worker.moveToThread(self.thread)
        self.worker.line.connect(self._on_line)
        self.worker.done.connect(self._on_done)
        self.thread.started.connect(self.worker.run)
        self.thread.finished.connect(lambda: _ACTIVE_THREADS.discard(self.thread))
        _ACTIVE_THREADS.add(self.thread)
        self.thread.start()

    def _on_line(self, text: str):
        if text:
            self.progress_log.append(text)

    def _on_done(self, ok: bool, message: str):
        self.progress_log.append(message)
        self.logger.log(f"USB burn finished: {'OK' if ok else 'FAILED'} — {message}")
        self.btn_create.setEnabled(True)
        if self.thread:
            self.thread.quit()
            self.thread.wait()
        if ok:
            QMessageBox.information(self, "USB Creator", message)
        else:
            QMessageBox.warning(self, "USB Creator", message)
        self.refresh_disks()


class PageSystemInfo(QWidget):
    """Static-ish system info."""
    def __init__(self, logger: Logger):
        super().__init__()
        self.logger = logger

        layout = QVBoxLayout(self)
        layout.setSpacing(14)
        layout.addWidget(QLabel("<p style='color:#9AA5C0;'>General system information.</p>"))

        self.label = QLabel()
        self.label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.btn = QPushButton("Refresh")

        layout.addWidget(self.label)
        layout.addWidget(self.btn)

        layout.addSpacing(20)

        health_box = QGroupBox("💽  Drive Health (S.M.A.R.T.)")
        health_lay = QVBoxLayout(health_box)
        self.btn_drive_health = QPushButton("🔍  Check Drive Health")
        health_lay.addWidget(self.btn_drive_health)
        self.drive_table = QTableWidget(0, 4)
        self.drive_table.setHorizontalHeaderLabels(["Drive", "Status", "Temperature", "Lifespan"])
        self.drive_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.drive_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.drive_table.setMaximumHeight(140)
        health_lay.addWidget(self.drive_table)
        layout.addWidget(health_box)

        layout.addStretch(1)

        self.btn.clicked.connect(self.refresh)
        self.btn_drive_health.clicked.connect(self.check_drive_health)
        self.refresh()

    def check_drive_health(self):
        if not IS_WINDOWS:
            QMessageBox.information(self, "Drive Health", "Windows only.")
            return
        ps = (
            "Get-PhysicalDisk | Select-Object FriendlyName, HealthStatus, "
            "@{N='Temp';E={(Get-StorageReliabilityCounter -PhysicalDisk $_ -ErrorAction SilentlyContinue).Temperature}}, "
            "@{N='Wear';E={(Get-StorageReliabilityCounter -PhysicalDisk $_ -ErrorAction SilentlyContinue).Wear}} "
            "| ConvertTo-Csv -NoTypeInformation"
        )
        code, out, err = run_powershell(ps)
        self.drive_table.setRowCount(0)
        if code != 0 or not out:
            self.logger.error(f"Drive health check failed: {err or out}")
            QMessageBox.warning(self, "Drive Health", "Could not read S.M.A.R.T. data.")
            return
        lines = [l for l in out.splitlines() if l.strip()]
        rows = []
        import csv
        for line in lines[1:]:
            parts = [p.strip() for p in next(csv.reader([line]), [])]
            if len(parts) >= 4:
                rows.append(parts)
        self.drive_table.setRowCount(len(rows))
        for r, parts in enumerate(rows):
            name, health, temp, wear = parts[0], parts[1], parts[2], parts[3]
            self.drive_table.setItem(r, 0, QTableWidgetItem(name))
            health_item = QTableWidgetItem(health)
            if health.lower() == "healthy":
                health_item.setForeground(QColor("#4ADE80"))
            else:
                health_item.setForeground(QColor("#F87171"))
            self.drive_table.setItem(r, 1, health_item)
            self.drive_table.setItem(r, 2, QTableWidgetItem(f"{temp}°C" if temp else "—"))
            self.drive_table.setItem(r, 3, QTableWidgetItem(f"{wear}% used" if wear else "—"))
        self.logger.success(f"Drive health checked: {len(rows)} drives.")

    def refresh(self):
        vm = psutil.virtual_memory()
        cpu = psutil.cpu_count(logical=True)
        path = "C:\\" if IS_WINDOWS else "/"
        try:
            du = psutil.disk_usage(path)
            disk_line = f"{human_bytes(du.used)} / {human_bytes(du.total)} (free {human_bytes(du.free)})"
        except Exception:
            disk_line = "N/A"

        info = [
            f"OS: {platform.platform()}",
            f"Machine: {platform.machine()}",
            f"Python: {platform.python_version()}",
            f"CPU threads: {cpu}",
            f"RAM: {human_bytes(vm.total)} (free {human_bytes(vm.available)})",
            f"Disk: {disk_line}",
            f"Boot time: {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(psutil.boot_time()))}",
            f"Admin: {'YES' if is_admin() else 'NO'}" if IS_WINDOWS else "Admin: N/A",
        ]
        self.label.setText("<br>".join(info))
        self.logger.log("System info refreshed.")


# -------------------------
# Privacy & Telemetry
# -------------------------

@dataclass
class PrivacyToggle:
    key: str
    title: str
    desc: str
    kind: str  # "dword" | "string" | "service"
    root: Optional[int] = None
    path: str = ""
    name: str = ""
    on_value: object = 1
    off_value: object = 0
    service_name: str = ""
    requires_admin: bool = True
    scope: str = "System"


TELEMETRY_TOGGLES: List[PrivacyToggle] = []
if IS_WINDOWS:
    TELEMETRY_TOGGLES = [
        PrivacyToggle(
            "adv_id", "Advertising ID",
            "Allows apps to use your Advertising ID for personalized ads.",
            "dword", winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\AdvertisingInfo", "Enabled",
            1, 0, requires_admin=False, scope="User",
        ),
        PrivacyToggle(
            "tailored", "Tailored Experiences",
            "Uses diagnostic data for personalized content/suggestions.",
            "dword", winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Privacy", "TailoredExperiencesWithDiagnosticDataEnabled",
            1, 0, requires_admin=False, scope="User",
        ),
        PrivacyToggle(
            "start_track", "Start/Search App Launch Tracking",
            "Tracks app launches to improve Start/Search suggestions.",
            "dword", winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Explorer\Advanced", "Start_TrackProgs",
            1, 0, requires_admin=False, scope="User",
        ),
        PrivacyToggle(
            "suggested_content", "Suggested Content in Settings",
            "Suggested content/ads inside the Windows Settings app.",
            "dword", winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\ContentDeliveryManager", "SubscribedContent-338389Enabled",
            1, 0, requires_admin=False, scope="User",
        ),
        PrivacyToggle(
            "speech", "Online Speech Recognition",
            "Sends voice data to Microsoft to improve speech recognition.",
            "dword", winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Speech_OneCore\Settings\OnlineSpeechPrivacy", "HasAccepted",
            1, 0, requires_admin=False, scope="User",
        ),
        PrivacyToggle(
            "location", "Location Access (this user)",
            "App access to your location.",
            "string", winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\CapabilityAccessManager\ConsentStore\location", "Value",
            "Allow", "Deny", requires_admin=False, scope="User",
        ),
        PrivacyToggle(
            "activity_feed", "Activity History (Timeline)",
            "Collects and sends activity history (Timeline) to Microsoft.",
            "dword", winreg.HKEY_LOCAL_MACHINE,
            r"SOFTWARE\Policies\Microsoft\Windows\System", "EnableActivityFeed",
            1, 0, requires_admin=True, scope="System",
        ),
        PrivacyToggle(
            "diagtrack_svc", "Connected User Experiences & Telemetry",
            "The main Windows telemetry service (DiagTrack).",
            "service", service_name="DiagTrack", requires_admin=True, scope="System",
        ),
        PrivacyToggle(
            "dmwappush_svc", "WAP Push Message Routing",
            "Related to push messages/telemetry (dmwappushsvc).",
            "service", service_name="dmwappushsvc", requires_admin=True, scope="System",
        ),
        PrivacyToggle(
            "wersvc", "Windows Error Reporting",
            "Sends error reports to Microsoft (WerSvc).",
            "service", service_name="WerSvc", requires_admin=True, scope="System",
        ),
        PrivacyToggle(
            "retaildemo_svc", "Retail Demo Service",
            "Retail demo service — rarely useful on a personal computer.",
            "service", service_name="RetailDemo", requires_admin=True, scope="System",
        ),
    ]


# Registry subtrees a custom toggle is allowed to target. Keeps a planted/
# tampered tweaks_privacy_custom.json from turning a "simple dword toggle"
# into a way to silently write to unrelated, security-sensitive keys (e.g.
# Run/Winlogon) with the Admin rights this app already has.
ALLOWED_CUSTOM_TOGGLE_PATH_PREFIXES = (
    r"Software\Microsoft\Windows\CurrentVersion\AdvertisingInfo",
    r"Software\Microsoft\Windows\CurrentVersion\Privacy",
    r"Software\Microsoft\Windows\CurrentVersion\ContentDeliveryManager",
    r"Software\Microsoft\Windows\CurrentVersion\CapabilityAccessManager\ConsentStore",
    r"Software\Microsoft\Windows\CurrentVersion\Explorer\Advanced",
    r"Software\Microsoft\Speech_OneCore\Settings",
    r"SOFTWARE\Policies\Microsoft\Windows",
)


def _is_allowed_custom_toggle_path(path: str) -> bool:
    normalized = (path or "").strip("\\").lower()
    return any(
        normalized == prefix.lower() or normalized.startswith(prefix.lower() + "\\")
        for prefix in ALLOWED_CUSTOM_TOGGLE_PATH_PREFIXES
    )


def _load_custom_privacy_toggles() -> List["PrivacyToggle"]:
    """Loads additional privacy toggles from an optional external JSON file
    (tweaks_privacy_custom.json, next to the app), so new simple dword-based
    toggles can be added without touching the Python source — a lightweight,
    low-risk step toward 'modular tweaks' without rewriting the whole app's
    architecture. The built-in hardcoded list above always stays as the
    safe, tested default; this only ever ADDS to it."""
    custom: List[PrivacyToggle] = []
    if not IS_WINDOWS:
        return custom
    path = find_resource("tweaks_privacy_custom.json")
    if not path:
        return custom
    try:
        import json
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        root_map = {"HKCU": winreg.HKEY_CURRENT_USER, "HKLM": winreg.HKEY_LOCAL_MACHINE}
        for entry in data.get("toggles", []):
            root = root_map.get(entry.get("root", "HKCU"))
            if root is None or "key" not in entry or "path" not in entry or "name" not in entry:
                continue
            if not _is_allowed_custom_toggle_path(entry["path"]):
                continue
            custom.append(PrivacyToggle(
                entry["key"], entry.get("title", entry["key"]), entry.get("desc", ""),
                "dword", root, entry["path"], entry["name"],
                entry.get("on_value", 1), entry.get("off_value", 0),
                requires_admin=entry.get("requires_admin", True),
                scope=entry.get("scope", "System"),
            ))
    except Exception:
        pass
    return custom


TELEMETRY_TOGGLES.extend(_load_custom_privacy_toggles())


class CircularGauge(QWidget):
    """A small circular progress gauge (0-100%), custom-painted with QPainter.
    Used for the Privacy Score — a single glanceable number is much more
    motivating than a checklist, and color shifts red→yellow→green as the
    score improves."""
    def __init__(self, size: int = 120, parent=None):
        super().__init__(parent)
        self._value = 0
        self._gauge_size = size
        self.setFixedSize(size, size)

    def setValue(self, value: int):
        self._value = max(0, min(100, value))
        self.update()

    def _color_for(self, value: int) -> QColor:
        if value < 40:
            return QColor("#F87171")   # red
        if value < 75:
            return QColor("#FBBF24")   # yellow
        return QColor("#4ADE80")       # green

    def paintEvent(self, event):
        from PySide6.QtGui import QPainter, QPen
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)

        side = min(self.width(), self.height())
        pen_width = max(8, side // 12)
        rect_margin = pen_width / 2 + 2
        rect = self.rect().adjusted(int(rect_margin), int(rect_margin), -int(rect_margin), -int(rect_margin))

        # background ring
        bg_pen = QPen(QColor("#1f2740"), pen_width, Qt.SolidLine, Qt.RoundCap)
        painter.setPen(bg_pen)
        painter.drawArc(rect, 0, 360 * 16)

        # progress arc (starts at 12 o'clock, clockwise)
        color = self._color_for(self._value)
        fg_pen = QPen(color, pen_width, Qt.SolidLine, Qt.RoundCap)
        painter.setPen(fg_pen)
        span = int(360 * 16 * (self._value / 100))
        painter.drawArc(rect, 90 * 16, -span)

        # center text
        painter.setPen(QColor("#F3F5FA"))
        font = painter.font()
        font.setPointSize(max(12, side // 7))
        font.setBold(True)
        painter.setFont(font)
        painter.drawText(self.rect(), Qt.AlignCenter, f"{self._value}%")


class PagePrivacy(QWidget):
    """Privacy & Telemetry center: toggles + related service control."""
    def __init__(self, logger: Logger):
        super().__init__()
        self.logger = logger
        self.checkboxes = {}
        self.status_labels = {}

        layout = QVBoxLayout(self)
        top_row = QHBoxLayout()
        header = QLabel(
            "<p>Control telemetry, advertising ID, location, Timeline, and related Windows services. "
            "<b>System</b>-level settings require Admin privileges.</p>"
        )
        header.setWordWrap(True)
        top_row.addWidget(header, 1)

        gauge_col = QVBoxLayout()
        self.privacy_gauge = CircularGauge(size=104)
        gauge_label = QLabel("Privacy Score")
        gauge_label.setObjectName("appSubtitle")
        gauge_label.setAlignment(Qt.AlignCenter)
        gauge_col.addWidget(self.privacy_gauge, 0, Qt.AlignCenter)
        gauge_col.addWidget(gauge_label)
        top_row.addLayout(gauge_col)
        layout.addLayout(top_row)

        level_box = QGroupBox("Telemetry Level  (Policy: AllowTelemetry)")
        level_layout = QHBoxLayout(level_box)
        self.combo_level = QComboBox()
        self.combo_level.addItems([
            "0 · Security (Enterprise only)",
            "1 · Basic",
            "2 · Enhanced",
            "3 · Full",
        ])
        self.btn_level_apply = QPushButton("Apply Level (Admin)")
        level_layout.addWidget(QLabel("Level:"))
        level_layout.addWidget(self.combo_level, 1)
        level_layout.addWidget(self.btn_level_apply)
        layout.addWidget(level_box)

        self.search_box = QLineEdit()
        self.search_box.setPlaceholderText("🔎  Search settings… (e.g. 'ads', 'location', 'Timeline')")
        layout.addWidget(self.search_box)

        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.NoFrame)
        cards_widget = QWidget()
        self.cards_layout = QVBoxLayout(cards_widget)
        self.cards_layout.setSpacing(10)
        self.scroll.setWidget(cards_widget)
        layout.addWidget(self.scroll, 1)

        self.toggle_cards: Dict[str, QGroupBox] = {}
        for t in TELEMETRY_TOGGLES:
            card = self.build_card(t)
            self.toggle_cards[t.key] = card
            self.cards_layout.addWidget(card)
        self.cards_layout.addStretch(1)
        self.search_box.textChanged.connect(self.filter_cards)

        btns = QHBoxLayout()
        btns.setSpacing(10)
        self.btn_refresh = QPushButton("↻  Refresh Status")
        self.btn_refresh.setMaximumWidth(170)
        self.btn_apply = QPushButton("✔  Apply Selected")
        self.btn_apply.setMaximumWidth(170)
        self.combo_preset = QComboBox()
        self.combo_preset.addItems([
            "🔒  Maximum Privacy",
            "🎮  Gaming Mode (balanced)",
            "🧹  Standard Debloat",
        ])
        self.btn_preset = QPushButton("Apply Profile (Admin)")
        btns.addWidget(self.btn_refresh)
        btns.addWidget(self.btn_apply)
        btns.addSpacing(8)
        btns.addWidget(self.combo_preset, 1)
        btns.addWidget(self.btn_preset)
        layout.addLayout(btns)

        shutup10_row = QHBoxLayout()
        self.btn_shutup10 = QPushButton("🧰  Open O&&O ShutUp10")
        self.btn_shutup10.setObjectName("btnGhost")
        self.btn_shutup10.setMaximumWidth(240)
        shutup10_row.addWidget(self.btn_shutup10)
        shutup10_row.addStretch(1)
        layout.addLayout(shutup10_row)

        io_row = QHBoxLayout()
        self.btn_export = QPushButton("💾  Export Profile (.winforge)")
        self.btn_import = QPushButton("📂  Import Profile (.winforge)")
        io_row.addWidget(self.btn_export)
        io_row.addWidget(self.btn_import)
        io_row.addStretch(1)
        layout.addLayout(io_row)

        self.btn_refresh.clicked.connect(self.refresh_all)
        self.btn_apply.clicked.connect(self.apply_all_checked_states)
        self.btn_level_apply.clicked.connect(self.apply_level)
        self.btn_export.clicked.connect(self.export_profile)
        self.btn_import.clicked.connect(self.import_profile)
        self.btn_preset.clicked.connect(self.apply_selected_preset)
        self.btn_shutup10.clicked.connect(self.launch_shutup10)

        if not IS_WINDOWS:
            self.setEnabled(False)
            self.logger.log("Privacy: Windows only.")
        else:
            self.refresh_all()

    PRIVACY_ICONS = {
        "adv_id": "🎯", "tailored": "🧬", "start_track": "📈",
        "suggested_content": "💡", "speech": "🎙️", "location": "📍",
        "activity_feed": "🕒", "diagtrack_svc": "📡", "dmwappush_svc": "📶",
        "wersvc": "🐞", "retaildemo_svc": "🛍️",
    }

    def build_card(self, t: PrivacyToggle) -> QGroupBox:
        box = QGroupBox()
        box.setObjectName("privacyCard")
        row = QHBoxLayout(box)

        icon_lbl = QLabel(self.PRIVACY_ICONS.get(t.key, "🔔"))
        icon_lbl.setFixedSize(38, 38)
        icon_lbl.setAlignment(Qt.AlignCenter)
        icon_lbl.setStyleSheet(
            "background-color: rgba(124,92,252,0.16); color:#B9A9FF;"
            "border-radius:10px; font-size:14pt;"
        )
        row.addWidget(icon_lbl)

        cb = QCheckBox()
        self.checkboxes[t.key] = cb

        text_col = QVBoxLayout()
        text_col.setSpacing(2)
        tag = f"<span style='color:#8892A6;'>[{t.scope}{' · Admin' if t.requires_admin else ''}]</span>"
        title_lbl = QLabel(f"<b>{t.title}</b>&nbsp;&nbsp;{tag}")
        desc_lbl = QLabel(t.desc)
        desc_lbl.setWordWrap(True)
        desc_lbl.setStyleSheet("color:#9AA5C0;")
        text_col.addWidget(title_lbl)
        text_col.addWidget(desc_lbl)

        status_lbl = QLabel("…")
        status_lbl.setMinimumWidth(90)
        status_lbl.setAlignment(Qt.AlignCenter)
        status_lbl.setObjectName("statusPill")
        self.status_labels[t.key] = status_lbl

        row.addWidget(cb)
        row.addLayout(text_col, 1)
        row.addWidget(status_lbl)
        apply_shadow(box, blur=18, dy=4, alpha=60, color="#7C5CFC")
        return box

    def read_current_state(self, t: PrivacyToggle) -> bool:
        """True == feature/telemetry currently ON / allowed."""
        if t.kind == "dword":
            off = t.off_value if isinstance(t.off_value, int) else 0
            val = reg_get_dword(t.root, t.path, t.name, default=off)
            return val == t.on_value
        if t.kind == "string":
            val = reg_get_string(t.root, t.path, t.name, default=str(t.off_value))
            return val == t.on_value
        if t.kind == "service":
            _, start_mode = get_service_state(t.service_name)
            return start_mode.upper() != "DISABLED"
        return False

    def refresh_all(self):
        if not IS_WINDOWS:
            return
        enabled_icon = find_resource("status_icons", "status_enabled.png")
        disabled_icon = find_resource("status_icons", "status_disabled.png")
        off_count = 0
        for t in TELEMETRY_TOGGLES:
            try:
                enabled = self.read_current_state(t)
                if not enabled:
                    off_count += 1
                cb = self.checkboxes[t.key]
                cb.blockSignals(True)
                cb.setChecked(enabled)
                cb.blockSignals(False)
                lbl = self.status_labels[t.key]
                icon_path = enabled_icon if enabled else disabled_icon
                text = "On" if enabled else "Off"
                if icon_path:
                    lbl.setText(f'<img src="{icon_path}" width="12" height="12"/> {text}')
                else:
                    lbl.setText(text)
                lbl.setStyleSheet(
                    f"background:{'#153229' if enabled else '#3a1c22'};"
                    f"color:{'#6EE7B7' if enabled else '#FF8686'};"
                    "border-radius:8px; padding:3px 8px; font-weight:600;"
                )
            except Exception as e:
                self.logger.log(f"Privacy refresh error ({t.key}): {e}")

        if TELEMETRY_TOGGLES:
            score = round(100 * off_count / len(TELEMETRY_TOGGLES))
            self.privacy_gauge.setValue(score)

        try:
            level = reg_get_dword(
                winreg.HKEY_LOCAL_MACHINE,
                r"SOFTWARE\Policies\Microsoft\Windows\DataCollection",
                "AllowTelemetry", default=-1,
            )
            if level in (0, 1, 2, 3):
                self.combo_level.setCurrentIndex(level)
        except Exception:
            pass

        self.logger.log("Privacy: state refreshed.")

    def ensure_admin_or_offer(self) -> bool:
        if is_admin():
            return True
        r = QMessageBox.question(
            self, "Admin required",
            "This action requires Admin privileges. Restart the app as Admin?",
            QMessageBox.Yes | QMessageBox.No,
        )
        if r == QMessageBox.Yes:
            self.logger.log("Elevation requested (Privacy).")
            elevate_self()
        return False

    def apply_single(self, t: PrivacyToggle, enable: bool):
        if t.requires_admin and not is_admin():
            raise PermissionError("Admin required")
        if t.kind == "dword":
            reg_set_dword(t.root, t.path, t.name, t.on_value if enable else t.off_value)
        elif t.kind == "string":
            reg_set_string(t.root, t.path, t.name, t.on_value if enable else t.off_value)
        elif t.kind == "service":
            code, out, err = set_service_enabled(t.service_name, enable)
            if code != 0:
                raise RuntimeError(err or out or f"Service command failed with code {code}")

    def apply_all_checked_states(self):
        if not IS_WINDOWS:
            return
        pending = [
            t for t in TELEMETRY_TOGGLES
            if self.checkboxes[t.key].isChecked() != self.read_current_state(t)
        ]
        if not pending:
            QMessageBox.information(self, "Privacy", "There are no pending changes.")
            return

        needs_admin = any(t.requires_admin for t in pending)
        if needs_admin and not is_admin():
            if not self.ensure_admin_or_offer():
                return

        summary_lines = "\n".join(
            f"• {t.title}: {'ON' if self.read_current_state(t) else 'OFF'} → "
            f"{'ON' if self.checkboxes[t.key].isChecked() else 'OFF'}"
            for t in pending
        )
        confirm = QMessageBox.question(
            self, "Confirm Changes",
            f"{len(pending)} change(s) will be applied:\n\n{summary_lines}\n\nContinue?",
            QMessageBox.Yes | QMessageBox.No,
        )
        if confirm != QMessageBox.Yes:
            return

        previous_states = {t.key: self.read_current_state(t) for t in pending}

        def undo():
            for t in pending:
                try:
                    self.apply_single(t, previous_states[t.key])
                except Exception:
                    pass
            self.refresh_all()

        applied, failed = 0, 0
        for t in pending:
            desired = self.checkboxes[t.key].isChecked()
            try:
                self.apply_single(t, desired)
                applied += 1
                self.logger.log(f"Privacy: {t.title} -> {'ON' if desired else 'OFF'}")
            except Exception as e:
                failed += 1
                self.logger.log(f"Privacy apply error ({t.key}): {e}")

        if applied:
            record_history(f"Privacy: {applied} telemetry setting(s)", undo)
        self.refresh_all()
        QMessageBox.information(self, "Privacy", f"Done.\nApplied: {applied}\nFailed: {failed}")

    def apply_level(self):
        if not self.ensure_admin_or_offer():
            return
        level = self.combo_level.currentIndex()
        try:
            reg_set_dword(
                winreg.HKEY_LOCAL_MACHINE,
                r"SOFTWARE\Policies\Microsoft\Windows\DataCollection",
                "AllowTelemetry", level,
            )
            self.logger.log(f"Privacy: Telemetry level set to {level}")
            QMessageBox.information(self, "Telemetry Level", f"Telemetry level set to: {level}")
        except Exception as e:
            QMessageBox.critical(self, "Error", str(e))
            self.logger.log(f"Telemetry level error: {e}")

    def filter_cards(self, query: str):
        q = (query or "").lower().strip()
        for t in TELEMETRY_TOGGLES:
            card = self.toggle_cards.get(t.key)
            if not card:
                continue
            match = (not q) or (q in t.title.lower()) or (q in t.desc.lower())
            card.setVisible(match)

    def launch_shutup10(self):
        if not IS_WINDOWS:
            QMessageBox.information(self, "O&O ShutUp10", "Windows only.")
            return

        common_paths = [
            os.path.join(os.environ.get("USERPROFILE", ""), "Downloads", "ShutUp10.exe"),
            os.path.join(os.environ.get("USERPROFILE", ""), "Downloads", "OOSU10.exe"),
            os.path.join(os.environ.get("USERPROFILE", ""), "Desktop", "ShutUp10.exe"),
            os.path.join(os.environ.get("USERPROFILE", ""), "Desktop", "OOSU10.exe"),
        ]
        found = next((p for p in common_paths if os.path.isfile(p)), None)

        path = found
        if not path:
            path, _ = QFileDialog.getOpenFileName(
                self, "Select ShutUp10.exe (O&O ShutUp10 / OOSU10)", "", "Executables (*.exe)"
            )
        if not path:
            return

        ok, err = shell_open(path)
        self.logger.log(f"Launch O&O ShutUp10: {'ok' if ok else 'FAILED — ' + err}")
        if not ok:
            QMessageBox.warning(self, "O&O ShutUp10", f"Could not open it.\n{err}")

    def export_profile(self):
        path, _ = QFileDialog.getSaveFileName(
            self, "Export Profile", "privacy_profile.winforge", "WinForge Profile (*.winforge)"
        )
        if not path:
            return
        data = {
            "type": "winforge_privacy_profile",
            "version": 1,
            "telemetry_level": self.combo_level.currentIndex(),
            "toggles": {key: cb.isChecked() for key, cb in self.checkboxes.items()},
        }
        try:
            import json
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
            self.logger.success(f"Profile exported: {path}")
            QMessageBox.information(self, "Export", "The profile was saved successfully.")
        except Exception as e:
            self.logger.error(f"Export failed: {e}")
            QMessageBox.warning(self, "Export", f"Save failed:\n{e}")

    def import_profile(self):
        path, _ = QFileDialog.getOpenFileName(self, "Import Profile", "", "WinForge Profile (*.winforge)")
        if not path:
            return
        try:
            import json
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if data.get("type") != "winforge_privacy_profile":
                QMessageBox.warning(self, "Import", "Invalid profile file.")
                return
        except Exception as e:
            QMessageBox.warning(self, "Import", f"Read failed:\n{e}")
            return

        toggles = data.get("toggles", {})
        applied = 0
        for key, checked in toggles.items():
            cb = self.checkboxes.get(key)
            if cb is not None:
                cb.setChecked(bool(checked))
                applied += 1
        level = data.get("telemetry_level")
        if isinstance(level, int) and 0 <= level < self.combo_level.count():
            self.combo_level.setCurrentIndex(level)

        self.logger.log(f"Profile imported: {applied} toggles loaded from {path}")
        confirm = QMessageBox.question(
            self, "Import",
            f"Loaded {applied} settings from the profile. Apply them now?",
            QMessageBox.Yes | QMessageBox.No,
        )
        if confirm == QMessageBox.Yes:
            self.apply_all_checked_states()

    def apply_selected_preset(self):
        idx = self.combo_preset.currentIndex()
        if idx == 0:
            self.apply_max_privacy_preset()
        elif idx == 1:
            self.apply_gaming_mode_preset()
        else:
            self.apply_standard_debloat_preset()

    def apply_max_privacy_preset(self):
        if not self.ensure_admin_or_offer():
            return
        ok = QMessageBox.question(
            self, "Maximum Privacy",
            "All the telemetry/privacy settings below will be turned off "
            "and Telemetry Level will be set to Basic. Continue?",
            QMessageBox.Yes | QMessageBox.No,
        )
        if ok != QMessageBox.Yes:
            return
        for t in TELEMETRY_TOGGLES:
            self.checkboxes[t.key].setChecked(False)
        try:
            reg_set_dword(
                winreg.HKEY_LOCAL_MACHINE,
                r"SOFTWARE\Policies\Microsoft\Windows\DataCollection",
                "AllowTelemetry", 1,
            )
            self.combo_level.setCurrentIndex(1)
        except Exception as e:
            self.logger.log(f"Preset telemetry level error: {e}")
        self.apply_all_checked_states()

    def apply_gaming_mode_preset(self):
        """Balanced profile: keep telemetry off for privacy, but leave
        things like location/speech recognition alone in case games or
        voice chat tools need them — less aggressive than Max Privacy."""
        if not self.ensure_admin_or_offer():
            return
        ok = QMessageBox.question(
            self, "Gaming Mode",
            "Turns off telemetry/advertising ID, while leaving on anything games/apps "
            "might need (location, speech recognition). Continue?",
            QMessageBox.Yes | QMessageBox.No,
        )
        if ok != QMessageBox.Yes:
            return
        keep_on = {"location", "speech"}
        for t in TELEMETRY_TOGGLES:
            if t.key not in keep_on:
                self.checkboxes[t.key].setChecked(False)
        self.apply_all_checked_states()

    def apply_standard_debloat_preset(self):
        """Middle ground: turn off the most invasive/advertising-flavored
        toggles, leave more borderline ones (like Timeline/App tracking) as
        the user currently has them."""
        if not self.ensure_admin_or_offer():
            return
        ok = QMessageBox.question(
            self, "Standard Debloat",
            "Turns off advertising ID, tailored experiences, and suggested "
            "content — without touching anything else. Continue?",
            QMessageBox.Yes | QMessageBox.No,
        )
        if ok != QMessageBox.Yes:
            return
        target_keys = {"adv_id", "tailored", "suggested_content"}
        for t in TELEMETRY_TOGGLES:
            if t.key in target_keys:
                self.checkboxes[t.key].setChecked(False)
        self.apply_all_checked_states()


# -------------------------
# Visual theme (modern dark UI)
# -------------------------

APP_STYLESHEET = """
* {
    outline: none;
}
QMainWindow, QWidget {
    background-color: #090d16;
    color: #E7EAF3;
    font-family: 'Segoe UI Variable', 'Segoe UI', 'Ubuntu', sans-serif;
    font-size: 10.3pt;
}
QWidget#rootPanel {
    background-color: #0b0f19;
}
QLabel {
    background: transparent;
}
QLabel h2, QLabel h3 {
    color: #F5F6FB;
}
QLabel#appTitle {
    font-size: 16pt;
    font-weight: 800;
    color: #FFFFFF;
    letter-spacing: 0.3px;
}
QLabel#appSubtitle {
    color: #8794B3;
    font-size: 8.7pt;
}
QLabel#brandSubtitle {
    color: #BFC9E5;
    font-size: 8.8pt;
}
QLabel#dangerNote {
    color: #FFB199;
    background-color: rgba(255, 90, 90, 0.12);
    border: 1px solid rgba(255, 90, 90, 0.35);
    border-radius: 10px;
    padding: 10px 14px;
    font-weight: 600;
}
QLabel#logoBadge {
    background-color: #7C5CFC;
    border-radius: 13px;
    font-size: 18pt;
    color: white;
}
QLabel#heroTitle {
    font-size: 19pt;
    font-weight: 800;
    color: #FFFFFF;
}
QLabel#heroSubtitle {
    color: #8892A6;
    font-size: 9.5pt;
}

/* ---------- Sidebar ---------- */
QWidget#sidebarContainer {
    background-color: #10151f;
    border-right: 1px solid #1b2233;
}
QListWidget#sidebar {
    background-color: transparent;
    border: none;
    padding: 6px 10px;
}
QListWidget#sidebar::item {
    padding: 10px 12px;
    margin: 2px 2px;
    border-radius: 11px;
    color: #9AA5C0;
    font-weight: 500;
}
QListWidget#sidebar::item:hover {
    background-color: #171f30;
    color: #F3F5FA;
}
QListWidget#sidebar::item:selected {
    background-color: qlineargradient(
        x1:0, y1:0, x2:1, y2:0,
        stop:0 rgba(124,92,252,0.30), stop:1 rgba(78,161,255,0.12)
    );
    color: #FFFFFF;
    border-left: 3px solid #8F73FF;
    font-weight: 700;
}

/* ---------- Buttons ---------- */
QPushButton {
    background-color: #7C5CFC;
    color: #FFFFFF;
    border: none;
    border-radius: 10px;
    padding: 9px 18px;
    font-weight: 600;
}
QPushButton:hover {
    background-color: #8E71FF;
}
QPushButton:pressed {
    background-color: #6748DB;
}
QPushButton:disabled {
    background-color: #1c2333;
    color: #545C73;
}
QPushButton#btnDanger {
    background-color: #E5484D;
}
QPushButton#btnDanger:hover { background-color: #F2565B; }
QPushButton#btnDanger:pressed { background-color: #C93A3E; }

QPushButton#btnSuccess {
    background-color: #12B76A;
}
QPushButton#btnSuccess:hover { background-color: #1DCB79; }
QPushButton#btnSuccess:pressed { background-color: #0E9C5A; }

QPushButton#btnWarn {
    background-color: #F59E0B;
    color: #241900;
}
QPushButton#btnWarn:hover { background-color: #FFB020; }
QPushButton#btnWarn:pressed { background-color: #D9880A; }

QPushButton#btnGhost {
    background-color: #161c2b;
    color: #C9D1E8;
    border: 1px solid #262f45;
}
QPushButton#btnGhost:hover {
    background-color: #1d2538;
    border: 1px solid #3a4560;
}

/* ---------- Premium controls ---------- */
QPushButton#btnPrimary {
    background-color: #7C5CFC;
    border: 1px solid #9B88FF;
    border-radius: 11px;
    padding: 10px 18px;
    font-weight: 800;
}
QPushButton#btnPrimary:hover { background-color: #8E71FF; }
QPushButton#btnSecondary {
    background-color: #171f30;
    color: #D8DDF0;
    border: 1px solid #2B3652;
}
QPushButton#btnSecondary:hover {
    background-color: #202A40;
    border: 1px solid #435171;
}
QPushButton#btnDangerLarge {
    background-color: #3A151D;
    color: #FFB4B8;
    border: 1px solid #9C333D;
    border-radius: 13px;
    padding: 12px 16px;
    font-weight: 800;
    min-height: 46px;
}
QPushButton#btnDangerLarge:hover {
    background-color: #4A1922;
    border-color: #E5484D;
}
QLineEdit#compactSearch {
    min-height: 32px;
    padding: 6px 10px;
    border-radius: 9px;
    background-color: #111724;
}
QFrame#healthStrip {
    background: qlineargradient(x1:0,y1:0,x2:1,y2:0, stop:0 #101827, stop:0.55 #131A2A, stop:1 #101827);
    border: 1px solid #202B46;
    border-radius: 13px;
}
QLabel#healthMetric {
    color: #8E9AB6;
    font-weight: 600;
}
QLabel#healthValue {
    color: #6EE7B7;
    font-weight: 800;
}
QFrame#pageIntroCard {
    background: qlineargradient(x1:0,y1:0,x2:1,y2:0, stop:0 #131A2A, stop:1 #111725);
    border: 1px solid #252F49;
    border-radius: 14px;
}
QLabel#sectionTitle {
    color: #F5F6FB;
    font-size: 12.5pt;
    font-weight: 800;
}
QLabel#sectionSubtitle, QLabel#microHint {
    color: #8E9AB6;
    font-size: 9pt;
}
QLabel#pendingBadge {
    background: #1A2235;
    color: #9AA5C0;
    border: 1px solid #303A57;
    border-radius: 10px;
    padding: 6px 10px;
    font-weight: 800;
}
QLabel#pendingBadge[pending="true"] {
    background: rgba(124,92,252,0.18);
    color: #C7B9FF;
    border: 1px solid rgba(124,92,252,0.45);
}
QGroupBox#featureCard {
    background: #0F1625;
    border: 1px solid #202B46;
    border-radius: 15px;
    margin-top: 14px;
    padding: 14px;
}
QGroupBox#featureCard::title {
    color: #C8BFFF;
}
QGroupBox#dangerCard {
    background: #140E15;
    border: 1px solid #4B202B;
    border-radius: 15px;
    margin-top: 14px;
    padding: 14px;
}
QGroupBox#dangerCard::title {
    color: #FF8C96;
}
QFrame#actionCard {
    background: #111827;
    border: 1px solid #222E49;
    border-radius: 13px;
}
QFrame#actionCard:hover {
    background: #141D2D;
    border: 1px solid #394766;
}
QLabel#actionTitle {
    color: #F1F4FB;
    font-weight: 800;
}
QLabel#actionDesc {
    color: #8D99B3;
    font-size: 8.6pt;
}
QLabel#actionArrow {
    color: #6E7C99;
    font-size: 15pt;
}

/* ---------- Inputs ---------- */
QLineEdit, QComboBox, QTextEdit {
    background-color: #131826;
    border: 1px solid #232c42;
    border-radius: 10px;
    padding: 8px 11px;
    selection-background-color: #7C5CFC;
}
QLineEdit:focus, QComboBox:focus, QTextEdit:focus {
    border: 1px solid #7C5CFC;
}
QComboBox::drop-down {
    border: none;
    width: 26px;
}
QComboBox QAbstractItemView {
    background-color: #161c2b;
    border: 1px solid #262f45;
    selection-background-color: #7C5CFC;
    outline: none;
    padding: 4px;
}

/* ---------- Panels ---------- */
QGroupBox {
    background-color: #121826;
    border: 1px solid #1f2740;
    border-radius: 16px;
    margin-top: 18px;
    padding: 16px;
    font-weight: 700;
}
QGroupBox::title {
    subcontrol-origin: margin;
    left: 16px;
    top: -4px;
    padding: 0 8px;
    color: #B9A9FF;
}
QGroupBox#privacyCard {
    margin-top: 4px;
    padding: 12px 14px;
    border: 1px solid #1f2740;
}
QGroupBox#privacyCard:hover {
    border: 1px solid #4a3f8f;
}
QFrame#statCard {
    background-color: #121826;
    border: 1px solid #1f2740;
    border-radius: 16px;
}
QFrame#statCard:hover {
    border: 1px solid #4a3f8f;
}

/* ---------- Tables & lists ---------- */
QTableWidget {
    background-color: #121826;
    alternate-background-color: #161c2b;
    gridline-color: #1B2438;
    border: 1px solid #1f2740;
    border-radius: 13px;
    selection-background-color: rgba(124, 92, 252, 0.28);
    padding: 2px;
}
QTableWidget::item {
    padding: 4px 6px;
}
QHeaderView::section {
    background-color: #161c2b;
    color: #8892A6;
    padding: 9px;
    border: none;
    border-bottom: 1px solid #262f45;
    font-weight: 700;
    text-transform: uppercase;
    font-size: 8.5pt;
}
QListWidget {
    background-color: #121826;
    border: 1px solid #1f2740;
    border-radius: 12px;
    padding: 4px;
}
QListWidget::item {
    padding: 7px 8px;
    border-radius: 8px;
}
QListWidget::item:hover {
    background-color: #171f30;
}
QListWidget::item:selected {
    background-color: rgba(124, 92, 252, 0.25);
    color: #FFFFFF;
}

/* ---------- Toggle-style checkboxes ---------- */
QCheckBox {
    spacing: 10px;
}
QCheckBox::indicator {
    width: 42px;
    height: 24px;
    border-radius: 12px;
    border: 1px solid #2f3852;
    background-color: #1a2135;
}
QCheckBox::indicator:hover {
    border: 1px solid #4a5578;
}
QCheckBox::indicator:checked {
    background-color: qlineargradient(
        x1:0, y1:0, x2:1, y2:0,
        stop:0 #7C5CFC, stop:1 #4EA1FF
    );
    border: 1px solid transparent;
}

/* ---------- Radio buttons (Windows Update mode picker) ---------- */
QRadioButton {
    spacing: 10px;
}
QRadioButton::indicator {
    width: 20px;
    height: 20px;
    border-radius: 10px;
    border: 2px solid #3a4460;
    background-color: #1a2135;
}
QRadioButton::indicator:hover {
    border: 2px solid #6a76a0;
}
QRadioButton::indicator:checked {
    border: 2px solid #7C5CFC;
    background-color: qradialgradient(
        cx:0.5, cy:0.5, radius:0.5, fx:0.5, fy:0.5,
        stop:0 #7C5CFC, stop:0.5 #7C5CFC, stop:0.6 #1a2135, stop:1 #1a2135
    );
}

/* ---------- Scroll areas ---------- */
QScrollArea {
    border: none;
    background: transparent;
}
QScrollBar:vertical {
    background: transparent;
    width: 11px;
    margin: 0;
}
QScrollBar::handle:vertical {
    background: #2A3350;
    min-height: 34px;
    border-radius: 5px;
}
QScrollBar::handle:vertical:hover {
    background: #3f4c78;
}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {
    height: 0;
}
QScrollBar:horizontal {
    background: transparent;
    height: 11px;
}
QScrollBar::handle:horizontal {
    background: #2A3350;
    min-width: 34px;
    border-radius: 5px;
}
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {
    width: 0;
}

/* ---------- Log console ---------- */
QTextEdit#logBox {
    background-color: #080b12;
    border: 1px solid #202A42;
    border-radius: 13px;
    font-family: 'Cascadia Code', Consolas, monospace;
    font-size: 9pt;
    color: #8FD1FF;
    padding: 8px;
}

/* ---------- Admin badge ---------- */
QLabel#adminBadgeOn {
    background-color: rgba(18,183,106,0.15);
    color: #6EE7B7;
    font-weight: 700;
    border-radius: 13px;
    padding: 6px 14px;
    border: 1px solid rgba(18,183,106,0.35);
}
QLabel#adminBadgeOff {
    background-color: rgba(245,158,11,0.15);
    color: #FFC078;
    font-weight: 700;
    border-radius: 13px;
    padding: 6px 14px;
    border: 1px solid rgba(245,158,11,0.35);
}

/* ---------- Tooltips ---------- */
QToolTip {
    background-color: #1a2135;
    color: #E7EAF3;
    border: 1px solid #2f3852;
    border-radius: 8px;
    padding: 6px 10px;
}

/* ---------- Message boxes ---------- */
QMessageBox {
    background-color: #121826;
}
"""

# Light Mode: generated by remapping the dark theme's background/text color
# tokens to light equivalents. Accent/status colors (purple, blue, green,
# red, yellow) are left untouched since they read fine on both themes and
# button text stays white against those same colored backgrounds either way.
_LIGHT_COLOR_MAP = {
    "#090d16": "#F4F6FB",
    "#0b0f19": "#FFFFFF",
    "#121826": "#FFFFFF",
    "#161c2b": "#EEF1F8",
    "#1a2135": "#E7ECF7",
    "#171f30": "#EDF0F9",
    "#1f2740": "#D9E0EF",
    "#262f45": "#C7D0E4",
    "#E7EAF3": "#1B2233",
    "#F3F5FA": "#10131C",
    "#F5F6FB": "#10131C",
    "#9AA5C0": "#5B6478",
    "#8892A6": "#6B7488",
}


def _build_light_stylesheet(dark_css: str) -> str:
    css = dark_css
    for dark, light in _LIGHT_COLOR_MAP.items():
        css = css.replace(dark, light)
    return css


LIGHT_STYLESHEET = _build_light_stylesheet(APP_STYLESHEET)


# -------------------------
# Main Window
# -------------------------

PAGE_ICONS = {
    "Dashboard": "📊",
    "Tweaks": "🛠️",
    "Actions": "⚡",
    "Maintenance": "🧹",
    "Network": "🌐",
    "Repair": "🔧",
    "Services": "⚙️",
    "Startup": "🚀",
    "Power": "🔋",
    "Windows Update": "🔄",
    "Privacy & Telemetry": "🛡️",
    "Apps": "📦",
    "USB Creator": "💽",
    "System Info": "ℹ️",
}

PAGE_SUBTITLES = {
    "Dashboard": "Live view of CPU, RAM, disk, and processes",
    "Tweaks": "Explorer, workflow, and Windows UI tweaks — with pending changes.",
    "Actions": "Quick system tools, diagnostics, and safe power actions.",
    "Maintenance": "Cleanup, DNS, and basic maintenance",
    "Network": "Network diagnostics and resets",
    "Repair": "SFC, DISM, CHKDSK, and Windows Update reset",
    "Services": "View and control Windows services",
    "Startup": "Startup entries — enable/disable",
    "Power": "Power plans",
    "Windows Update": "Automatic, security only, or fully disabled",
    "Privacy & Telemetry": "Telemetry, advertising ID, location, services",
    "Apps": "Install & uninstall applications",
    "USB Creator": "Create a bootable USB from an .iso file",
    "System Info": "General system information",
}


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("WinForge")
        self.setMinimumSize(1040, 680)
        self.resize(1360, 860)

        root = QWidget()
        root.setObjectName("rootPanel")
        self.setCentralWidget(root)

        main = QHBoxLayout(root)
        main.setContentsMargins(0, 0, 0, 0)
        main.setSpacing(0)

        # ---- Sidebar ----
        sidebar_wrap = QVBoxLayout()
        sidebar_wrap.setContentsMargins(0, 0, 0, 0)
        sidebar_wrap.setSpacing(0)

        brand_row = QHBoxLayout()
        brand_row.setContentsMargins(18, 20, 12, 6)
        brand_row.setSpacing(10)
        logo_badge = QLabel()
        logo_badge.setObjectName("logoBadge")
        logo_badge.setFixedSize(44, 44)
        logo_badge.setAlignment(Qt.AlignCenter)
        logo_source = APP_LOGO_PNG_PATH or APP_ICON_PATH
        logo_pix = QPixmap(logo_source) if logo_source and os.path.isfile(logo_source) else QPixmap()
        if not logo_pix.isNull():
            # devicePixelRatio 2x render, then set the ratio, so the badge
            # stays crisp on HiDPI displays too — not just downscaled once.
            target_px = 44 * 2
            scaled = logo_pix.scaled(target_px, target_px, Qt.KeepAspectRatio, Qt.SmoothTransformation)
            scaled.setDevicePixelRatio(2.0)
            logo_badge.setPixmap(scaled)
        else:
            logo_badge.setText("🧰")
        apply_shadow(logo_badge, blur=20, dy=4, alpha=140, color="#7C5CFC")
        brand_text = QVBoxLayout()
        brand_text.setSpacing(0)
        brand = QLabel("WinForge")
        brand.setObjectName("appTitle")
        brand_sub = QLabel("Windows utilities, refined")
        brand_sub.setObjectName("brandSubtitle")
        brand_text.addWidget(brand)
        brand_text.addWidget(brand_sub)
        brand_row.addWidget(logo_badge)
        brand_row.addLayout(brand_text, 1)

        self.sidebar = QListWidget()
        self.sidebar.setObjectName("sidebar")
        self.sidebar.setFixedWidth(264)
        self.sidebar.setSpacing(2)
        self.sidebar.setFrameShape(QFrame.NoFrame)

        self.stack = QWidget()
        self.stack_layout = QVBoxLayout(self.stack)
        self.stack_layout.setContentsMargins(26, 14, 26, 10)

        self.log = QTextEdit()
        self.log.setObjectName("logBox")
        self.log.setReadOnly(True)
        self.log.setFixedHeight(200)
        self.logger = Logger(self.log)

        # ---- Hero header (per-page title, updates on navigation) ----
        hero = QVBoxLayout()
        hero.setContentsMargins(24, 22, 24, 0)
        hero.setSpacing(2)
        hero_row = QHBoxLayout()
        self.hero_icon = QLabel("")
        self.hero_icon.setFixedSize(40, 40)
        self.hero_icon.setScaledContents(True)
        hero_row.addWidget(self.hero_icon)
        hero_row.addSpacing(4)
        self.hero_title = QLabel("")
        self.hero_title.setObjectName("heroTitle")
        hero_row.addWidget(self.hero_title, 1)

        self.badge_admin = QLabel()
        self.badge_admin.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.btn_admin = QPushButton("🛡️  Run as Admin")
        self.btn_admin.setObjectName("btnGhost")
        self.btn_admin.clicked.connect(self.run_as_admin_clicked)
        self.btn_theme = QPushButton("🌙  Dark")
        self.btn_theme.setObjectName("btnGhost")
        self.btn_theme.clicked.connect(self.toggle_theme)
        self._light_mode = False
        hero_row.addWidget(self.badge_admin)
        hero_row.addSpacing(8)
        hero_row.addWidget(self.btn_admin)
        hero_row.addSpacing(8)
        hero_row.addWidget(self.btn_theme)

        self.hero_subtitle = QLabel("")
        self.hero_subtitle.setObjectName("heroSubtitle")

        hero.addLayout(hero_row)
        hero.addWidget(self.hero_subtitle)

        self.pages = {
            "Dashboard": PageDashboard(self.logger),
            "Tweaks": PageTweaks(self.logger),
            "Actions": PageActions(self.logger),
            "Maintenance": PageMaintenance(self.logger),
            "Network": PageNetwork(self.logger),
            "Repair": PageRepair(self.logger),
            "Services": PageServices(self.logger),
            "Startup": PageStartup(self.logger),
            "Power": PagePower(self.logger),
            "Windows Update": PageWindowsUpdate(self.logger),
            "Privacy & Telemetry": PagePrivacy(self.logger),
            "Apps": PageApps(self.logger),
            "USB Creator": PageUsbCreator(self.logger),
            "System Info": PageSystemInfo(self.logger),
        }
        self.page_names = list(self.pages.keys())

        self.sidebar.setIconSize(QSize(26, 26))
        for name in self.page_names:
            icon_file = PAGE_ICON_FILES.get(name, "")
            icon_path = find_resource("page_icons", icon_file) if icon_file else ""
            if icon_path:
                item = QListWidgetItem(QIcon(icon_path), f"   {name}")
            else:
                icon = PAGE_ICONS.get(name, "•")
                item = QListWidgetItem(f"{icon}   {name}")
            item.setSizeHint(item.sizeHint().expandedTo(item.sizeHint()))
            self.sidebar.addItem(item)

        self.current_page = None
        self.sidebar.currentRowChanged.connect(self.show_page)
        self.sidebar.setCurrentRow(0)

        sidebar_wrap.addLayout(brand_row)
        sidebar_wrap.addSpacing(10)
        sidebar_wrap.addWidget(self.sidebar, 1)

        right = QVBoxLayout()
        right.setContentsMargins(0, 0, 0, 0)
        right.setSpacing(0)
        right.addLayout(hero)

        self.stack_scroll = QScrollArea()
        self.stack_scroll.setObjectName("stackScrollArea")
        self.stack_scroll.setWidgetResizable(True)
        self.stack_scroll.setFrameShape(QFrame.NoFrame)
        self.stack_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.stack_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.stack_scroll.setWidget(self.stack)
        right.addWidget(self.stack_scroll, 1)

        log_wrap = QVBoxLayout()
        log_wrap.setContentsMargins(26, 8, 26, 18)
        log_head = QHBoxLayout()
        log_label = QLabel("<b>📜 System Log</b>")
        log_head.addWidget(log_label)
        log_head.addStretch(1)
        self.btn_undo = QPushButton("  Undo Last Change")
        undo_icon_path = find_resource("status_icons", "undo_arrow.png")
        if undo_icon_path:
            self.btn_undo.setIcon(QIcon(undo_icon_path))
        else:
            self.btn_undo.setText("↩  Undo Last Change")
        self.btn_undo.setObjectName("btnWarn")
        self.btn_undo.clicked.connect(self.undo_last_change)
        log_head.addWidget(self.btn_undo)
        self.btn_history = QPushButton("🕐  History")
        self.btn_history.setObjectName("btnGhost")
        self.btn_history.clicked.connect(self.open_history_timeline)
        log_head.addWidget(self.btn_history)
        self.log_clear = QPushButton("Clear")
        self.log_clear.setObjectName("btnGhost")
        self.log_copy = QPushButton("Copy")
        self.log_copy.setObjectName("btnGhost")
        self.log_clear.clicked.connect(self.log.clear)
        self.log_copy.clicked.connect(lambda: QApplication.clipboard().setText(self.log.toPlainText()))
        log_head.addWidget(self.log_copy)
        log_head.addWidget(self.log_clear)
        log_wrap.addLayout(log_head)
        log_wrap.addWidget(self.log)
        right.addLayout(log_wrap)

        sidebar_container = QWidget()
        sidebar_container.setObjectName("sidebarContainer")
        sidebar_container.setLayout(sidebar_wrap)
        main.addWidget(sidebar_container)
        main.addLayout(right, 1)

        self.logger.log("App started.")
        self.logger.log("Security: no command runs via shell (no shell injection risk), "
                         "all deletions/formatting/uninstalls require explicit confirmation, and admin-only "
                         "actions check privileges first.")
        if not IS_WINDOWS:
            self.logger.log("Warning: This build targets Windows. Some features are disabled.")
        self.refresh_admin_badge()

        from PySide6.QtGui import QShortcut, QKeySequence
        QShortcut(QKeySequence("Ctrl+F"), self, activated=self.open_global_search)
        QShortcut(QKeySequence("Ctrl+S"), self, activated=self.apply_current_page)

    def open_global_search(self):
        dlg = QDialog(self)
        dlg.setWindowTitle("🔎  Search Settings")
        dlg.resize(560, 420)
        lay = QVBoxLayout(dlg)
        search_box = QLineEdit()
        search_box.setPlaceholderText("Type to search… (e.g. 'DNS', 'telemetry', 'hidden')")
        lay.addWidget(search_box)
        results_list = QListWidget()
        lay.addWidget(results_list, 1)
        hint = QLabel("Double-click or Enter to jump there.")
        hint.setObjectName("appSubtitle")
        lay.addWidget(hint)

        index = []
        for t in TELEMETRY_TOGGLES:
            index.append((f"[Privacy & Telemetry]  {t.title} — {t.desc}", "Privacy & Telemetry", t.key))
        tweaks_items = [
            ("Show hidden files", "Shows hidden files and folders."),
            ("Show file extensions", "Shows .exe, .png, .txt etc. in file names."),
            ("Show protected OS files", "Shows protected operating system files."),
            ("Show full path in title bar", "Shows the full path in the Explorer title bar."),
            ("Take Ownership context menu", "A new right-click option to take ownership of files."),
            ("Open with Notepad context menu", "A new right-click option to open with Notepad."),
            ("Microsoft Edge Debloater", "Disables background processes/Startup Boost/Copilot sidebar."),
        ]
        for title, desc in tweaks_items:
            index.append((f"[Tweaks]  {title} — {desc}", "Tweaks", None))
        for name in self.page_names:
            index.append((f"[Page]  {name}", name, None))

        def do_filter(q):
            results_list.clear()
            q = (q or "").lower().strip()
            for text, page, key in index:
                if not q or q in text.lower():
                    item = QListWidgetItem(text)
                    item.setData(Qt.UserRole, (page, key))
                    results_list.addItem(item)

        def jump(item):
            if item is None:
                return
            page, key = item.data(Qt.UserRole)
            if page in self.page_names:
                idx = self.page_names.index(page)
                self.sidebar.setCurrentRow(idx)
            if key and page == "Privacy & Telemetry":
                privacy_page = self.pages.get("Privacy & Telemetry")
                if privacy_page is not None and hasattr(privacy_page, "search_box"):
                    for t in TELEMETRY_TOGGLES:
                        if t.key == key:
                            privacy_page.search_box.setText(t.title)
                            break
            dlg.accept()

        search_box.textChanged.connect(do_filter)
        results_list.itemDoubleClicked.connect(jump)
        search_box.returnPressed.connect(lambda: jump(results_list.currentItem() or (results_list.item(0) if results_list.count() else None)))
        do_filter("")
        search_box.setFocus()
        dlg.exec()

    def apply_current_page(self):
        page = self.current_page
        if page is None:
            return
        if hasattr(page, "apply") and callable(getattr(page, "apply")):
            page.apply()
        elif hasattr(page, "apply_all_checked_states") and callable(getattr(page, "apply_all_checked_states")):
            page.apply_all_checked_states()
        else:
            self.logger.log("Ctrl+S: the current page has no apply action.")

    def refresh_admin_badge(self):
        if IS_WINDOWS:
            admin = is_admin()
            self.badge_admin.setObjectName("adminBadgeOn" if admin else "adminBadgeOff")
            self.badge_admin.setText(f"●  {'Administrator' if admin else 'Standard user'}")
            self.btn_admin.setEnabled(not admin)
        else:
            self.badge_admin.setObjectName("adminBadgeOff")
            self.badge_admin.setText("●  Non-Windows")
            self.btn_admin.setEnabled(False)
        self.badge_admin.style().unpolish(self.badge_admin)
        self.badge_admin.style().polish(self.badge_admin)

    def undo_last_change(self):
        desc = peek_history_description()
        if not desc:
            QMessageBox.information(self, "Undo", "There is no recent change to undo.")
            return
        confirm = QMessageBox.question(
            self, "Undo Last Change",
            f"Undo the last change:\n\n{desc}\n\nContinue?",
            QMessageBox.Yes | QMessageBox.No,
        )
        if confirm != QMessageBox.Yes:
            return
        ok, err, description = undo_last_history_entry()
        if ok:
            self.logger.success(f"Undo: {description}")
            QMessageBox.information(self, "Undo", "The change was undone.")
        else:
            self.logger.error(f"Undo failed: {err}")
            QMessageBox.warning(self, "Undo", f"Undo failed:\n{err}")

    def open_history_timeline(self):
        dlg = QDialog(self)
        dlg.setWindowTitle("🕐  Action History & Timeline")
        dlg.resize(640, 420)
        lay = QVBoxLayout(dlg)
        lay.addWidget(QLabel(
            "<p style='color:#9AA5C0;'>History of recent bulk-apply actions. "
            "You can undo any of them — not just the last one.</p>"
        ))

        table = QTableWidget(0, 3)
        table.setHorizontalHeaderLabels(["Time", "Action", ""])
        table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeToContents)
        table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeToContents)
        table.setEditTriggers(QTableWidget.NoEditTriggers)
        table.verticalHeader().setVisible(False)
        lay.addWidget(table, 1)

        def populate():
            entries = get_history_entries()
            table.setRowCount(len(entries))
            for r, e in enumerate(entries):
                table.setItem(r, 0, QTableWidgetItem(e["time"]))
                desc_item = QTableWidgetItem(e["description"] + ("  (undone)" if e["undone"] else ""))
                if e["undone"]:
                    desc_item.setForeground(QColor("#5B6478"))
                table.setItem(r, 1, desc_item)
                btn = QPushButton("↩ Undo" if not e["undone"] else "—")
                btn.setEnabled(not e["undone"])
                btn.setObjectName("btnWarn")
                if not e["undone"]:
                    btn.clicked.connect(lambda checked=False, entry_id=e["id"]: do_undo(entry_id))
                table.setCellWidget(r, 2, btn)

        def do_undo(entry_id: int):
            entries = get_history_entries()
            match = next((e for e in entries if e["id"] == entry_id), None)
            desc = match["description"] if match else "this change"
            confirm = QMessageBox.question(
                dlg, "Undo", f"Undo:\n\n{desc}\n\nContinue?", QMessageBox.Yes | QMessageBox.No,
            )
            if confirm != QMessageBox.Yes:
                return
            ok, err, description = undo_history_entry_by_id(entry_id)
            if ok:
                self.logger.success(f"Undo: {description}")
            else:
                self.logger.error(f"Undo failed: {err}")
                QMessageBox.warning(dlg, "Undo", f"Failed:\n{err}")
            populate()

        populate()
        close_btn = QPushButton("Close")
        close_btn.clicked.connect(dlg.accept)
        lay.addWidget(close_btn)
        dlg.exec()

    def toggle_theme(self):
        self._light_mode = not self._light_mode
        app = QApplication.instance()
        if self._light_mode:
            app.setStyleSheet(LIGHT_STYLESHEET + ACCENT_OVERRIDE)
            self.btn_theme.setText("☀️  Light")
            self.logger.log("Theme switched to Light mode.")
        else:
            app.setStyleSheet(APP_STYLESHEET + ACCENT_OVERRIDE)
            self.btn_theme.setText("🌙  Dark")
            self.logger.log("Theme switched to Dark mode.")

    def run_as_admin_clicked(self):
        if not IS_WINDOWS:
            return
        self.logger.log("User clicked: Run as Admin")
        elevate_self()

    def show_page(self, idx: int):
        if idx < 0 or idx >= len(self.page_names):
            return
        name = self.page_names[idx]
        if self.current_page is not None:
            self.current_page.setParent(None)

        page = self.pages[name]
        self.stack_layout.addWidget(page)
        self.current_page = page

        icon_file = PAGE_ICON_FILES.get(name, "")
        icon_path = find_resource("page_icons", icon_file) if icon_file else ""
        if icon_path:
            self.hero_icon.setPixmap(QPixmap(icon_path))
            self.hero_icon.setVisible(True)
            self.hero_title.setText(name)
        else:
            self.hero_icon.setVisible(False)
            icon = PAGE_ICONS.get(name, "•")
            self.hero_title.setText(f"{icon}  {name}")
        self.hero_subtitle.setText(PAGE_SUBTITLES.get(name, ""))

        self.logger.log(f"Opened page: {name}")
        self.refresh_admin_badge()

    def closeEvent(self, event):
        running_threads = [thread for thread in _ACTIVE_THREADS if thread.isRunning()]
        if running_threads:
            QMessageBox.warning(
                self, "Operation in progress",
                "Close the app once the active tasks are finished.",
            )
            event.ignore()
            return
        event.accept()


def _compute_accent_override() -> str:
    accent = get_windows_accent_color()
    if not accent:
        return ""
    return f"""
QPushButton#btnPrimary {{ background-color: {accent}; }}
QPushButton#btnPrimary:hover {{ background-color: {accent}; }}
QListWidget#sidebar::item:selected {{ border-left: 2px solid {accent}; }}
QLabel#logoBadge {{ background-color: {accent}; }}
"""


ACCENT_OVERRIDE = _compute_accent_override()


def main():
    if IS_WINDOWS:
        try:
            # Gives the app its own taskbar identity so Windows uses our icon
            # instead of grouping under the generic python.exe icon.
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("WinForge.App.1")
        except Exception:
            pass

        # DPI awareness MUST be set before QApplication is created. Without
        # this, Windows treats the app as DPI-unaware and bitmap-stretches
        # its rendered output to match the monitor's scale factor — this is
        # exactly what causes buttons/text to look compressed or overlap on
        # a laptop's higher-DPI screen, even though the same window looks
        # perfectly normal on an external monitor running at 100% scaling.
        # Try Per-Monitor-V2 first (Windows 10 1703+, best), then fall back
        # to older APIs for compatibility with earlier Windows versions.
        try:
            ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
        except Exception:
            try:
                ctypes.windll.shcore.SetProcessDpiAwareness(2)  # PROCESS_PER_MONITOR_DPI_AWARE
            except Exception:
                try:
                    ctypes.windll.user32.SetProcessDPIAware()
                except Exception:
                    pass

    # Avoids Qt rounding fractional scale factors (125%, 150%) to the
    # nearest integer, which can otherwise cause slightly blurry text on
    # common laptop scaling settings.
    try:
        QApplication.setHighDpiScaleFactorRoundingPolicy(Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)
    except Exception:
        pass

    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    app.setFont(QFont("Segoe UI", 10))
    app.setStyleSheet(APP_STYLESHEET + ACCENT_OVERRIDE)

    if os.path.isfile(APP_ICON_PATH):
        app.setWindowIcon(QIcon(APP_ICON_PATH))

    w = MainWindow()
    if os.path.isfile(APP_ICON_PATH):
        w.setWindowIcon(QIcon(APP_ICON_PATH))
    w.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
