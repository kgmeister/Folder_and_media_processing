#!/usr/bin/env python3
"""
Manual updater for the Secure Media Processor.

Network access happens ONLY when the user presses "Check for updates" / "Update"
(or runs `secure_media_processor.py --update`). Nothing is sent except the
request for the latest version of this repository.

Credentials: none are needed while the repository is public. If it is made
private, put a read-only GitHub token in update_config.json next to this file:
    {"github_token": "github_pat_..."}
That file is git-ignored, so the token is never committed or pushed.

Three install types:
  * packaged app (SecureMediaProcessor.exe): downloads the exe from the latest
    GitHub release (built by .github/workflows/build-windows.yml on every push),
    checks its sha256, and swaps it in. Restart to use it.
  * git checkout (a .git folder next to this file): `git pull --ff-only`.
    Refuses to run over uncommitted changes and never rewrites local commits.
  * plain folder: downloads the latest snapshot, checks that every .py file
    compiles, backs up the files it replaces to .update_backup/<time>/, then
    copies the new files in. Files deleted upstream are moved to the backup.
"""
import hashlib, json, os, py_compile, re, shutil, subprocess, sys, tempfile, time, urllib.error, urllib.request, zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

REPO = "kgmeister/Folder_and_media_processing"
BRANCH = "main"
FROZEN = getattr(sys, "frozen", False)               # running as the packaged .exe
EXE_ASSET = "SecureMediaProcessor.exe"
APP_DIR = Path(__file__).resolve().parent
# The packaged app keeps its config next to the exe (its own files live in a temporary unpack folder).
CONFIG_FILE = (Path(sys.executable).resolve().parent if FROZEN else APP_DIR) / "update_config.json"
VERSION_FILE = APP_DIR / ".installed_version"      # plain-folder installs: commit sha now installed
MANIFEST_FILE = APP_DIR / ".installed_files"       # plain-folder installs: files the last update wrote
BACKUP_DIR = APP_DIR / ".update_backup"
# Never touched by an update: local config/state, bundled binaries, logs, git internals.
PRESERVE = {"update_config.json", ".installed_version", ".installed_files", ".update_backup", ".git", "tools", ".venv"}

class UpdateError(Exception):
    pass

@dataclass
class UpdateInfo:
    local: Optional[str]       # commit sha currently installed (None = unknown)
    remote: str                # latest commit sha on the branch
    remote_date: str
    remote_message: str
    local_ahead: bool = False  # git checkout whose HEAD already contains the remote commit (unpushed work)
    download_url: str = ""     # packaged app: the release's exe
    digest: str = ""           # packaged app: "sha256:<hex>" published by GitHub for that exe

    @property
    def up_to_date(self) -> bool:
        return self.local_ahead or (self.local is not None and self.local == self.remote)

def _token() -> Optional[str]:
    try:
        return json.loads(CONFIG_FILE.read_text(encoding="utf-8")).get("github_token") or None
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as e:
        raise UpdateError(f"Could not read {CONFIG_FILE.name}: {e}")

def _get(url: str, accept: str = "application/vnd.github+json") -> bytes:
    req = urllib.request.Request(url, headers={"Accept": accept, "User-Agent": "SecureMediaProcessor-updater"})
    tok = _token()
    if tok:
        req.add_header("Authorization", f"Bearer {tok}")
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.read()
    except urllib.error.HTTPError as e:
        hint = " (private repository? add a token to update_config.json)" if e.code in (401, 403, 404) else ""
        raise UpdateError(f"GitHub returned HTTP {e.code}{hint}")
    except (urllib.error.URLError, TimeoutError) as e:
        raise UpdateError(f"Could not reach GitHub: {getattr(e, 'reason', e)}")

def _git(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(APP_DIR), *args], capture_output=True, text=True, timeout=300)

def is_git_checkout() -> bool:
    return (APP_DIR / ".git").exists() and shutil.which("git") is not None

def build_commit() -> Optional[str]:
    """Commit the packaged exe was built from (written into _build_info.py by build_exe.py)."""
    try:
        import _build_info
        return _build_info.COMMIT or None
    except ImportError:
        return None

def local_version() -> Optional[str]:
    if FROZEN:
        return build_commit()
    if is_git_checkout():
        r = _git("rev-parse", "HEAD")
        return r.stdout.strip() if r.returncode == 0 else None
    try:
        return VERSION_FILE.read_text(encoding="utf-8").strip() or None
    except OSError:
        return None

def _check_release() -> UpdateInfo:
    try:
        rel = json.loads(_get(f"https://api.github.com/repos/{REPO}/releases/latest"))
    except UpdateError as e:
        if "HTTP 404" in str(e):
            raise UpdateError("No app build has been published on GitHub yet (it is built automatically after a push).")
        raise
    m = re.search(r"commit:\s*([0-9a-f]{40})", rel.get("body") or "")
    asset = next((a for a in rel.get("assets", []) if a.get("name") == EXE_ASSET), None)
    if not m or asset is None:
        raise UpdateError(f"The latest release ({rel.get('tag_name')}) has no {EXE_ASSET} or commit id.")
    return UpdateInfo(build_commit(), m.group(1), rel.get("published_at") or "", rel.get("name") or rel.get("tag_name") or "",
                      download_url=asset["browser_download_url"], digest=asset.get("digest") or "")

def check() -> UpdateInfo:
    if FROZEN:
        return _check_release()
    data = json.loads(_get(f"https://api.github.com/repos/{REPO}/commits/{BRANCH}"))
    commit = data["commit"]
    local = local_version()
    ahead = (is_git_checkout() and local is not None and local != data["sha"]
             and _git("merge-base", "--is-ancestor", data["sha"], local).returncode == 0)
    return UpdateInfo(local, data["sha"], commit["committer"]["date"],
                      commit["message"].splitlines()[0] if commit["message"] else "", ahead)

def _remote_url() -> str:
    tok = _token()
    return f"https://x-access-token:{tok}@github.com/{REPO}.git" if tok else f"https://github.com/{REPO}.git"

def _apply_git() -> str:
    dirty = _git("status", "--porcelain", "--untracked-files=no")
    if dirty.returncode != 0:
        raise UpdateError(f"git status failed: {dirty.stderr.strip()}")
    if dirty.stdout.strip():
        raise UpdateError("This folder has uncommitted changes. Commit or discard them, then update again.")
    before = local_version()
    # The URL is passed on the command line, so a token is never written into .git/config.
    r = _git("pull", "--ff-only", _remote_url(), BRANCH)
    if r.returncode != 0:
        msg = (r.stderr or r.stdout).strip().replace(_token() or "\0", "***")
        raise UpdateError(f"git pull failed (local commits not on GitHub yet?): {msg[-400:]}")
    after = local_version()
    return "Already up to date." if before == after else f"Updated {before[:7]} -> {after[:7]}."

def _apply_snapshot(info: UpdateInfo) -> str:
    with tempfile.TemporaryDirectory(prefix="smp_update_") as tmp:
        tmp = Path(tmp)
        archive = tmp / "update.zip"
        archive.write_bytes(_get(f"https://api.github.com/repos/{REPO}/zipball/{info.remote}", "application/vnd.github+json"))
        with zipfile.ZipFile(archive) as zf:
            zf.extractall(tmp / "x")                     # zipfile drops absolute paths and '..' parts
        tops = [p for p in (tmp / "x").iterdir() if p.is_dir()]
        if len(tops) != 1:
            raise UpdateError("Unexpected archive layout from GitHub")
        new_root = tops[0]
        new_files = sorted(p.relative_to(new_root) for p in new_root.rglob("*")
                           if p.is_file() and p.relative_to(new_root).parts[0] not in PRESERVE)
        for rel in new_files:                            # refuse a snapshot that doesn't even compile
            if rel.suffix == ".py":
                try:
                    py_compile.compile(str(new_root / rel), cfile=str(tmp / "check.pyc"), doraise=True)
                except py_compile.PyCompileError as e:
                    raise UpdateError(f"Downloaded {rel} does not compile; nothing was changed. {e.msg}")
        backup = BACKUP_DIR / time.strftime("%Y%m%d_%H%M%S")
        try:
            old_files = {Path(l) for l in MANIFEST_FILE.read_text(encoding="utf-8").splitlines() if l}
        except OSError:
            old_files = set()
        for rel in new_files:
            dst = APP_DIR / rel
            if dst.exists():
                (backup / rel).parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(dst, backup / rel)
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(new_root / rel, dst)
        removed = 0
        for rel in sorted(old_files - set(new_files)):   # deleted upstream since the last update
            if (APP_DIR / rel).is_file() and rel.parts[0] not in PRESERVE:
                (backup / rel).parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(APP_DIR / rel), str(backup / rel))
                removed += 1
        MANIFEST_FILE.write_text("\n".join(str(p) for p in new_files) + "\n", encoding="utf-8")
        VERSION_FILE.write_text(info.remote + "\n", encoding="utf-8")
    note = f", {removed} removed" if removed else ""
    note += _refresh_libraries(backup)
    where = f" Previous files backed up to {backup}." if backup.exists() else ""
    return f"Installed {info.remote[:7]} ({len(new_files)} files{note}).{where}"

def _download(url: str, dest: Path):
    req = urllib.request.Request(url, headers={"User-Agent": "SecureMediaProcessor-updater"})
    tok = _token()
    if tok:
        req.add_header("Authorization", f"Bearer {tok}")
    try:
        with urllib.request.urlopen(req, timeout=60) as r, open(dest, "wb") as f:
            shutil.copyfileobj(r, f, 1 << 20)
    except (urllib.error.URLError, TimeoutError) as e:
        raise UpdateError(f"Download failed: {getattr(e, 'reason', e)}")

def _apply_exe(info: UpdateInfo, exe: Optional[Path] = None) -> str:
    """Download the release exe next to the running one, verify it, then swap: Windows lets a running exe be
    renamed, so the current one becomes .old (removed on the next start) and the new one takes its name."""
    exe = exe or Path(sys.executable).resolve()
    new, old = exe.with_name(exe.name + ".download"), exe.with_name(exe.name + ".old")
    try:
        _download(info.download_url, new)
        if info.digest.startswith("sha256:"):
            h = hashlib.sha256()
            with open(new, "rb") as f:
                for chunk in iter(lambda: f.read(1 << 20), b""): h.update(chunk)
            if h.hexdigest() != info.digest.split(":", 1)[1].lower():
                raise UpdateError("The download does not match GitHub's published checksum; nothing was changed.")
        with open(new, "rb") as f:
            if f.read(2) != b"MZ":
                raise UpdateError("The download is not a Windows program; nothing was changed.")
        if old.exists():
            old.unlink()
        exe.rename(old)
        try:
            new.rename(exe)
        except OSError:
            old.rename(exe)                              # put the working version back
            raise
    except OSError as e:
        raise UpdateError(f"Could not replace {exe.name}: {e}")
    finally:
        if new.exists():
            new.unlink()
    return f"Installed build {info.remote[:7]} ({info.remote_message})."

def cleanup_old_exe():
    """Remove the previous exe left behind by an update (packaged app only)."""
    if FROZEN:
        old = Path(sys.executable).resolve()
        old = old.with_name(old.name + ".old")
        try:
            old.unlink()
        except OSError:
            pass

def restart():
    """Start a fresh copy of the app (packaged exe, or python + this app) and end this one."""
    args = [sys.executable] if FROZEN else [sys.executable, str(APP_DIR / "app.py")]
    flags = {"creationflags": subprocess.DETACHED_PROCESS} if os.name == "nt" else {}
    subprocess.Popen(args + sys.argv[1:], close_fds=True, **flags)
    os._exit(0)

def _refresh_libraries(backup: Path) -> str:
    """Installed app (install_windows.py) with its own .venv: if requirements.txt changed, install them."""
    venv_python = APP_DIR / ".venv" / "Scripts" / "python.exe"
    old_reqs = backup / "requirements.txt"
    new_reqs = APP_DIR / "requirements.txt"
    if not venv_python.exists() or not new_reqs.exists():
        return ""
    if old_reqs.exists() and old_reqs.read_bytes() == new_reqs.read_bytes():
        return ""
    reqs = [l for l in new_reqs.read_text(encoding="utf-8").splitlines()
            if l.strip() and not l.startswith("#") and not l.startswith("pytest")]
    flags = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
    r = subprocess.run([str(venv_python), "-m", "pip", "install", "--disable-pip-version-check", "-q", *reqs],
                       capture_output=True, text=True, timeout=1800, **flags)
    if r.returncode != 0:
        raise UpdateError(f"New files are installed, but updating libraries failed: {r.stderr.strip()[-300:]}")
    return ", libraries updated"

def apply(info: Optional[UpdateInfo] = None) -> str:
    """Install the latest version. Returns a message; raises UpdateError on failure.
    The running app keeps using the old code until it is restarted."""
    if FROZEN:
        return _apply_exe(info or check())
    if is_git_checkout():
        return _apply_git()
    return _apply_snapshot(info or check())

if __name__ == "__main__":
    i = check()
    print(f"installed: {i.local or 'unknown'}\nlatest:    {i.remote} ({i.remote_date}) {i.remote_message}")
    print("Up to date." if i.up_to_date else apply(i))
