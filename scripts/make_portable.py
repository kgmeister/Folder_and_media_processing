#!/usr/bin/env python3
r"""
Builds the portable Windows app: a folder you copy anywhere (another drive, a USB stick) and double-click.
Nothing is installed, and nothing is written outside the folder.

    python scripts/make_portable.py          -> dist\SecureMediaProcessor\
    python scripts/make_portable.py --zip    -> also dist\SecureMediaProcessor-portable.zip

Layout:
    Secure Media Processor.cmd   double-click to start
    app\                         the app: a copy of the repository's src\ folder
    runtime\                     a private copy of this Python (official python.org files, signed by the
                                 Python Software Foundation) plus the libraries the app needs
    data\                        settings, logs and ffmpeg: everything the app writes

Why a signed Python and a .cmd launcher instead of an .exe: Windows 11 Smart App Control blocks unsigned
programs, and any .exe we build ourselves is unsigned. pythonw.exe is signed, so the app runs.
Build it with the same Python version you want inside (3.12 here); the libraries are installed for it.
"""
import argparse, shutil, subprocess, sys, time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"                     # the app; copied as-is to app\ (the updater mirrors the same folder)
OUT = ROOT / "dist" / "SecureMediaProcessor"
# Parts of the Python install the app never uses (GUI toolkit, tests, IDE, docs, the build machine's packages).
SKIP_TOP = {"Scripts", "Doc", "Tools", "tcl", "include", "libs", "share", "NEWS.txt"}
SKIP_LIB = {"site-packages", "test", "idlelib", "tkinter", "turtledemo", "__pycache__", "ensurepip", "lib2to3"}
SKIP_DLL = ("tcl", "tk", "_tkinter")
LAUNCHER = """@echo off
rem Secure Media Processor - portable. Starts the app from this folder; nothing is installed.
start "" "%~dp0runtime\\pythonw.exe" -E -s "%~dp0app\\app.py" %*
"""

def copy_runtime(dest: Path):
    base = Path(sys.base_prefix)
    for item in base.iterdir():
        if item.name in SKIP_TOP:
            continue
        if item.is_file():
            shutil.copy2(item, dest / item.name)
        elif item.name == "DLLs":
            (dest / "DLLs").mkdir()
            for f in item.iterdir():
                if not f.name.lower().startswith(SKIP_DLL):
                    shutil.copy2(f, dest / "DLLs" / f.name)
        elif item.name == "Lib":
            shutil.copytree(item, dest / "Lib", ignore=lambda d, names: [n for n in names if n in SKIP_LIB])
            (dest / "Lib" / "site-packages").mkdir()

def main() -> int:
    ap = argparse.ArgumentParser(description="Build the portable Secure Media Processor folder.")
    ap.add_argument("--zip", action="store_true", help="also create dist/SecureMediaProcessor-portable.zip")
    args = ap.parse_args()
    if OUT.exists():
        shutil.rmtree(OUT)
    (OUT / "runtime").mkdir(parents=True); (OUT / "data").mkdir()

    print(f"Copying Python {sys.version.split()[0]} from {sys.base_prefix} ...")
    copy_runtime(OUT / "runtime")

    print("Installing the app's libraries into runtime\\Lib\\site-packages ...")
    reqs = [l for l in (SRC / "requirements.txt").read_text().splitlines() if l.strip() and not l.startswith("#")]
    subprocess.run([sys.executable, "-m", "pip", "install", "--disable-pip-version-check", "-q", "--only-binary=:all:",
                    "--target", str(OUT / "runtime" / "Lib" / "site-packages"), "pip", *reqs], check=True)

    # Optional native speed-ups that Smart App Control blocks (unsigned, little reputation). Each package falls
    # back to pure Python without them; removing them avoids a "Part of this app has been blocked" pop-up.
    for f in (OUT / "runtime" / "Lib" / "site-packages" / "msgpack").glob("_cmsgpack*.pyd"):
        f.unlink()
    # pip's console-script launchers (pip.exe, flet.exe, ...): never used (everything runs as python -m), unsigned,
    # and regenerated with new bytes on every build, which would add ~1 MB of noise to the repo per rebuild.
    site = OUT / "runtime" / "Lib" / "site-packages"
    shutil.rmtree(site / "bin", ignore_errors=True)
    for record in site.glob("*.dist-info/RECORD"):        # and drop them from pip's file lists, so a rebuild is identical
        lines = record.read_bytes().decode("utf-8").splitlines(keepends=True)
        record.write_bytes("".join(l for l in lines if not l.startswith("../../bin/")).encode("utf-8"))

    tracked = subprocess.run(["git", "-C", str(SRC), "ls-files", "-z", "."], capture_output=True, text=True).stdout
    app_files = sorted(f for f in tracked.split("\0") if f) or sorted(
        p.relative_to(SRC).as_posix() for p in SRC.rglob("*") if p.is_file() and "__pycache__" not in p.parts)
    for rel in app_files:                  # only committed files: never a local update_config.json or cache
        (OUT / "app" / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(SRC / rel, OUT / "app" / rel)
    commit = subprocess.run(["git", "-C", str(ROOT), "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
    if commit:
        (OUT / "app" / ".installed_version").write_text(commit + "\n", encoding="utf-8")    # for "Check for updates"
    (OUT / "app" / ".installed_files").write_text("\n".join(app_files) + "\n", encoding="utf-8")
    (OUT / "Secure Media Processor.cmd").write_text(LAUNCHER, encoding="ascii")
    (OUT / "data" / "README.txt").write_text("Settings, run logs and the app's ffmpeg are kept here.\n", encoding="ascii")

    print("Checking that the portable Python starts and finds every library ...")
    r = subprocess.run([str(OUT / "runtime" / "python.exe"), "-E", "-s", "-c",
                        "import sys, flet, PIL, imagehash, mozjpeg_lossless_optimization; "
                        "sys.path.insert(0, sys.argv[1]); import media_core as c; "
                        "print(sys.prefix, '| portable data:', c.APP_DATA)", str(OUT / "app")],
                       capture_output=True, text=True)
    print(r.stdout.strip() or r.stderr.strip()[-800:])
    if r.returncode != 0:
        print("Portable runtime check FAILED.")
        return 1
    size = sum(f.stat().st_size for f in OUT.rglob("*") if f.is_file())
    print(f"Built {OUT} ({size / 1e6:.0f} MB) from commit {commit[:7] or 'unknown'}")
    if args.zip:
        z = shutil.make_archive(str(ROOT / "dist" / "SecureMediaProcessor-portable"), "zip", OUT.parent, OUT.name)
        print(f"Zipped to {z}")
    return 0

if __name__ == "__main__":
    t = time.time(); code = main(); print(f"({time.time() - t:.0f}s)"); sys.exit(code)
