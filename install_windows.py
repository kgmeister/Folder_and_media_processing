#!/usr/bin/env python3
r"""
Installs Secure Media Processor as a normal Windows app for the current user (no admin rights):

  * %LOCALAPPDATA%\Programs\Secure Media Processor\  - the app, plus a private Python environment (.venv)
  * Start Menu and Desktop shortcuts "Secure Media Processor" with the app icon

Why not just the .exe? Windows 11 Smart App Control blocks unsigned programs, and a self-built .exe is
unsigned. The shortcuts start the app through Python's own pythonw.exe, which is signed by the Python
Software Foundation, so the app opens even with Smart App Control on. ("Check for updates" in the app
keeps this install current.)

    python install_windows.py              install or repair
    python install_windows.py --uninstall  remove the app, its shortcuts and its private Python
                                           (settings, logs and the installed ffmpeg are kept)
"""
import argparse, os, shutil, subprocess, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
APP_NAME = "Secure Media Processor"
TARGET = Path(os.environ["LOCALAPPDATA"]) / "Programs" / APP_NAME
RUNTIME_FILES = ["app.py", "media_core.py", "secure_media_processor.py", "updater.py", "tools_installer.py",
                 "requirements.txt", "README.md", "assets/icon.ico", "assets/icon.png"]

def shortcut_paths():
    appdata = Path(os.environ["APPDATA"])
    start_menu = appdata / "Microsoft" / "Windows" / "Start Menu" / "Programs" / f"{APP_NAME}.lnk"
    desktop = Path(subprocess.run(["powershell", "-NoProfile", "-Command", "[Environment]::GetFolderPath('Desktop')"],
                                  capture_output=True, text=True).stdout.strip() or Path.home() / "Desktop")
    return [start_menu, desktop / f"{APP_NAME}.lnk"]

def make_shortcut(lnk: Path, target: Path, args: str, icon: Path, workdir: Path):
    lnk.parent.mkdir(parents=True, exist_ok=True)
    ps = ("$s = (New-Object -ComObject WScript.Shell).CreateShortcut($env:SMP_LNK); "
          "$s.TargetPath = $env:SMP_TARGET; $s.Arguments = $env:SMP_ARGS; $s.IconLocation = $env:SMP_ICON; "
          "$s.WorkingDirectory = $env:SMP_WORKDIR; $s.Description = 'Secure Media Processor'; $s.Save()")
    env = dict(os.environ, SMP_LNK=str(lnk), SMP_TARGET=str(target), SMP_ARGS=args, SMP_ICON=f"{icon},0",
               SMP_WORKDIR=str(workdir))
    subprocess.run(["powershell", "-NoProfile", "-Command", ps], env=env, check=True)

def install():
    print(f"Installing to {TARGET}")
    TARGET.mkdir(parents=True, exist_ok=True)
    for rel in RUNTIME_FILES:
        (TARGET / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / rel, TARGET / rel)
    venv = TARGET / ".venv"
    if not (venv / "Scripts" / "python.exe").exists():
        print("Creating the app's private Python environment...")
        subprocess.run([sys.executable, "-m", "venv", str(venv)], check=True)
    print("Installing the app's libraries (Pillow, ImageHash, mozjpeg, Flet)...")
    reqs = [l for l in (ROOT / "requirements.txt").read_text().splitlines() if l.strip() and not l.startswith("#")
            and not l.startswith("pytest")]
    subprocess.run([str(venv / "Scripts" / "python.exe"), "-m", "pip", "install", "--disable-pip-version-check",
                    "-q", *reqs], check=True)
    # Tell the updater which version this is and which files it owns.
    commit = subprocess.run(["git", "-C", str(ROOT), "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
    if commit:
        (TARGET / ".installed_version").write_text(commit + "\n", encoding="utf-8")
    (TARGET / ".installed_files").write_text("\n".join(RUNTIME_FILES) + "\n", encoding="utf-8")
    for lnk in shortcut_paths():
        make_shortcut(lnk, venv / "Scripts" / "pythonw.exe", f'"{TARGET / "app.py"}"', TARGET / "assets" / "icon.ico", TARGET)
        print(f"Shortcut: {lnk}")
    print(f"\nDone. Start '{APP_NAME}' from the Start Menu or the Desktop.")

def uninstall():
    for lnk in shortcut_paths():
        if lnk.exists():
            lnk.unlink(); print(f"Removed {lnk}")
    if TARGET.exists():
        shutil.rmtree(TARGET); print(f"Removed {TARGET}")
    print("Settings, logs and the installed ffmpeg are kept in %LOCALAPPDATA%\\SecureMediaProcessor.")

if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=f"Install {APP_NAME} for the current user.")
    ap.add_argument("--uninstall", action="store_true")
    uninstall() if ap.parse_args().uninstall else install()
