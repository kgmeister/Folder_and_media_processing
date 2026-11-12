"""
Installs ffmpeg + ffprobe for the app, per user, without admin rights and without touching PATH:
they go into %LOCALAPPDATA%\\SecureMediaProcessor\\tools, which media_core.find_tools() searches.

Source: the official gyan.dev "essentials" release build 8.0.1, from its GitHub mirror (GyanD/codexffmpeg).
It is pinned to that exact file on purpose. ffmpeg builds are not code-signed, and Windows 11 Smart App Control
only lets unsigned programs run when the exact file has a good reputation. A long-lived, widely downloaded
release qualifies; daily builds (e.g. BtbN's) are new files every day and get blocked. The download is checked
against the pinned sha256 below, which matches GitHub's published digest for that release asset.
Network access happens only when the user asks for the install.
"""
import hashlib, shutil, sys, tempfile, urllib.error, urllib.request, zipfile
from pathlib import Path
from typing import Callable, Optional

import media_core as core

VERSION = "8.0.1"
ZIP_URL = f"https://github.com/GyanD/codexffmpeg/releases/download/{VERSION}/ffmpeg-{VERSION}-essentials_build.zip"
ZIP_SHA256 = "e2aaeaa0fdbc397d4794828086424d4aaa2102cef1fb6874f6ffd29c0b88b673"
APPROX_DOWNLOAD_MB = 106
WANTED = ("ffmpeg.exe", "ffprobe.exe")                 # static builds: no DLLs needed; ffplay is not needed

class InstallError(Exception):
    pass

def supported() -> bool:
    return sys.platform == "win32"

def _get(url: str, dest: Path, progress: Optional[Callable[[int, int], None]] = None):
    req = urllib.request.Request(url, headers={"User-Agent": "SecureMediaProcessor-tools"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            total, done = int(r.headers.get("Content-Length") or 0), 0
            with open(dest, "wb") as f:
                while chunk := r.read(1 << 20):
                    f.write(chunk); done += len(chunk)
                    if progress: progress(done, total)
    except (urllib.error.URLError, TimeoutError) as e:
        raise InstallError(f"Download failed: {getattr(e, 'reason', e)}")

def install_ffmpeg(progress: Optional[Callable[[int, int], None]] = None, target: Path = None) -> str:
    """Download, verify and unpack ffmpeg/ffprobe. Returns a message; raises InstallError."""
    if not supported():
        raise InstallError("Automatic ffmpeg install is only available on Windows.")
    target = target or core.TOOLS_DIR
    with tempfile.TemporaryDirectory(prefix="smp_ffmpeg_") as tmp:
        archive = Path(tmp) / "ffmpeg.zip"
        _get(ZIP_URL, archive, progress)
        h = hashlib.sha256()
        with open(archive, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""): h.update(chunk)
        if h.hexdigest() != ZIP_SHA256:
            raise InstallError("The download does not match the expected checksum; nothing was installed.")
        staging = Path(tmp) / "bin"; staging.mkdir()
        with zipfile.ZipFile(archive) as zf:
            for info in zf.infolist():
                parts = Path(info.filename).parts
                if len(parts) >= 2 and parts[-2] == "bin" and parts[-1].lower() in WANTED:
                    (staging / parts[-1].lower()).write_bytes(zf.read(info))
        if not all((staging / n).exists() for n in WANTED):
            raise InstallError("The download did not contain ffmpeg.exe and ffprobe.exe; nothing was installed.")
        ok, detail = core.tool_runs(str(staging / "ffmpeg.exe"))
        if not ok:
            raise InstallError(f"{detail} Nothing was installed.")
        core.ensure_dir(target)
        for n in WANTED:
            shutil.copy2(staging / n, target / n)
    return f"Installed ffmpeg {VERSION} (gyan.dev essentials build) to {target}"
