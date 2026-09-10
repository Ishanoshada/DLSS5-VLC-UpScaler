#!/usr/bin/env python3
"""
app.py
------
Simple Windows GUI for the DLSS5 VLC Upscaler.

Wraps scripts/install.py and scripts/vlc-shader.py so you can:
  - See at a glance whether the VLC folder is ready
  - Install / Uninstall with one click
  - Pick any video file, launch VLC with ReShade injected
  - Stop VLC with one click

Usage:
  python app.py                (normal user is fine for launch/stop)
  run as Administrator         (needed for install/uninstall into Program Files)

Requires only the Python standard library (tkinter).
"""

from __future__ import annotations

import ctypes
import os
import subprocess
import sys
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, scrolledtext, ttk

# ---------------------------------------------------------------------------
# Paths  (this file lives in scripts\gui\, the scripts are one level up)
# ---------------------------------------------------------------------------

HERE = Path(__file__).resolve().parent          # ...\scripts\gui
SCRIPTS = HERE.parent                            # ...\scripts
REPO = SCRIPTS.parent                            # ...\repo

INSTALL_PY = SCRIPTS / "install.py"
VLC_SHADER_PY = SCRIPTS / "vlc-shader.py"

VLC_DEFAULT = Path(r"C:\Program Files\VideoLAN\VLC")
VLC_EXE = VLC_DEFAULT / "vlc.exe"

# Every extension we offer in the picker. The launcher itself accepts any
# file VLC can open; this is only a convenience filter.
VIDEO_EXTS = [
    "mp4", "mkv", "avi", "mov", "webm", "m4v", "wmv", "flv",
    "mpg", "mpeg", "m2ts", "ts", "mts", "vob", "ogv", "ogm",
    "divx", "3gp", "3g2", "asf", "rm", "rmvb", "f4v", "mxf",
    "hevc", "h264", "h265", "av1", "vp9", "mka", "mks",
]

VIDEO_FILTER = [
    ("Video files", " ".join(f"*.{e}" for e in VIDEO_EXTS)),
    ("Matroska",    "*.mkv *.mka *.mks"),
    ("MPEG / TS",   "*.mpg *.mpeg *.m2ts *.ts *.mts"),
    ("All files",   "*.*"),
]

# Files we check to know if VLC is set up
CHECK_FILES = [
    VLC_DEFAULT / "dxgi.dll",
    VLC_DEFAULT / "dlss5-feed.addon64",
    VLC_DEFAULT / "renodx-dlss5.addon64",
    VLC_DEFAULT / "ReShade.ini",
    VLC_DEFAULT / "ReShadePreset.ini",
    VLC_DEFAULT / "dlss5-feed.cfg",
    VLC_DEFAULT / "reshade-shaders" / "Shaders" / "DLSS5_Feed.fx",
    VLC_DEFAULT / "reshade-shaders" / "Shaders" / "lumenite_Kernel.fx",
]

# Subprocess environment: force UTF-8 on both sides so nothing crashes on
# non-ASCII output when piped.
def _subprocess_env() -> dict:
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    env["PYTHONUNBUFFERED"] = "1"
    return env


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def is_admin() -> bool:
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def _no_window():
    return getattr(subprocess, "CREATE_NO_WINDOW", 0)


def vlc_pids() -> list[int]:
    try:
        out = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq vlc.exe", "/NH", "/FO", "CSV"],
            capture_output=True, text=True, timeout=5,
            encoding="utf-8", errors="replace",
            creationflags=_no_window(),
        ).stdout
    except Exception:
        return []
    pids = []
    for line in out.splitlines():
        if "vlc.exe" not in line.lower():
            continue
        parts = [p.strip('"') for p in line.split(",")]
        if len(parts) >= 2:
            try:
                pids.append(int(parts[1]))
            except ValueError:
                pass
    return pids


def kill_vlc() -> int:
    pids = vlc_pids()
    if not pids:
        return 0
    try:
        subprocess.run(
            ["taskkill", "/IM", "vlc.exe", "/F"],
            capture_output=True, timeout=10,
            creationflags=_no_window(),
        )
    except Exception:
        pass
    return len(pids)


# ---------------------------------------------------------------------------
# GUI
# ---------------------------------------------------------------------------

class App(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("DLSS5 VLC Upscaler")
        self.geometry("760x560")
        self.minsize(640, 480)

        self.video_path = tk.StringVar(value="")
        self.status_text = tk.StringVar(value="Checking…")
        self.admin_text = tk.StringVar()
        self.pids_text = tk.StringVar(value="VLC: not running")
        self.skip_big = tk.BooleanVar(value=True)
        self.busy = False

        self._build_ui()
        self._refresh_status()
        self._poll_pids()

    # ---------------- UI ----------------

    def _build_ui(self) -> None:
        # Status
        top = ttk.LabelFrame(self, text="Status", padding=10)
        top.pack(fill="x", padx=10, pady=(10, 6))

        self.status_label = ttk.Label(
            top, textvariable=self.status_text,
            font=("Segoe UI", 11, "bold"),
        )
        self.status_label.pack(anchor="w")
        ttk.Label(top, textvariable=self.admin_text,
                  font=("Segoe UI", 9)).pack(anchor="w", pady=(2, 0))
        ttk.Label(top, textvariable=self.pids_text,
                  font=("Segoe UI", 9)).pack(anchor="w", pady=(2, 0))

        # Install row
        row1 = ttk.Frame(self)
        row1.pack(fill="x", padx=10, pady=(0, 6))

        self.btn_install = ttk.Button(row1, text="Install into VLC",
                                      command=self.on_install, width=20)
        self.btn_install.pack(side="left", padx=(0, 6))

        self.btn_uninstall = ttk.Button(row1, text="Uninstall",
                                        command=self.on_uninstall, width=14)
        self.btn_uninstall.pack(side="left", padx=(0, 6))

        self.btn_refresh = ttk.Button(row1, text="Refresh status",
                                      command=self._refresh_status, width=14)
        self.btn_refresh.pack(side="left", padx=(0, 6))

        ttk.Checkbutton(row1, text="Skip big NVIDIA DLLs",
                        variable=self.skip_big).pack(side="left", padx=(12, 0))

        # Video picker
        picker = ttk.LabelFrame(self, text="Video", padding=10)
        picker.pack(fill="x", padx=10, pady=(6, 6))

        vid_row = ttk.Frame(picker)
        vid_row.pack(fill="x")
        ttk.Entry(vid_row, textvariable=self.video_path,
                  font=("Consolas", 9)).pack(side="left", fill="x", expand=True)
        ttk.Button(vid_row, text="Browse…", command=self.on_browse,
                   width=10).pack(side="left", padx=(6, 0))

        # Launch row
        row3 = ttk.Frame(self)
        row3.pack(fill="x", padx=10, pady=(0, 6))

        self.btn_launch = ttk.Button(row3, text="Launch VLC with ReShade",
                                     command=self.on_launch, width=28)
        self.btn_launch.pack(side="left", padx=(0, 6))

        self.btn_stop = ttk.Button(row3, text="Stop VLC",
                                   command=self.on_stop, width=14)
        self.btn_stop.pack(side="left", padx=(0, 6))

        self.btn_open_vlc = ttk.Button(row3, text="Open VLC folder",
                                       command=self.on_open_folder, width=16)
        self.btn_open_vlc.pack(side="left")

        # Log
        log_frame = ttk.LabelFrame(self, text="Log", padding=4)
        log_frame.pack(fill="both", expand=True, padx=10, pady=(0, 10))

        self.log = scrolledtext.ScrolledText(
            log_frame, font=("Consolas", 9), wrap="word",
            background="#111", foreground="#ddd",
            insertbackground="#ddd", height=12,
        )
        self.log.pack(fill="both", expand=True)
        self.log.configure(state="disabled")

        ttk.Label(
            self,
            text="Install/Uninstall need Administrator. Launch/Stop work as a normal user.",
            font=("Segoe UI", 8), foreground="#666",
        ).pack(anchor="w", padx=12, pady=(0, 8))

    # ---------------- Log ----------------

    def log_line(self, msg: str) -> None:
        self.log.configure(state="normal")
        self.log.insert("end", msg.rstrip() + "\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    # ---------------- Status ----------------

    def _refresh_status(self) -> None:
        missing = [p for p in CHECK_FILES if not p.is_file()]

        if not VLC_EXE.is_file():
            self.status_text.set(f"X VLC not found at {VLC_DEFAULT}")
            self.status_label.configure(foreground="#c00")
        elif missing:
            self.status_text.set(f"! Not installed - {len(missing)} file(s) missing")
            self.status_label.configure(foreground="#c80")
        else:
            self.status_text.set("OK - ReShade + Feeder installed")
            self.status_label.configure(foreground="#080")

        self.admin_text.set(
            "Running as Administrator" if is_admin()
            else "Not Administrator - install/uninstall will fail"
        )

    def _poll_pids(self) -> None:
        pids = vlc_pids()
        if pids:
            self.pids_text.set(f"VLC running: PID {', '.join(map(str, pids))}")
        else:
            self.pids_text.set("VLC: not running")
        self.after(1500, self._poll_pids)

    # ---------------- Actions ----------------

    def _set_busy(self, busy: bool) -> None:
        self.busy = busy
        state = "disabled" if busy else "normal"
        for b in (self.btn_install, self.btn_uninstall, self.btn_launch,
                  self.btn_stop, self.btn_refresh):
            b.configure(state=state)

    def on_browse(self) -> None:
        p = filedialog.askopenfilename(
            title="Pick a video",
            filetypes=VIDEO_FILTER,
        )
        if p:
            self.video_path.set(p)

    def on_open_folder(self) -> None:
        try:
            os.startfile(str(VLC_DEFAULT))
        except Exception as e:
            messagebox.showerror("Cannot open folder", str(e))

    def on_install(self) -> None:
        if not is_admin():
            if not messagebox.askyesno(
                "Not Administrator",
                "Installing to C:\\Program Files\\VideoLAN\\VLC requires "
                "Administrator rights.\n\n"
                "Close this window and re-open it as Administrator.\n\n"
                "Continue anyway? (will likely fail)",
            ):
                return

        args = ["--skip-big"] if self.skip_big.get() else []
        self._run_async(
            "Installing DLSS5 Feeder into VLC…",
            [sys.executable, str(INSTALL_PY)] + args,
            on_done=self._after_install,
        )

    def _after_install(self, code: int) -> None:
        self._refresh_status()
        if code == 0:
            messagebox.showinfo("Install", "Install finished. See log for details.")
        else:
            messagebox.showwarning(
                "Install",
                f"install.py exited with code {code}.\n"
                "Check the log for details.",
            )

    def on_uninstall(self) -> None:
        if not messagebox.askyesno(
            "Uninstall",
            "Remove every DLSS5 / ReShade file from VLC?\n\n"
            "This will not touch VLC itself - only the files we installed.",
        ):
            return
        self._run_async(
            "Uninstalling DLSS5 / ReShade from VLC…",
            [sys.executable, str(INSTALL_PY), "--uninstall"],
            on_done=lambda c: (
                self._refresh_status(),
                messagebox.showinfo("Uninstall",
                                    f"Uninstall finished (exit {c})."),
            ),
        )

    def on_launch(self) -> None:
        video = self.video_path.get().strip()
        if video:
            # Windows accepts forward slashes, but VLC is happiest with native
            # separators. Normalise without requiring the file to exist first
            # (a network share may take a moment).
            video = str(Path(video))
            if not Path(video).is_file():
                messagebox.showerror("Video not found", f"Not a file:\n{video}")
                return

        if vlc_pids():
            if not messagebox.askyesno(
                "VLC already running",
                "VLC is already running without ReShade injected.\n\n"
                "Close it and relaunch with ReShade?",
            ):
                return
            kill_vlc()

        args = [sys.executable, str(VLC_SHADER_PY)]
        if video:
            args += ["--video", video]

        self._run_async("Launching VLC with ReShade injected…", args)

    def on_stop(self) -> None:
        n = kill_vlc()
        if n:
            self.log_line(f"[stop] terminated {n} VLC process(es)")
        else:
            self.log_line("[stop] no VLC process was running")

    # ---------------- Async runner ----------------

    def _run_async(self, title: str, cmd: list[str],
                   on_done=None) -> None:
        if self.busy:
            return
        self._set_busy(True)
        self.log_line(f"\n=== {title} ===")
        self.log_line(f"$ {' '.join(cmd)}")

        env = _subprocess_env()

        def worker() -> None:
            code = 1
            try:
                proc = subprocess.Popen(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    bufsize=1,
                    env=env,
                    creationflags=_no_window(),
                )
                assert proc.stdout is not None
                for line in proc.stdout:
                    self.after(0, self.log_line, line)
                code = proc.wait()
            except Exception as e:
                self.after(0, self.log_line, f"(error: {e})")
            self.after(0, self._on_worker_done, code, on_done)

        threading.Thread(target=worker, daemon=True).start()

    def _on_worker_done(self, code: int, on_done) -> None:
        self.log_line(f"=== done (exit {code}) ===\n")
        self._set_busy(False)
        if on_done:
            try:
                on_done(code)
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Entry
# ---------------------------------------------------------------------------

def main() -> int:
    app = App()
    app.mainloop()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(0)