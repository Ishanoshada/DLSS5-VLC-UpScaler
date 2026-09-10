#!/usr/bin/env python3
"""
vlc-shader.py
-------------
Launch VLC with ReShade force-injected, bypassing VLC's SetDefaultDllDirectories
mitigation (which blocks the classic drop-dxgi.dll-next-to-exe method).

How:
  1. CreateProcess(vlc.exe, CREATE_SUSPENDED)
  2. VirtualAllocEx + WriteProcessMemory to place the DLL path
  3. CreateRemoteThread calling LoadLibraryW(<path>)
  4. Wait for the remote thread, then ResumeThread(main)

Usage:
  python vlc-shader.py --video "D:\\movies\\file.mkv"
  python vlc-shader.py
  python vlc-shader.py --dll "path\\to\\ReShade64.dll"
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Make stdout/stderr UTF-8 safe on Windows consoles and pipes.
# Must run before anything else prints.
# ---------------------------------------------------------------------------

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
import subprocess
import time
from ctypes import wintypes
from pathlib import Path

VLC_DEFAULT    = Path(r"C:\Program Files\VideoLAN\VLC\vlc.exe")
RESHADE_DEFAULT = VLC_DEFAULT.parent / "dxgi.dll"

# --- Win32 ---------------------------------------------------------------

k32 = ctypes.WinDLL("kernel32", use_last_error=True)

CREATE_SUSPENDED       = 0x00000004
MEM_COMMIT             = 0x00001000
MEM_RESERVE            = 0x00002000
PAGE_READWRITE         = 0x04
INFINITE               = 0xFFFFFFFF
PROCESS_ALL_ACCESS     = 0x001F0FFF
STARTF_USESHOWWINDOW   = 0x00000001
SW_SHOWNORMAL          = 1

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


def _check(ok, what):
    if not ok:
        err = ctypes.get_last_error()
        raise OSError(f"{what} failed: WinError {err}")


def inject(pid: int, dll_path: Path) -> None:
    """Load dll_path into the process with the given pid via CreateRemoteThread."""
    print(f"[*] Injecting {dll_path.name} into PID {pid}")
    h = k32.OpenProcess(PROCESS_ALL_ACCESS, False, pid)
    _check(h, "OpenProcess")

    try:
        path_bytes = (str(dll_path) + "\0").encode("utf-16-le")
        size = len(path_bytes)

        remote_buf = k32.VirtualAllocEx(
            h, None, size, MEM_COMMIT | MEM_RESERVE, PAGE_READWRITE,
        )
        _check(remote_buf, "VirtualAllocEx")

        written = ctypes.c_size_t(0)
        _check(
            k32.WriteProcessMemory(h, remote_buf, path_bytes,
                                   size, ctypes.byref(written)),
            "WriteProcessMemory",
        )

        k32_loadlibrary = k32.GetProcAddress(
            k32.GetModuleHandleW("kernel32.dll"), b"LoadLibraryW")
        if not k32_loadlibrary:
            raise OSError("GetProcAddress(LoadLibraryW) returned NULL")

        hthread = k32.CreateRemoteThread(
            h, None, 0, ctypes.c_void_p(k32_loadlibrary),
            remote_buf, 0, None,
        )
        _check(hthread, "CreateRemoteThread")

        k32.WaitForSingleObject(hthread, INFINITE)
        k32.CloseHandle(hthread)
        print(f"[OK] Injected into PID {pid}")
    finally:
        k32.CloseHandle(h)


def launch_and_inject(vlc: Path, video: Path | None, dll: Path) -> int:
    if not vlc.is_file():
        print(f"[!] vlc.exe not found: {vlc}")
        return 2
    if not dll.is_file():
        print(f"[!] ReShade DLL not found: {dll}")
        return 2
    if video and not video.is_file():
        print(f"[!] Video file not found: {video}")
        return 2

    # Build a Windows-style command line. Any path (mp4, mkv, avi, ts, ...)
    # goes through exactly the same way: VLC's demuxer decides what to do.
    cmdline = f'"{vlc}"'
    if video:
        cmdline += f' "{video}"'
    print(f"[*] Launching (suspended): {cmdline}")

    si = STARTUPINFO()
    si.cb = ctypes.sizeof(si)
    si.dwFlags = STARTF_USESHOWWINDOW
    si.wShowWindow = SW_SHOWNORMAL
    pi = PROCESS_INFORMATION()

    ok = k32.CreateProcessW(
        str(vlc), ctypes.c_wchar_p(cmdline),
        None, None, False,
        CREATE_SUSPENDED,
        None, str(vlc.parent),
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
    k32.CloseHandle(pi.hThread)
    k32.CloseHandle(pi.hProcess)
    print("[OK] VLC resumed. ReShade overlay should be active.")
    print("[*] Press HOME in VLC to open it.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Launch VLC with ReShade force-injected.")
    ap.add_argument("--vlc", type=Path, default=VLC_DEFAULT)
    ap.add_argument("--video", type=Path, default=None,
                    help="optional media file to open (any format VLC handles)")
    ap.add_argument("--dll", type=Path, default=RESHADE_DEFAULT,
                    help=f"ReShade DLL to inject (default: {RESHADE_DEFAULT})")
    args = ap.parse_args()

    if sys.platform != "win32":
        print("Windows only.")
        return 2

    return launch_and_inject(args.vlc, args.video, args.dll)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n[*] stopped.")
        sys.exit(0)