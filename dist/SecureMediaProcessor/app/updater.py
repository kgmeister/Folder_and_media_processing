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

Install types:
  * git checkout (this file is tracked in a clone of the repository; this also
    covers the portable app committed under dist/): `git pull --ff-only` of the
    whole clone. Refuses to run over uncommitted changes, never rewrites local
    commits.
  * plain folder, e.g. the portable app from the release zip: downloads only the
    repository's src/ folder (the app; the folder this file lives in mirrors it),
    checks each file against GitHub's hash and that every .py file compiles,
    backs up the files it replaces to .update_backup/<time>/, then copies the new
    files in. Files deleted upstream are moved to the backup. In the portable
    app, if requirements.txt changed, the libraries in its runtime/ are updated.
  * Android app: the newest APK is attached to the latest GitHub release. The
    check compares the commit the APK was built from; installing it is done by
    Android (the app opens the download, Android asks to install the update).
"""
import hashlib, json, os, py_compile, shutil, subprocess, sys, tempfile, time, urllib.error, urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

REPO = "kgmeister/Folder_and_media_processing"
BRANCH = "main"
APP_SUBDIR = "src"                                  # the repository folder an installed copy mirrors
APK_ASSET = "SecureMediaProcessor.apk"              # release asset name of the Android app
ANDROID = os.environ.get("FLET_PLATFORM") == "android" or sys.platform == "android"
APP_DIR = Path(__file__).resolve().parent
CONFIG_FILE = APP_DIR / "update_config.json"
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
    download_url: str = ""     # Android: the APK of the latest release

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
    """True when this file is tracked in a git clone of the repository (not just any folder inside some repo)."""
    if shutil.which("git") is None:
        return False
    try:
        return _git("ls-files", "--error-unmatch", Path(__file__).name).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False

def local_version() -> Optional[str]:
    if is_git_checkout():
        r = _git("rev-parse", "HEAD")
        return r.stdout.strip() if r.returncode == 0 else None
    try:
        return VERSION_FILE.read_text(encoding="utf-8").strip() or None
    except OSError:
        return None

def _check_release() -> UpdateInfo:
    """Android: the latest release, created by CI with --target <commit sha>, carries the APK."""
    data = json.loads(_get(f"https://api.github.com/repos/{REPO}/releases/latest"))
    asset = next((a for a in data.get("assets", []) if a.get("name") == APK_ASSET), None)
    if asset is None:
        raise UpdateError("The latest release has no Android app (its build may still be running, or it failed). "
                          "Try again later.")
    return UpdateInfo(local_version(), data.get("target_commitish", ""), data.get("published_at", ""),
                      data.get("name", ""), download_url=asset["browser_download_url"])

def check() -> UpdateInfo:
    if ANDROID:
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

def _app_tree(sha: str) -> dict:
    """{relative path: git blob sha} of every file in the repository's APP_SUBDIR at commit `sha` (2 API calls)."""
    api = f"https://api.github.com/repos/{REPO}/git/trees"
    root = json.loads(_get(f"{api}/{sha}"))
    sub = next((e for e in root.get("tree", []) if e["path"] == APP_SUBDIR and e["type"] == "tree"), None)
    if sub is None:
        raise UpdateError(f"The latest version has no {APP_SUBDIR}/ folder; download it again from GitHub instead.")
    tree = json.loads(_get(f"{api}/{sub['sha']}?recursive=1"))
    if tree.get("truncated"):
        raise UpdateError("GitHub returned an incomplete file list; nothing was changed.")
    return {e["path"]: e["sha"] for e in tree["tree"] if e["type"] == "blob"}

def _blob_sha(data: bytes) -> str:
    """The id git gives a file's content; GitHub lists it for every file, so a download can be checked against it."""
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()

def _download_app(sha: str, dest: Path) -> list:
    """Download the app files (raw file host: not counted against the API's 60 requests/hour)."""
    files = []
    for rel, blob in sorted(_app_tree(sha).items()):
        rel = Path(rel)
        if rel.is_absolute() or ".." in rel.parts or rel.parts[0] in PRESERVE:
            continue
        data = _get(f"https://raw.githubusercontent.com/{REPO}/{sha}/{APP_SUBDIR}/{rel.as_posix()}", "*/*")
        if _blob_sha(data) != blob:
            raise UpdateError(f"Downloaded {rel} does not match GitHub's hash; nothing was changed.")
        (dest / rel).parent.mkdir(parents=True, exist_ok=True)
        (dest / rel).write_bytes(data)
        files.append(rel)
    if not files:
        raise UpdateError("The latest version contains no app files; nothing was changed.")
    return files

def _apply_snapshot(info: UpdateInfo) -> str:
    with tempfile.TemporaryDirectory(prefix="smp_update_") as tmp:
        tmp = Path(tmp)
        new_root = tmp / "new"
        new_files = _download_app(info.remote, new_root)
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

def restart():
    """Start a fresh copy of the app and end this one."""
    args = [sys.executable, "-E", "-s", str(APP_DIR / "app.py")]
    flags = {"creationflags": subprocess.DETACHED_PROCESS} if os.name == "nt" else {}
    subprocess.Popen(args + sys.argv[1:], close_fds=True, **flags)
    os._exit(0)

def _refresh_libraries(backup: Path) -> str:
    """Portable app (make_portable.py) with its own runtime: if requirements.txt changed, install them into it."""
    venv_python = APP_DIR.parent / "runtime" / "python.exe"
    old_reqs = backup / "requirements.txt"
    new_reqs = APP_DIR / "requirements.txt"
    if not venv_python.exists() or not new_reqs.exists():
        return ""
    if old_reqs.exists() and old_reqs.read_bytes() == new_reqs.read_bytes():
        return ""
    reqs = [l for l in new_reqs.read_text(encoding="utf-8").splitlines()
            if l.strip() and not l.startswith("#") and not l.startswith("pytest")]
    flags = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
    r = subprocess.run([str(venv_python), "-E", "-s", "-m", "pip", "install", "--disable-pip-version-check", "-q", *reqs],
                       capture_output=True, text=True, timeout=1800, **flags)
    if r.returncode != 0:
        raise UpdateError(f"New files are installed, but updating libraries failed: {r.stderr.strip()[-300:]}")
    return ", libraries updated"

def apply(info: Optional[UpdateInfo] = None) -> str:
    """Install the latest version. Returns a message; raises UpdateError on failure.
    The running app keeps using the old code until it is restarted."""
    if ANDROID:
        raise UpdateError("On Android the update is the APK from the latest release; Android installs it.")
    if is_git_checkout():
        return _apply_git()
    return _apply_snapshot(info or check())

if __name__ == "__main__":
    i = check()
    print(f"installed: {i.local or 'unknown'}\nlatest:    {i.remote} ({i.remote_date}) {i.remote_message}")
    print("Up to date." if i.up_to_date else apply(i))
