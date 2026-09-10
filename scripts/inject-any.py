#!/usr/bin/env python3
"""
inject-any.py
-------------
Launch ANY Windows .exe with ReShade force-injected at runtime.

Generalizes vlc-shader.py: instead of a hardcoded VLC path, the target is
whatever you pass with --target. The injection mechanism is the same one
that bypasses VLC's SetDefaultDllDirectories mitigation — it works on any
process that isn't protected by anti-cheat / signed-driver enforcement.

Usage:
  # Simplest: inject whatever ReShade DLL is next to the exe
  python inject-any.py --target "C:\\Program Files\\DAUM\\PotPlayer\\PotPlayer64.exe"

  # With a file argument
  python inject-any.py --target "C:\\Program Files\\mpv\\mpv.exe" --file "D:\\movie.mkv"

  # Pass arbitrary command-line args to the target
  python inject-any.py --target "C:\\game\\game.exe" --args "-windowed" "-dx11"

  # Explicit ReShade DLL (overrides auto-detect)
  python inject-any.py --target "app.exe" --dll "C:\\reshade\\ReShade64.dll"

  # Kill already-running instances first
  python inject-any.py --target "app.exe" --kill-existing

  # Kill by PID instead of waiting for exit
  python inject-any.py --target "app.exe" --detach

Auto-detection:
  Looks next to the target exe for the first of:
    dxgi.dll  (D3D11/D3D12 proxy — preferred)
    d3d11.dll (D3D11 alternative)
    d3d12.dll (D3D12 alternative)
    dinput8.dll (DirectInput 8 — wide coverage, low priority)
    winmm.dll   (last resort — everything loads it)
  If none exist, falls back to a shared DLL next to this script:
    reshade\ReShade64.dll or reshade\ReShade32.dll
"""

from __future__ import annotations

# UTF-8 safe stdout for the same reason vlc-shader.py needed it
import sys


def _force_utf8() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


_force_utf8()

import argparse
import ctypes
import os
import struct
import subprocess
import time
from ctypes import wintypes
from pathlib import Path

# ---------------------------------------------------------------------------
# Win32
# ---------------------------------------------------------------------------

k32 = ctypes.WinDLL("kernel32", use_last_error=True)

CREATE_SUSPENDED = 0x00000004
MEM_COMMIT       = 0x00001000
MEM_RESERVE      = 0x00002000
PAGE_READWRITE   = 0x04
INFINITE         = 0xFFFFFFFF
PROCESS_ALL_ACCESS = 0x001F0FFF
STARTF_USESHOWWINDOW = 0x00000001
SW_SHOWNORMAL    = 1

k32.CreateProcessW.restype = wintypes.BOOL
k32.CreateProcessW.argtypes = [
    wintypes.LPCWSTR, wintypes.LPWSTR,
    ctypes.c_void_p, ctypes.c_void_p,
    wintypes.BOOL, wintypes.DWORD,
    ctypes.c_void_p, wintypes.LPCWSTR,
    ctypes.c_void_p, ctypes.c_void_p,
]
k32.ResumeThread.restype = wintypes.DWORD
k32.ResumeThread.argtypes = [wintypes.HANDLE]
k32.OpenProcess.restype = wintypes.HANDLE
k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
k32.VirtualAllocEx.restype = ctypes.c_void_p
k32.VirtualAllocEx.argtypes = [
    wintypes.HANDLE, ctypes.c_void_p, ctypes.c_size_t,
    wintypes.DWORD, wintypes.DWORD,
]
k32.WriteProcessMemory.restype = wintypes.BOOL
k32.WriteProcessMemory.argtypes = [
    wintypes.HANDLE, ctypes.c_void_p, ctypes.c_void_p,
    ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t),
]
k32.GetModuleHandleW.restype = wintypes.HMODULE
k32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
k32.GetProcAddress.restype = ctypes.c_void_p
k32.GetProcAddress.argtypes = [wintypes.HMODULE, ctypes.c_char_p]
k32.CreateRemoteThread.restype = wintypes.HANDLE
k32.CreateRemoteThread.argtypes = [
    wintypes.HANDLE, ctypes.c_void_p, ctypes.c_size_t,
    ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD,
    ctypes.POINTER(wintypes.DWORD),
]
k32.WaitForSingleObject.restype = wintypes.DWORD
k32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
k32.CloseHandle.restype = wintypes.BOOL
k32.CloseHandle.argtypes = [wintypes.HANDLE]


class STARTUPINFO(ctypes.Structure):
    _fields_ = [
        ("cb",              wintypes.DWORD),
        ("lpReserved",      wintypes.LPWSTR),
        ("lpDesktop",       wintypes.LPWSTR),
        ("lpTitle",         wintypes.LPWSTR),
        ("dwX",             wintypes.DWORD),
        ("dwY",             wintypes.DWORD),
        ("dwXSize",         wintypes.DWORD),
        ("dwYSize",         wintypes.DWORD),
        ("dwXCountChars",   wintypes.DWORD),
        ("dwYCountChars",   wintypes.DWORD),
        ("dwFillAttribute", wintypes.DWORD),
        ("dwFlags",         wintypes.DWORD),
        ("wShowWindow",     wintypes.WORD),
        ("cbReserved2",     wintypes.WORD),
        ("lpReserved2",     ctypes.c_void_p),
        ("hStdInput",       wintypes.HANDLE),
        ("hStdOutput",      wintypes.HANDLE),
        ("hStdError",       wintypes.HANDLE),
    ]


class PROCESS_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("hProcess",    wintypes.HANDLE),
        ("hThread",     wintypes.HANDLE),
        ("dwProcessId", wintypes.DWORD),
        ("dwThreadId",  wintypes.DWORD),
    ]


def _check(ok, what: str) -> None:
    if not ok:
        raise OSError(f"{what} failed: WinError {ctypes.get_last_error()}")


# ---------------------------------------------------------------------------
# Target inspection
# ---------------------------------------------------------------------------

def pe_bitness(exe: Path) -> str:
    """'64', '32', or '?' by reading the PE header."""
    try:
        with exe.open("rb") as f:
            head = f.read(0x200)
        if head[:2] != b"MZ":
            return "?"
        e_lfanew = struct.unpack_from("<I", head, 0x3C)[0]
        with exe.open("rb") as f:
            f.seek(e_lfanew + 4)
            machine = struct.unpack("<H", f.read(2))[0]
        return {0x8664: "64", 0x14c: "32", 0xAA64: "64"}.get(machine, "?")
    except OSError:
        return "?"


# Order matters: dxgi first (D3D11/12, best), then d3d11/d3d12, then fallbacks.
PROXY_NAMES = ["dxgi.dll", "d3d11.dll", "d3d12.dll",
               "dinput8.dll", "version.dll", "winmm.dll"]


def find_proxy_dll(target_dir: Path, bitness: str) -> Path | None:
    """
    Return the ReShade DLL to inject.

    Priority:
      1. A proxy DLL next to the target exe whose name is in PROXY_NAMES and
         whose bitness matches the target.
      2. A ReShade DLL next to this script: reshade\\ReShade64.dll or
         reshade\\ReShade32.dll (or ReShade64.dll directly).
    """
    for name in PROXY_NAMES:
        p = target_dir / name
        if p.is_file() and pe_bitness(p) == bitness:
            return p

    # Shared fallback next to the script
    here = Path(__file__).resolve().parent
    candidates = [
        here / "reshade" / f"ReShade{bitness}.dll",
        here / f"ReShade{bitness}.dll",
        here / "reshade" / "ReShade64.dll",
    ]
    for c in candidates:
        if c.is_file():
            return c
    return None


def list_matching_processes(exe_name: str) -> list[int]:
    """PIDs of running processes whose image name matches exe_name."""
    try:
        out = subprocess.run(
            ["tasklist", "/FI", f"IMAGENAME eq {exe_name}",
             "/NH", "/FO", "CSV"],
            capture_output=True, text=True, timeout=10,
            encoding="utf-8", errors="replace",
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        ).stdout
    except Exception:
        return []
    pids = []
    low = exe_name.lower()
    for line in out.splitlines():
        if low not in line.lower():
            continue
        parts = [p.strip('"') for p in line.split(",")]
        if len(parts) >= 2:
            try:
                pids.append(int(parts[1]))
            except ValueError:
                pass
    return pids


def kill_matching(exe_name: str) -> int:
    pids = list_matching_processes(exe_name)
    if not pids:
        return 0
    try:
        subprocess.run(
            ["taskkill", "/IM", exe_name, "/F"],
            capture_output=True, timeout=10,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except Exception:
        pass
    return len(pids)


# ---------------------------------------------------------------------------
# Injection
# ---------------------------------------------------------------------------

def inject(pid: int, dll_path: Path) -> None:
    print(f"[*] Injecting {dll_path.name} into PID {pid}")
    h = k32.OpenProcess(PROCESS_ALL_ACCESS, False, pid)
    _check(h, "OpenProcess")
    try:
        path_bytes = (str(dll_path) + "\0").encode("utf-16-le")
        size = len(path_bytes)

        remote_buf = k32.VirtualAllocEx(
            h, None, size, MEM_COMMIT | MEM_RESERVE, PAGE_READWRITE)
        _check(remote_buf, "VirtualAllocEx")

        written = ctypes.c_size_t(0)
        _check(
            k32.WriteProcessMemory(h, remote_buf, path_bytes,
                                   size, ctypes.byref(written)),
            "WriteProcessMemory",
        )

        load = k32.GetProcAddress(
            k32.GetModuleHandleW("kernel32.dll"), b"LoadLibraryW")
        if not load:
            raise OSError("GetProcAddress(LoadLibraryW) returned NULL")

        ht = k32.CreateRemoteThread(h, None, 0, ctypes.c_void_p(load),
                                    remote_buf, 0, None)
        _check(ht, "CreateRemoteThread")
        k32.WaitForSingleObject(ht, INFINITE)
        k32.CloseHandle(ht)
        print(f"[OK] Injected into PID {pid}")
    finally:
        k32.CloseHandle(h)


def build_command_line(exe: Path, files: list[Path],
                       extra_args: list[str]) -> str:
    """Windows-style command line: exe then each quoted argument."""
    parts = [f'"{exe}"']
    for f in files:
        parts.append(f'"{f}"')
    for a in extra_args:
        # Don't double-quote arguments that already carry quotes
        parts.append(a if a.startswith('"') else f'"{a}"' if " " in a else a)
    return " ".join(parts)


def launch_and_inject(exe: Path, dll: Path,
                      files: list[Path],
                      extra_args: list[str],
                      detach: bool,
                      wait_seconds: float) -> int:
    if not exe.is_file():
        print(f"[!] Target not found: {exe}")
        return 2
    if not dll.is_file():
        print(f"[!] ReShade DLL not found: {dll}")
        return 2

    cmdline = build_command_line(exe, files, extra_args)
    print(f"[*] Launching (suspended): {cmdline}")

    si = STARTUPINFO()
    si.cb = ctypes.sizeof(si)
    si.dwFlags = STARTF_USESHOWWINDOW
    si.wShowWindow = SW_SHOWNORMAL
    pi = PROCESS_INFORMATION()

    ok = k32.CreateProcessW(
        str(exe), ctypes.c_wchar_p(cmdline),
        None, None, False,
        CREATE_SUSPENDED,
        None, str(exe.parent),
        ctypes.byref(si), ctypes.byref(pi),
    )
    _check(ok, "CreateProcessW")
    print(f"[*] PID {pi.dwProcessId} suspended")

    time.sleep(0.2)

    try:
        inject(pi.dwProcessId, dll)
    except OSError as e:
        print(f"[!] Injection failed: {e}")
        k32.ResumeThread(pi.hThread)
        k32.CloseHandle(pi.hThread)
        k32.CloseHandle(pi.hProcess)
        return 1

    k32.ResumeThread(pi.hThread)

    if detach:
        k32.CloseHandle(pi.hThread)
        k32.CloseHandle(pi.hProcess)
        print("[OK] Target resumed. ReShade attached.")
        print(f"[*] PID {pi.dwProcessId} is running — detaching.")
        return 0

    print("[OK] Target resumed. ReShade should be attached.")
    if wait_seconds > 0:
        print(f"[*] Waiting up to {wait_seconds:.0f}s for exit "
              "(Ctrl+C to detach early)…")
    else:
        print("[*] Waiting for exit (Ctrl+C to detach early)…")

    try:
        start = time.time()
        while True:
            rc = k32.WaitForSingleObject(pi.hProcess, 250)
            if rc == 0:                       # WAIT_OBJECT_0
                break
            if wait_seconds > 0 and time.time() - start > wait_seconds:
                print("[*] Wait timeout reached, detaching.")
                break
    except KeyboardInterrupt:
        print("\n[*] Detach requested.")
    finally:
        k32.CloseHandle(pi.hThread)
        k32.CloseHandle(pi.hProcess)
    return 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(
        description="Launch any Windows .exe with ReShade force-injected.")
    ap.add_argument("--target", "-t", type=Path, required=True,
                    help="path to the .exe to launch and inject")
    ap.add_argument("--dll", type=Path, default=None,
                    help="ReShade DLL to inject (default: auto-detect)")
    ap.add_argument("--file", type=Path, action="append", default=[],
                    help="a file to pass as an argument (repeatable)")
    ap.add_argument("--args", nargs=argparse.REMAINDER, default=[],
                    help="extra arguments to pass to the target verbatim "
                         "(everything after --args)")
    ap.add_argument("--kill-existing", action="store_true",
                    help="kill running instances of the target first")
    ap.add_argument("--detach", action="store_true",
                    help="return as soon as injection succeeds, don't wait "
                         "for the target to exit")
    ap.add_argument("--wait", type=float, default=0.0,
                    help="max seconds to wait for exit (0 = forever)")
    ap.add_argument("--show-dll", action="store_true",
                    help="print the DLL that would be injected, then exit")
    args = ap.parse_args()

    if sys.platform != "win32":
        print("Windows only.")
        return 2

    target: Path = args.target.resolve()
    if not target.is_file():
        print(f"[!] Target not found: {target}")
        return 2

    bitness = pe_bitness(target)
    if bitness == "?":
        print(f"[!] Cannot read PE header of {target}; aborting.")
        return 2

    dll = args.dll
    if dll is None:
        dll = find_proxy_dll(target.parent, bitness)
    if dll is None:
        print(f"[!] No ReShade DLL found next to {target.parent}")
        print(f"    Expected one of: {', '.join(PROXY_NAMES)}")
        print(f"    Or a shared copy at {Path(__file__).parent}\\reshade\\"
              f"ReShade{bitness}.dll")
        print(f"    Or specify one with --dll")
        return 2

    if args.show_dll:
        print(dll)
        return 0

    print(f"[*] Target   : {target}")
    print(f"[*] Bitness  : {bitness}-bit")
    print(f"[*] ReShade  : {dll}")

    if args.kill_existing:
        n = kill_matching(target.name)
        if n:
            print(f"[*] Killed {n} running {target.name} process(es)")
            time.sleep(0.5)

    files = [f.resolve() for f in args.file]
    for f in files:
        if not f.is_file():
            print(f"[!] File argument not found: {f}")
            return 2

    return launch_and_inject(
        target, dll, files, list(args.args),
        detach=args.detach, wait_seconds=args.wait,
    )


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n[*] stopped.")
        sys.exit(0)