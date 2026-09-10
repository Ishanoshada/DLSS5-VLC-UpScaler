#!/usr/bin/env python3
"""
install.py
----------
Verify the repo tree, auto-download anything missing, auto-create config
files, clean the VLC folder, and install every file the DLSS5 Feeder +
ReShade stack needs.

Everything is handled automatically:
  - Missing binaries      -> downloaded from GitHub / reshade.me
  - Missing config files  -> generated locally (they're just text)
  - Missing shader headers-> downloaded from crosire/reshade-shaders
  - Missing LumeniteFX    -> extracted from the full zip (shaders + includes
                             + textures)
  - Partial downloads     -> written to .part, renamed only on success
  - Flaky connections     -> 3 retries with exponential backoff

Usage:
  python install.py                    install (fetches + creates everything)
  python install.py --fetch            only download, do not install
  python install.py --force-fetch      re-download every file
  python install.py --skip-big         skip the 225 MB nvngx files
  python install.py --uninstall        remove DLSS5/ReShade from VLC
  python install.py --uninstall-all    also delete reshade-shaders\ + vlcrc
  python install.py --dry-run          show plan, change nothing

Run as Administrator.
"""

from __future__ import annotations

import argparse
import ctypes
import io
import shutil
import sys
import time
import urllib.request
import urllib.error
import zipfile
from pathlib import Path

# ---------------------------------------------------------------------------
# Terminal colors
# ---------------------------------------------------------------------------

def _enable_ansi() -> bool:
    if sys.platform != "win32":
        return True
    try:
        k32 = ctypes.windll.kernel32
        h = k32.GetStdHandle(-11)
        mode = ctypes.c_ulong()
        if not k32.GetConsoleMode(h, ctypes.byref(mode)):
            return False
        return bool(k32.SetConsoleMode(h, mode.value | 0x4))
    except Exception:
        return False


ANSI = _enable_ansi()

def _c(code: str, t: str) -> str:
    return t if not ANSI else f"\x1b[{code}m{t}\x1b[0m"

def green(t):  return _c("32", t)
def yellow(t): return _c("33", t)
def red(t):    return _c("31", t)
def cyan(t):   return _c("36", t)
def dim(t):    return _c("2",  t)
def bold(t):   return _c("1",  t)


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

REPO_DIR     = Path(__file__).resolve().parent.parent
FILES_DIR    = REPO_DIR / "dlss-files"
SHADERS_DIR  = REPO_DIR / "reshade-shaders" / "Shaders"
INCLUDE_DIR  = SHADERS_DIR / "include"
TEXTURES_DIR = REPO_DIR / "reshade-shaders" / "Textures"

VLC_DIR_DEFAULT = Path(r"C:\Program Files\VideoLAN\VLC")

# Files that go into VLC's root folder
BINARIES = [
    "dxgi.dll",
    "dlss5-feed.addon64",
    "renodx-dlss5.addon64",
    "ReShade.ini",
    "ReShadePreset.ini",
    "dlss5-feed.cfg",
]
OPTIONAL_BINARIES = [
    "nvngx_dlss.dll",
    "nvngx_dlssnr.dll",
]

# Shader files (all go into reshade-shaders\Shaders\)
SHADERS = [
    "DLSS5_Feed.fx",
    "lumenite_Kernel.fx",
    "lumenite_TRAA.fx",
]
SHADER_HEADERS = [
    "ReShade.fxh",
    "ReShadeUI.fxh",
    "DrawText.fxh",
    "Blending.fxh",
]
SHADER_INCLUDES = [
    "lumenite_Helpers.fxh",
]
TEXTURES = [
    "lumenite_bluenoise256.png",
]

# Files removed on uninstall
DELETE_ROOT = [
    "dxgi.dll", "d3d11.dll", "d3d12.dll", "winmm.dll", "dinput8.dll",
    "dlss5-feed.addon64", "renodx-dlss5.addon64",
    "nvngx_dlss.dll", "nvngx_dlssnr.dll",
    "ReShade.ini", "ReShadePreset.ini", "dlss5-feed.cfg", "ReShade.log",
]
DELETE_BACKUP_SUFFIXES = [".bak", ".placeholder", ".part"]
DELETE_SHADERS = SHADERS
DELETE_HEADERS = SHADER_HEADERS
DELETE_INCLUDES = SHADER_INCLUDES
DELETE_TEXTURES = TEXTURES
DELETE_FOLDERS = ["reshade-shaders"]

VLC_USER_CONFIG = Path.home() / "AppData" / "Roaming" / "vlc" / "vlcrc"

OK, WARN, FAIL, INFO = "OK", "WARN", "FAIL", "INFO"

# ---------------------------------------------------------------------------
# Download sources
# ---------------------------------------------------------------------------

# ReShade setup — mirrors + primary
RESHADE_SETUP_URLS = [
    "https://reshade.me/downloads/ReShade_Setup_6.8.0_Addon.exe",
    "https://github.com/crosire/reshade/releases/download/v6.8.0/ReShade_Setup_6.8.0_Addon.exe",
]

# DLSS5-Feeder — the tags move; try "latest/download" first
FEEDER_ASSETS = [
    "https://github.com/jlrouzies-fr/DLSS5-Feeder/releases/latest/download/{name}",
    "https://github.com/jlrouzies-fr/DLSS5-Feeder/releases/download/v0.15.1/{name}",
    "https://github.com/jlrouzies-fr/DLSS5-Feeder/releases/download/v0.14.1/{name}",
]

# RenoDX DLSS5 addon — community rehosts
RENODX_DLSS5_URLS = [
    "https://github.com/yumlevi/renodx-dlss-installer/releases/download/latest/renodx-dlss5-v2.5.addon64",
    "https://github.com/RankFTW/rhi-repo/releases/latest/download/renodx-dlss5.addon64",
]

# LumeniteFX zip
LUMENITE_ZIP_URLS = [
    "https://codeload.github.com/umar-afzaal/LumeniteFX/zip/refs/heads/mainline",
    "https://github.com/umar-afzaal/LumeniteFX/archive/refs/heads/mainline.zip",
]

# NVIDIA DLSS runtime zip — community pack
NVIDIA_ZIP_URLS = [
    "https://github.com/zhubaohi/FF7R-DLSS5/releases/download/v1/nvidia.zip",
]

# ReShade shader headers
RESHADE_SHADERS_RAW_URLS = [
    "https://raw.githubusercontent.com/crosire/reshade-shaders/slim/Shaders",
    "https://cdn.jsdelivr.net/gh/crosire/reshade-shaders@master/Shaders",
]

# ---------------------------------------------------------------------------
# File -> source spec
# ---------------------------------------------------------------------------
# kind:
#   "direct"  : bytes straight to file
#   "zip"     : zip member (basename match)
#   "reshade" : ReShade setup exe with appended zip
#   "generate": create locally (config files)

SOURCES: dict[str, dict] = {
    # Binaries
    "dxgi.dll": {
        "kind": "reshade",
        "member": "ReShade64.dll",
        "urls": RESHADE_SETUP_URLS,
    },
    "dlss5-feed.addon64": {
        "kind": "direct",
        "urls": [u.format(name="dlss5-feed.addon64") for u in FEEDER_ASSETS],
    },
    "renodx-dlss5.addon64": {
        "kind": "direct",
        "urls": RENODX_DLSS5_URLS,
    },
    "nvngx_dlss.dll": {
        "kind": "zip",
        "member": "nvngx_dlss.dll",
        "urls": NVIDIA_ZIP_URLS,
    },
    "nvngx_dlssnr.dll": {
        "kind": "zip",
        "member": "nvngx_dlssnr.dll",
        "urls": NVIDIA_ZIP_URLS,
    },

    # Shaders
    "DLSS5_Feed.fx": {
        "kind": "direct",
        "urls": [u.format(name="DLSS5_Feed.fx") for u in FEEDER_ASSETS],
    },
    "lumenite_Kernel.fx": {
        "kind": "zip",
        "member": "lumenite_Kernel.fx",
        "urls": LUMENITE_ZIP_URLS,
    },
    "lumenite_TRAA.fx": {
        "kind": "zip",
        "member": "lumenite_TRAA.fx",
        "urls": LUMENITE_ZIP_URLS,
    },
    "lumenite_Helpers.fxh": {
        "kind": "zip",
        "member": "lumenite_Helpers.fxh",
        "urls": LUMENITE_ZIP_URLS,
    },
    "lumenite_bluenoise256.png": {
        "kind": "zip",
        "member": "lumenite_bluenoise256.png",
        "urls": LUMENITE_ZIP_URLS,
    },

    # Shader headers (ReShade core)
    "ReShade.fxh": {
        "kind": "direct",
        "urls": [f"{u}/ReShade.fxh" for u in RESHADE_SHADERS_RAW_URLS],
    },
    "ReShadeUI.fxh": {
        "kind": "direct",
        "urls": [f"{u}/ReShadeUI.fxh" for u in RESHADE_SHADERS_RAW_URLS],
    },
    "DrawText.fxh": {
        "kind": "direct",
        "urls": [f"{u}/DrawText.fxh" for u in RESHADE_SHADERS_RAW_URLS],
    },
    "Blending.fxh": {
        "kind": "direct",
        "urls": [f"{u}/Blending.fxh" for u in RESHADE_SHADERS_RAW_URLS],
    },

    # Config files — generated locally, not downloaded
    "ReShade.ini":        {"kind": "generate", "template": "reshade_ini"},
    "ReShadePreset.ini":  {"kind": "generate", "template": "reshade_preset"},
    "dlss5-feed.cfg":     {"kind": "generate", "template": "feed_cfg"},
}

BIG_FILES = {"nvngx_dlss.dll", "nvngx_dlssnr.dll"}

# ---------------------------------------------------------------------------
# Config file templates
# ---------------------------------------------------------------------------

RESHADE_INI_TEMPLATE = """[GENERAL]
EffectSearchPaths=.\\reshade-shaders\\Shaders\\**
TextureSearchPaths=.\\reshade-shaders\\Textures\\**
PresetPath=.\\ReShadePreset.ini
PreprocessorDefinitions=DLSS5_MV_PROVIDER=3
Logging=1

[ADDON]
DisabledAddons=
"""

RESHADE_PRESET_TEMPLATE = """Techniques=Lumenite_Kernel@lumenite_Kernel.fx,DLSS5_Feed@DLSS5_Feed.fx
TechniqueSorting=Lumenite_Kernel@lumenite_Kernel.fx,DLSS5_Feed@DLSS5_Feed.fx
PreprocessorDefinitions=DLSS5_MV_PROVIDER=3

[DLSS5_Feed.fx]
APPEARANCE_MASK=1
APPEARANCE_THRESHOLD=0.060
APPEARANCE_STRENGTH=1.00
LIGHTING_MASK=1
LIGHTING_THRESHOLD=0.070
LIGHTING_STRENGTH=1.10
DETAIL_MASK=1
DETAIL_THRESHOLD=0.015
DETAIL_STRENGTH=1.30
"""

FEED_CFG_TEMPLATE = """enabled=1
mode=2
hdr=-1
depth_inverted=-1
flags=-1
reset_every=0
warmup_rebuild=180
rebuild=0
log_frames=3
create_delay=180
preset=0
work_resolution=40
work_upscale=1
work_sharpness=0.45
gpu_timeout_ms=10000
buffer_home=1
async_home=0
sync_home=0
mv_scale_x=1.000
mv_scale_y=1.000
stall_log_ms=50
reset_mode=2
log_detail=1
log_detail_every=60
light_stab=1
light_stab_strength=0.350
light_stab_max_delta=0.060
ofa_enabled=0
ofa_grid=2
ofa_perf=10
engine_velocity=0
velocity_cand=-1
velocity_decode=0
velocity_scale=1.000
quality_preset=auto-video
auto_profile_applied=1
auto_profile=
"""

TEMPLATES = {
    "reshade_ini":    RESHADE_INI_TEMPLATE,
    "reshade_preset": RESHADE_PRESET_TEMPLATE,
    "feed_cfg":       FEED_CFG_TEMPLATE,
}


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

def log(level: str, msg: str) -> None:
    tag = {
        OK:   green("[ OK ]"),
        WARN: yellow("[WARN]"),
        FAIL: red("[FAIL]"),
        INFO: cyan("[info]"),
    }[level]
    print(f"{tag} {msg}")


# ---------------------------------------------------------------------------
# Progress bar
# ---------------------------------------------------------------------------

def _fmt_bytes(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024.0:
            return f"{n:5.1f} {unit}"
        n /= 1024.0
    return f"{n:5.1f} TB"


def _fmt_time(seconds: float) -> str:
    if seconds <= 0 or seconds > 9999:
        return "  --:--"
    m, s = divmod(int(seconds), 60)
    if m >= 60:
        h, m = divmod(m, 60)
        return f"{h:d}:{m:02d}:{s:02d}"
    return f"  {m:02d}:{s:02d}"


def progress_bar(current: int, total: int, prefix: str,
                 width: int = 30, started: float | None = None,
                 extra: str = "") -> None:
    if total <= 0:
        spinner = "|/-\\"[int(time.time() * 8) % 4]
        line = f"        {spinner} {prefix} {_fmt_bytes(current)}  {extra}"
        sys.stdout.write("\r" + line[:120].ljust(120))
        sys.stdout.flush()
        return

    frac = min(current / total, 1.0)
    filled = int(width * frac)
    bar = ("#" * filled) + ("-" * (width - filled))

    rate_str = ""
    if started is not None and current > 0:
        elapsed = time.time() - started
        if elapsed > 0.1:
            bps = current / elapsed
            rate_str = f" {_fmt_bytes(bps)}/s"
            if frac > 0.001:
                eta = elapsed * (total - current) / current
                rate_str += f" ETA {_fmt_time(eta)}"

    pct = f"{frac*100:5.1f}%"
    size = f"{_fmt_bytes(current)}/{_fmt_bytes(total)}"
    line = f"        {prefix} [{bar}] {pct} {size}{rate_str} {extra}"
    sys.stdout.write("\r" + line[:140].ljust(140))
    sys.stdout.flush()


def finish_bar() -> None:
    sys.stdout.write("\r" + " " * 140 + "\r")
    sys.stdout.flush()


# ---------------------------------------------------------------------------
# Download helpers
# ---------------------------------------------------------------------------

class DownloadCancelled(Exception):
    pass


def _download_with_progress(url: str, label: str,
                            retries: int = 3) -> bytes:
    last_err: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            req = urllib.request.Request(
                url, headers={"User-Agent": "dlss5-vlc-upscaler/1.0"},
            )
            with urllib.request.urlopen(req, timeout=120) as r:
                total = int(r.headers.get("Content-Length", 0))
                chunks: list[bytes] = []
                read = 0
                started = time.time()
                last_update = 0.0
                while True:
                    chunk = r.read(64 * 1024)
                    if not chunk:
                        break
                    chunks.append(chunk)
                    read += len(chunk)
                    now = time.time()
                    if now - last_update > 0.05 or total == 0:
                        progress_bar(read, total, label, started=started)
                        last_update = now
                progress_bar(read, total, label, started=started)
                finish_bar()
                return b"".join(chunks)
        except KeyboardInterrupt:
            finish_bar()
            raise DownloadCancelled()
        except (urllib.error.URLError, urllib.error.HTTPError,
                ConnectionError, TimeoutError, OSError) as e:
            finish_bar()
            last_err = e
            if attempt < retries:
                wait = 2 ** attempt
                print(f"        {yellow('retry')} {attempt}/{retries} "
                      f"failed ({type(e).__name__}); waiting {wait}s")
                time.sleep(wait)
            else:
                raise
    if last_err:
        raise last_err
    raise RuntimeError("download failed")


def _extract_zip_member(data: bytes, member_name: str) -> bytes:
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        for name in z.namelist():
            if Path(name.replace("\\", "/")).name.lower() == member_name.lower():
                return z.read(name)
    raise RuntimeError(f"member {member_name} not in zip")


def _extract_reshade_dll(data: bytes, member_name: str) -> bytes:
    sig = b"PK\x03\x04"
    idx = len(data)
    attempts = 0
    while attempts < 50:
        attempts += 1
        idx = data.rfind(sig, 0, idx)
        if idx < 0:
            break
        try:
            with zipfile.ZipFile(io.BytesIO(data[idx:])) as z:
                if len(z.namelist()) < 2:
                    continue
                for name in z.namelist():
                    if Path(name.replace("\\", "/")).name.lower() == member_name.lower():
                        return z.read(name)
        except (zipfile.BadZipFile, OSError):
            continue
    raise RuntimeError(f"{member_name} not in ReShade setup")


def _try_urls(urls: list[str], label: str) -> bytes:
    """Try each URL in order until one succeeds."""
    last: Exception | None = None
    for i, url in enumerate(urls, 1):
        try:
            if i > 1:
                print(f"        {dim(f'try mirror {i}/{len(urls)}')}")
            return _download_with_progress(url, label)
        except DownloadCancelled:
            raise
        except Exception as e:
            last = e
            if i < len(urls):
                print(f"        {yellow('mirror failed')}: "
                      f"{type(e).__name__}")
                continue
    raise RuntimeError(f"all {len(urls)} sources failed: {last}")


# ---------------------------------------------------------------------------
# File operations
# ---------------------------------------------------------------------------

def make_template(name: str, dest: Path) -> bool:
    """Write a config file from its built-in template."""
    spec = SOURCES.get(name, {})
    template_key = spec.get("template")
    text = TEMPLATES.get(template_key or "")
    if not text:
        return False
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(text, encoding="utf-8")
    return True


def fetch_file(name: str, dest: Path, force: bool = False,
               skip_big: bool = False) -> bool:
    """Ensure dest exists. Generate if it's a template; download otherwise."""
    if skip_big and name in BIG_FILES and not dest.is_file():
        log(INFO, f"{name}: skipped (--skip-big)")
        return True

    if dest.is_file() and not force and dest.stat().st_size > 20:
        return True

    spec = SOURCES.get(name)
    if not spec:
        log(WARN, f"{name}: no source configured")
        return False

    kind = spec.get("kind")

    # Generated config files
    if kind == "generate":
        if make_template(name, dest):
            log(OK, f"{name}  ({dim('generated')}, "
                    f"{_fmt_bytes(dest.stat().st_size)})")
            return True
        log(FAIL, f"{name}: template {spec.get('template')!r} not found")
        return False

    # Downloaded files
    urls = spec.get("urls") or []
    if not urls:
        log(WARN, f"{name}: no URL configured")
        return False

    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_suffix(dest.suffix + ".part")

    try:
        data = _try_urls(urls, name)

        if kind == "direct":
            part.write_bytes(data)
        elif kind == "zip":
            member_data = _extract_zip_member(data, spec.get("member", name))
            part.write_bytes(member_data)
        elif kind == "reshade":
            member_data = _extract_reshade_dll(
                data, spec.get("member", "ReShade64.dll"))
            part.write_bytes(member_data)
        else:
            raise RuntimeError(f"unknown kind {kind!r}")

        if dest.is_file():
            dest.unlink()
        part.rename(dest)
        log(OK, f"{name}  ({_fmt_bytes(dest.stat().st_size)})")
        return True

    except DownloadCancelled:
        if part.is_file():
            part.unlink()
        raise
    except Exception as e:
        if part.is_file():
            part.unlink()
        log(FAIL, f"{name}: {type(e).__name__}: {e}")
        print(f"        {dim('source(s):')}")
        for u in urls[:2]:
            print(f"          {dim(u)}")
        return False


# ---------------------------------------------------------------------------
# Full fetch pass
# ---------------------------------------------------------------------------

def _all_targets() -> list[tuple[str, Path]]:
    """Every file the repo needs, with where it goes."""
    t: list[tuple[str, Path]] = []
    for n in BINARIES:
        t.append((n, FILES_DIR / n))
    for n in OPTIONAL_BINARIES:
        t.append((n, FILES_DIR / n))
    for n in SHADERS:
        t.append((n, SHADERS_DIR / n))
    for n in SHADER_HEADERS:
        t.append((n, SHADERS_DIR / n))
    for n in SHADER_INCLUDES:
        t.append((n, INCLUDE_DIR / n))
    for n in TEXTURES:
        t.append((n, TEXTURES_DIR / n))
    return t


def fetch_all(force: bool = False, skip_big: bool = False) -> tuple[int, int]:
    print()
    log(INFO, "Fetching + generating every required file")
    print()

    targets = _all_targets()
    pending = []
    for name, dest in targets:
        if skip_big and name in BIG_FILES and not dest.is_file():
            continue
        if not force and dest.is_file() and dest.stat().st_size > 20:
            continue
        pending.append((name, dest))

    if not pending:
        log(OK, "Every file already present")
        return (0, 0)

    total = len(pending)
    print(f"        {bold(f'{total} file(s) to fetch')}")
    if any(n in BIG_FILES for n, _ in pending):
        print(f"        {dim('(includes large NVIDIA DLLs — this can take a while)')}")
    print()

    ok = fail = 0
    started = time.time()
    try:
        for i, (name, dest) in enumerate(pending, 1):
            print(f"        {dim(f'[{i}/{total}]')}")
            if fetch_file(name, dest, force=force, skip_big=skip_big):
                ok += 1
            else:
                fail += 1
            print()
    except (KeyboardInterrupt, DownloadCancelled):
        finish_bar()
        print()
        log(WARN, "Interrupted by user")
        print(f"        {dim('finished files are kept; re-run to resume')}")
        return (ok, fail)

    elapsed = time.time() - started
    print(f"        {dim(f'total {elapsed:.1f}s')}")
    return (ok, fail)


# ---------------------------------------------------------------------------
# Admin / VLC helpers
# ---------------------------------------------------------------------------

def check_admin() -> bool:
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def check_vlc(vlc_dir: Path) -> bool:
    return (vlc_dir / "vlc.exe").is_file()


def vlc_running() -> list[int]:
    import subprocess
    try:
        out = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq vlc.exe", "/NH", "/FO", "CSV"],
            capture_output=True, text=True, timeout=10,
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
    import subprocess
    pids = vlc_running()
    if not pids:
        return 0
    try:
        subprocess.run(["taskkill", "/IM", "vlc.exe", "/F"],
                       capture_output=True, timeout=10)
    except Exception:
        pass
    return len(pids)


# ---------------------------------------------------------------------------
# Install
# ---------------------------------------------------------------------------

def check_repo_files() -> tuple[list[Path], list[str]]:
    found, missing = [], []
    for n in BINARIES:
        p = FILES_DIR / n
        if p.is_file():
            found.append(p)
        else:
            missing.append(f"dlss-files/{n}")
    for n in OPTIONAL_BINARIES:
        p = FILES_DIR / n
        if p.is_file():
            found.append(p)
    for n in SHADERS:
        p = SHADERS_DIR / n
        if p.is_file():
            found.append(p)
        else:
            missing.append(f"reshade-shaders/Shaders/{n}")
    for n in SHADER_HEADERS:
        p = SHADERS_DIR / n
        if p.is_file():
            found.append(p)
    for n in SHADER_INCLUDES:
        p = INCLUDE_DIR / n
        if p.is_file():
            found.append(p)
    for n in TEXTURES:
        p = TEXTURES_DIR / n
        if p.is_file():
            found.append(p)
    return found, missing


def check_placeholders(files: list[Path]) -> list[str]:
    bad = []
    for p in files:
        try:
            if p.stat().st_size < 20:
                bad.append(f"{p.name} ({p.stat().st_size} bytes)")
        except OSError:
            pass
    return bad


def clean_vlc(vlc_dir: Path) -> list[str]:
    removed = []
    for name in DELETE_ROOT:
        p = vlc_dir / name
        if p.is_file():
            try:
                p.unlink()
                removed.append(name)
            except OSError as e:
                log(WARN, f"could not delete {name}: {e}")
        for suf in DELETE_BACKUP_SUFFIXES:
            b = p.with_suffix(p.suffix + suf)
            if b.is_file():
                try:
                    b.unlink()
                    removed.append(b.name)
                except OSError:
                    pass
    shader_dir = vlc_dir / "reshade-shaders" / "Shaders"
    include_dir = shader_dir / "include"
    texture_dir = vlc_dir / "reshade-shaders" / "Textures"
    for name in DELETE_SHADERS + DELETE_HEADERS:
        p = shader_dir / name
        if p.is_file():
            try:
                p.unlink()
                removed.append(f"reshade-shaders/Shaders/{name}")
            except OSError:
                pass
    for name in DELETE_INCLUDES:
        p = include_dir / name
        if p.is_file():
            try:
                p.unlink()
                removed.append(f"reshade-shaders/Shaders/include/{name}")
            except OSError:
                pass
    for name in DELETE_TEXTURES:
        p = texture_dir / name
        if p.is_file():
            try:
                p.unlink()
                removed.append(f"reshade-shaders/Textures/{name}")
            except OSError:
                pass
    return removed


def install(vlc_dir: Path, files: list[Path]) -> list[str]:
    shader_dir = vlc_dir / "reshade-shaders" / "Shaders"
    include_dir = shader_dir / "include"
    texture_dir = vlc_dir / "reshade-shaders" / "Textures"
    shader_dir.mkdir(parents=True, exist_ok=True)
    include_dir.mkdir(parents=True, exist_ok=True)
    texture_dir.mkdir(parents=True, exist_ok=True)

    installed: list[str] = []
    total = len(files)
    print()

    for i, src in enumerate(files, 1):
        name = src.name
        if name in SHADERS or name in SHADER_HEADERS:
            dst = shader_dir / name
            rel = f"reshade-shaders/Shaders/{name}"
        elif name in SHADER_INCLUDES:
            dst = include_dir / name
            rel = f"reshade-shaders/Shaders/include/{name}"
        elif name in TEXTURES:
            dst = texture_dir / name
            rel = f"reshade-shaders/Textures/{name}"
        else:
            dst = vlc_dir / name
            rel = name

        try:
            size = src.stat().st_size
            if size > 5 * 1024 * 1024:
                started = time.time()
                progress_bar(0, size, f"[{i}/{total}] {name}", started=started)
                shutil.copy2(src, dst)
                progress_bar(size, size, f"[{i}/{total}] {name}", started=started)
                finish_bar()
            else:
                shutil.copy2(src, dst)

            print(f"        {green('+')} {rel}  {dim(_fmt_bytes(dst.stat().st_size))}")
            installed.append(rel)
        except OSError as e:
            finish_bar()
            log(FAIL, f"copy {name}: {e}")

    return installed


def verify(vlc_dir: Path) -> tuple[int, int]:
    present, missing = 0, 0
    checks: list[tuple[Path, str]] = []
    for n in BINARIES:
        checks.append((vlc_dir / n, n))
    shader_dir = vlc_dir / "reshade-shaders" / "Shaders"
    for n in SHADERS:
        checks.append((shader_dir / n, f"reshade-shaders/Shaders/{n}"))
    for n in SHADER_HEADERS:
        p = shader_dir / n
        if p.is_file() and p.stat().st_size > 20:
            present += 1
    for p, label in checks:
        if p.is_file() and p.stat().st_size > 20:
            present += 1
        else:
            log(WARN, f"missing: {label}")
            missing += 1
    return present, missing


# ---------------------------------------------------------------------------
# Uninstall
# ---------------------------------------------------------------------------

def uninstall(vlc_dir: Path, also_all: bool) -> int:
    print()
    log(INFO, f"Uninstalling DLSS5 / ReShade from {vlc_dir}")
    killed = kill_vlc()
    if killed:
        log(OK, f"Stopped {killed} running vlc.exe process(es)")
    else:
        log(INFO, "VLC not running")

    removed = []
    for name in DELETE_ROOT:
        p = vlc_dir / name
        if p.is_file():
            try:
                p.unlink()
                removed.append(name)
            except OSError as e:
                log(WARN, f"could not delete {name}: {e}")
        for suf in DELETE_BACKUP_SUFFIXES:
            b = p.with_suffix(p.suffix + suf)
            if b.is_file():
                try:
                    b.unlink()
                    removed.append(b.name)
                except OSError:
                    pass

    shader_dir = vlc_dir / "reshade-shaders" / "Shaders"
    include_dir = shader_dir / "include"
    texture_dir = vlc_dir / "reshade-shaders" / "Textures"
    for name in DELETE_SHADERS + DELETE_HEADERS:
        p = shader_dir / name
        if p.is_file():
            try:
                p.unlink()
                removed.append(f"reshade-shaders/Shaders/{name}")
            except OSError:
                pass
    for name in DELETE_INCLUDES:
        p = include_dir / name
        if p.is_file():
            try:
                p.unlink()
                removed.append(f"reshade-shaders/Shaders/include/{name}")
            except OSError:
                pass
    for name in DELETE_TEXTURES:
        p = texture_dir / name
        if p.is_file():
            try:
                p.unlink()
                removed.append(f"reshade-shaders/Textures/{name}")
            except OSError:
                pass

    if also_all:
        for folder in DELETE_FOLDERS:
            d = vlc_dir / folder
            if d.is_dir():
                try:
                    shutil.rmtree(d)
                    removed.append(f"{folder}/  (folder)")
                except OSError as e:
                    log(WARN, f"could not remove {folder}: {e}")
        if VLC_USER_CONFIG.is_file():
            try:
                VLC_USER_CONFIG.unlink()
                removed.append(f"{VLC_USER_CONFIG}  (vlcrc reset)")
            except OSError as e:
                log(WARN, f"could not remove vlcrc: {e}")

    print()
    if not removed:
        log(INFO, "Nothing to remove — VLC was already clean")
    else:
        for n in removed:
            print(f"        {red('-')} {n}")
        log(OK, f"Removed {len(removed)} item(s)")

    print()
    print("=" * 72)
    print("  Uninstall complete.")
    if also_all:
        print("  reshade-shaders\\ folder and vlcrc were also removed.")
    else:
        print("  reshade-shaders\\ folder left in place.")
        print("  Run with --uninstall-all to remove it too.")
    print("=" * 72)
    return 0


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(
        description="Install or uninstall DLSS5 Feeder + ReShade for VLC.")
    ap.add_argument("--vlc-dir", type=Path, default=VLC_DIR_DEFAULT)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--uninstall", action="store_true")
    ap.add_argument("--uninstall-all", action="store_true")
    ap.add_argument("--fetch", action="store_true")
    ap.add_argument("--force-fetch", action="store_true")
    ap.add_argument("--skip-big", action="store_true")
    args = ap.parse_args()

    vlc_dir = args.vlc_dir
    print("=" * 72)
    mode = ("uninstall" if (args.uninstall or args.uninstall_all)
            else "fetch" if args.fetch else "install")
    print(f"  {bold('DLSS5 Feeder')}  --  {mode}  into VLC")
    print("=" * 72)
    print()
    log(INFO, f"Repo root : {REPO_DIR}")
    log(INFO, f"VLC folder: {vlc_dir}")
    print()

    if not check_admin():
        log(WARN, "Not running as Administrator — writes to Program Files "
                  "will likely fail.")
    else:
        log(OK, "Running as Administrator")

    # Uninstall branch
    if args.uninstall or args.uninstall_all:
        if not check_vlc(vlc_dir):
            log(FAIL, f"vlc.exe not found in {vlc_dir}")
            return 2
        log(OK, "vlc.exe found")
        if args.dry_run:
            print()
            log(INFO, "Dry run — would remove:")
            for n in DELETE_ROOT:
                p = vlc_dir / n
                if p.is_file():
                    print(f"        {p}")
            print()
            log(INFO, "Dry run — stopping here")
            return 0
        return uninstall(vlc_dir, args.uninstall_all)

    # Install/fetch branch
    if not check_vlc(vlc_dir):
        log(FAIL, f"vlc.exe not found in {vlc_dir}")
        return 2
    log(OK, "vlc.exe found")

    files, missing = check_repo_files()
    if args.skip_big:
        missing = [m for m in missing
                   if not any(b in m for b in BIG_FILES)]

    if missing or args.force_fetch:
        if missing:
            log(INFO, f"{len(missing)} file(s) missing — fetching")
        else:
            log(INFO, "Force-fetch: re-downloading every file")
        ok, fail = fetch_all(force=args.force_fetch, skip_big=args.skip_big)
        print()
        if fail == 0:
            log(OK, f"fetch: {ok} OK, 0 failed")
        else:
            log(WARN, f"fetch: {ok} OK, {fail} failed")
        files, missing = check_repo_files()
        if args.skip_big:
            missing = [m for m in missing
                       if not any(b in m for b in BIG_FILES)]

    if missing:
        log(FAIL, "Still missing required files:")
        for n in missing:
            print(f"        - {n}")
        print()
        print("  Some sources failed. Check your internet connection,")
        print("  or place the files manually:")
        print("    dlss-files/                <- binaries + config")
        print("    reshade-shaders/Shaders/   <- .fx and .fxh files")
        print("    reshade-shaders/Shaders/include/")
        print("    reshade-shaders/Textures/")
        return 3

    log(OK, f"All required repo files present ({len(files)} files)")

    bad = check_placeholders(files)
    if bad:
        log(FAIL, "These repo files are tiny placeholders:")
        for n in bad:
            print(f"        - {n}")
        log(INFO, "Re-run with --force-fetch to replace them")
        return 4
    log(OK, "No placeholder files in repo")

    if args.fetch:
        print()
        print("=" * 72)
        print("  Fetch complete. Run without --fetch to install into VLC.")
        print("=" * 72)
        return 0

    if args.dry_run:
        print()
        log(INFO, "Dry run — stopping before clean+install")
        return 0

    killed = kill_vlc()
    if killed:
        log(OK, f"Stopped {killed} running vlc.exe process(es)")

    print()
    log(INFO, f"Cleaning previous install in {vlc_dir}")
    removed = clean_vlc(vlc_dir)
    if removed:
        for n in removed:
            print(f"        {red('-')} {n}")
    else:
        log(INFO, "nothing to clean (fresh install)")

    print()
    log(INFO, f"Installing into {vlc_dir}")
    installed = install(vlc_dir, files)
    if not installed:
        log(FAIL, "Nothing was installed")
        return 5

    print()
    log(INFO, "Verifying")
    present, missing_after = verify(vlc_dir)

    print()
    print("=" * 72)
    if missing_after:
        print(f"  {yellow(f'{present} OK')}   {red(f'{missing_after} MISSING')}")
        print("  Some files failed to install. Re-run as Administrator.")
        print("=" * 72)
        return 1
    else:
        print(f"  {green(f'{present} files installed and verified.')}")
        print()
        print("  Next steps:")
        print(f"    1. python scripts\\vlc-shader.py "
              f"--video \"D:\\path\\to\\video.mp4\"")
        print("    2. Press HOME in VLC to open the ReShade overlay")
        print()
        print("  Uninstall later with:")
        print("    python scripts\\install.py --uninstall")
        print("=" * 72)
        return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        finish_bar()
        print()
        print(yellow("[*] stopped."))
        sys.exit(130)