from __future__ import annotations

import argparse
import ctypes
from ctypes import wintypes
from dataclasses import asdict, dataclass
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import queue
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import tkinter as tk
from tkinter import messagebox, ttk
import urllib.error
import urllib.request
import uuid
import winreg
import zipfile


VERSION = "1.0.1"
REPOSITORY = "Sai-Hakuto/DS_HIDDEN_HEROES"
RELEASE_API = f"https://api.github.com/repos/{REPOSITORY}/releases/latest"
MAIN_PATTERN = re.compile(r"^DS_HIDDEN_HEROES_ALL_7_\d{8}\.zip$")
GUARD_PATTERN = re.compile(r"^DragonSword_Update_Guard\.zip$")
APP_ID = "4570720"
CREATE_NO_WINDOW = 0x08000000
LOCAL_APPDATA = Path(os.environ.get("LOCALAPPDATA", tempfile.gettempdir()))
DATA_ROOT = Path(os.environ.get("HIDDEN_HEROES_INSTALLER_DATA_ROOT", LOCAL_APPDATA / "HiddenHeroesInstaller"))
GUARD_ROOT = LOCAL_APPDATA / "DragonSwordGuard"
POWERSHELL = Path(r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe")
UNINSTALL_SHORTCUT = "Uninstall Hidden Heroes.lnk"

COLORS = {
    "bg": "#090D14", "surface": "#101722", "surface2": "#172131",
    "border": "#29384E", "text": "#F2F5FA", "muted": "#9AA8BC",
    "faint": "#66758A", "gold": "#D9A441", "gold2": "#F0BC54",
    "green": "#58C98B", "red": "#E87979", "blue": "#6FA8FF",
}


class InstallerError(RuntimeError):
    pass


class InstallCancelled(InstallerError):
    pass


@dataclass(frozen=True)
class Asset:
    name: str
    url: str
    size: int
    sha256: str


@dataclass(frozen=True)
class Release:
    tag: str
    main: Asset
    guard: Asset | None


@dataclass(frozen=True)
class FilePlan:
    relative: str
    staged: Path
    size: int


@dataclass
class InstallResult:
    tag: str
    asset: str
    sha256: str
    files_verified: int
    game_root: str
    backup_root: str
    log_path: str
    system_integration: bool
    guard_was_restored: bool = False
    warning: str = ""


@dataclass
class UninstallResult:
    game_root: str
    deleted_root: str
    files_moved: int
    files_restored: int
    guard_was_restored: bool
    log_path: str


class Reporter:
    def __init__(self, callback=None, cancel_event: threading.Event | None = None):
        self.callback = callback
        self.cancel_event = cancel_event or threading.Event()

    def send(self, percent: int, stage: str, status: str, detail: str = "") -> None:
        percent = max(0, min(100, int(percent)))
        log(f"progress={percent} stage={stage} status={status} detail={detail}")
        if self.callback:
            self.callback(percent, stage, status, detail)

    def cancellable(self) -> None:
        if self.cancel_event.is_set():
            raise InstallCancelled("Installation was cancelled before game files were changed.")


LOG_PATH: Path | None = None


def initialize_log() -> Path:
    global LOG_PATH
    log_dir = DATA_ROOT / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    LOG_PATH = log_dir / f"install_{datetime.now():%Y%m%d_%H%M%S_%f}.log"
    LOG_PATH.write_text(f"Hidden Heroes Installer {VERSION}\n", encoding="utf-8")
    return LOG_PATH


def log(text: str) -> None:
    if LOG_PATH is None:
        return
    with LOG_PATH.open("a", encoding="utf-8", newline="") as stream:
        stream.write(f"{datetime.now().isoformat(timespec='milliseconds')} {text}\n")


def norm(path: Path | str) -> Path:
    return Path(os.path.abspath(os.path.normpath(str(path))))


def same_path(left: Path | str, right: Path | str) -> bool:
    # Path identity may use a junction alias while receipts retain the user's
    # original spelling. Resolve for comparison, not for stored path rewriting.
    return os.path.normcase(str(norm(left).resolve())) == os.path.normcase(str(norm(right).resolve()))


def inside(path: Path | str, root: Path | str) -> bool:
    try:
        return os.path.commonpath([str(norm(path)), str(norm(root))]).casefold() == str(norm(root)).casefold()
    except ValueError:
        return False


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().upper()


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, path)


def _request(url: str):
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": f"Hidden-Heroes-Installer/{VERSION}",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    try:
        return urllib.request.urlopen(request, timeout=45)
    except urllib.error.HTTPError as error:
        raise InstallerError(f"GitHub returned HTTP {error.code} for {url}.") from error
    except OSError as error:
        raise InstallerError(f"Could not connect to GitHub: {error}") from error


def _asset(value: dict, pattern: re.Pattern[str], required: bool) -> Asset | None:
    matches = [item for item in value.get("assets", []) if pattern.fullmatch(str(item.get("name", "")))]
    if not matches and not required:
        return None
    if len(matches) != 1:
        raise InstallerError(f"Release must contain exactly one asset matching {pattern.pattern}; found {len(matches)}.")
    item = matches[0]
    name = str(item.get("name", ""))
    url = str(item.get("browser_download_url", ""))
    size = int(item.get("size", 0))
    digest = str(item.get("digest", ""))
    digest_match = re.fullmatch(r"sha256:([0-9a-fA-F]{64})", digest)
    if size < 1024 * 1024 or size > 2 * 1024 * 1024 * 1024:
        raise InstallerError(f"Asset {name} has an unexpected size: {size} bytes.")
    if not digest_match:
        raise InstallerError(f"Asset {name} has no usable GitHub SHA-256 digest.")
    trusted = f"https://github.com/{REPOSITORY}/releases/download/"
    if not url.startswith(trusted):
        raise InstallerError(f"Asset {name} points outside the trusted Hidden Heroes repository.")
    return Asset(name, url, size, digest_match.group(1).upper())


def resolve_release(metadata_path: Path | None = None) -> Release:
    if metadata_path:
        value = json.loads(metadata_path.read_text(encoding="utf-8"))
    else:
        with _request(RELEASE_API) as response:
            value = json.loads(response.read().decode("utf-8"))
    if value.get("draft") or value.get("prerelease"):
        raise InstallerError("GitHub returned a draft or prerelease.")
    tag = str(value.get("tag_name", ""))
    if not re.fullmatch(r"[0-9A-Za-z][0-9A-Za-z._-]{0,63}", tag):
        raise InstallerError(f"GitHub returned an invalid release tag: {tag!r}.")
    main = _asset(value, MAIN_PATTERN, True)
    assert main is not None
    return Release(tag, main, _asset(value, GUARD_PATTERN, False))


def download(asset: Asset, destination: Path, reporter: Reporter, start: int, end: int, local: Path | None = None) -> None:
    source = local.open("rb") if local else _request(asset.url)
    try:
        total = local.stat().st_size if local else int(source.headers.get("Content-Length", asset.size))
        if total != asset.size:
            raise InstallerError(f"Download length {total} does not match GitHub metadata {asset.size}.")
        written = 0
        last_percent = -1
        with destination.open("wb") as target:
            while True:
                reporter.cancellable()
                chunk = source.read(1024 * 1024)
                if not chunk:
                    break
                target.write(chunk)
                written += len(chunk)
                percent = start + int((end - start) * written / max(1, total))
                if percent != last_percent:
                    reporter.send(percent, "download", "Downloading from GitHub", f"{written / 1048576:.1f} / {total / 1048576:.1f} MB")
                    last_percent = percent
        if written != asset.size:
            raise InstallerError(f"Download ended at {written} bytes; expected {asset.size}.")
    finally:
        source.close()


ALLOWED_PREFIXES = (
    "DS/Binaries/Win64/ue4ss/",
    "DS/Content/Paks/mods/",
)
ROOT_FILES = {"README.md", "MANIFEST.json"}
REQUIRED_FILES = {
    "README.md",
    "MANIFEST.json",
    "DS/Binaries/Win64/dwmapi.dll",
    "DS/Binaries/Win64/ue4ss/LICENSE",
    "DS/Binaries/Win64/ue4ss/UE4SS.dll",
    "DS/Binaries/Win64/ue4ss/UE4SS-settings.ini",
    "DS/Binaries/Win64/ue4ss/QualificationMods/mods.txt",
    "DS/Binaries/Win64/ue4ss/QualificationMods/DSAbuseCompat/Native/DSAbuseCompat.dll",
    "DS/Binaries/Win64/ue4ss/QualificationMods/DSAbuseCompat/README.md",
    "DS/Binaries/Win64/ue4ss/QualificationMods/DSAbuseCompat/Scripts/main.lua",
    "DS/Binaries/Win64/ue4ss/QualificationMods/CharacterShowSetInfoGuard/Native/DSCharacterShowSetInfoGuard.dll",
    "DS/Binaries/Win64/ue4ss/QualificationMods/CharacterShowSetInfoGuard/README.md",
    "DS/Binaries/Win64/ue4ss/QualificationMods/CharacterShowSetInfoGuard/Scripts/main.lua",
    "DS/Content/Paks/mods/DS_HIDDEN_HEROES_P.pak",
}


def allowed_archive_path(relative: str, directory: bool) -> bool:
    if not directory and relative in ROOT_FILES:
        return True
    if not directory and relative.casefold() == "DS/Binaries/Win64/dwmapi.dll".casefold():
        return True
    folded = relative.casefold().rstrip("/") + "/"
    for prefix in ALLOWED_PREFIXES:
        if relative.casefold().startswith(prefix.casefold()):
            return True
        if directory and prefix.casefold().startswith(folded):
            return True
    return False


def validate_zip_name(info: zipfile.ZipInfo) -> tuple[str, bool]:
    raw = info.filename.replace("\\", "/")
    path = PurePosixPath(raw)
    parts = tuple(part for part in path.parts if part not in ("", "/"))
    if not parts or path.is_absolute() or any(part in (".", "..") or ":" in part for part in parts):
        raise InstallerError(f"Unsafe ZIP path: {raw!r}.")
    mode = (info.external_attr >> 16) & 0xFFFF
    if mode and stat.S_ISLNK(mode):
        raise InstallerError(f"ZIP symlinks are not allowed: {raw!r}.")
    relative = "/".join(parts)
    directory = info.is_dir() or raw.endswith("/")
    if not allowed_archive_path(relative, directory):
        raise InstallerError(f"ZIP entry is outside the Hidden Heroes installation scope: {relative!r}.")
    return relative, directory


def validate_embedded_manifest(staging: Path, plans: list[FilePlan]) -> None:
    manifest_path = staging / "MANIFEST.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise InstallerError("Hidden Heroes MANIFEST.json is missing or invalid.") from error
    items = manifest.get("files")
    if not isinstance(items, list) or not items:
        raise InstallerError("Hidden Heroes manifest has no payload file list.")
    plan_by_path = {item.relative.replace(os.sep, "/"): item for item in plans}
    manifest_paths: set[str] = set()
    for item in items:
        if not isinstance(item, dict):
            raise InstallerError("Hidden Heroes manifest contains an invalid file record.")
        relative = str(item.get("path", "")).replace("\\", "/")
        path = PurePosixPath(relative)
        if (
            path.is_absolute() or not relative.startswith("DS/") or
            any(part in ("", ".", "..") or ":" in part for part in path.parts)
        ):
            raise InstallerError(f"Unsafe manifest path: {relative!r}.")
        key = relative.casefold()
        if key in manifest_paths:
            raise InstallerError(f"Duplicate manifest path: {relative!r}.")
        manifest_paths.add(key)
        plan = plan_by_path.get(relative)
        expected_hash = str(item.get("sha256", "")).upper()
        expected_size = int(item.get("bytes", -1))
        if plan is None:
            raise InstallerError(f"Manifest payload is missing from ZIP: {relative!r}.")
        if plan.size != expected_size or not re.fullmatch(r"[0-9A-F]{64}", expected_hash):
            raise InstallerError(f"Manifest metadata is invalid for {relative!r}.")
        if sha256(plan.staged) != expected_hash:
            raise InstallerError(f"Manifest SHA-256 mismatch for {relative!r}.")
    planned_paths = {relative.casefold() for relative in plan_by_path}
    if manifest_paths != planned_paths:
        extra = sorted(planned_paths - manifest_paths)
        raise InstallerError(f"ZIP contains an unmanifested payload file: {extra[0] if extra else 'unknown'}.")


def extract_main_archive(archive: Path, staging: Path, reporter: Reporter) -> list[FilePlan]:
    with zipfile.ZipFile(archive) as package:
        if not 10 <= len(package.infolist()) <= 5000:
            raise InstallerError(f"Unexpected ZIP entry count: {len(package.infolist())}.")
        seen: set[str] = set()
        entries: list[tuple[zipfile.ZipInfo, str]] = []
        total = 0
        for info in package.infolist():
            relative, directory = validate_zip_name(info)
            key = relative.casefold()
            if key in seen:
                raise InstallerError(f"Duplicate ZIP path: {relative!r}.")
            seen.add(key)
            if not directory:
                total += info.file_size
                if total > 2 * 1024 * 1024 * 1024:
                    raise InstallerError("ZIP expands beyond the 2 GB safety limit.")
                entries.append((info, relative))
        missing = [item for item in REQUIRED_FILES if item.casefold() not in seen]
        if missing:
            raise InstallerError(f"Hidden Heroes ZIP is missing required file {missing[0]!r}.")

        plans: list[FilePlan] = []
        expanded = 0
        last_percent = -1
        for info, relative in entries:
            reporter.cancellable()
            target = norm(staging / Path(*relative.split("/")))
            if not inside(target, staging):
                raise InstallerError(f"ZIP path escapes staging: {relative!r}.")
            target.parent.mkdir(parents=True, exist_ok=True)
            with package.open(info) as source, target.open("wb") as output:
                while True:
                    chunk = source.read(1024 * 1024)
                    if not chunk:
                        break
                    output.write(chunk)
                    expanded += len(chunk)
                    percent = 58 + int(17 * expanded / max(1, total))
                    if percent != last_percent:
                        reporter.send(percent, "extract", "Validating and extracting", relative)
                        last_percent = percent
            if relative.startswith("DS/"):
                plans.append(FilePlan(relative.replace("/", os.sep), target, info.file_size))
        validate_embedded_manifest(staging, plans)
        return plans


def validate_game_root(value: Path | str, skip_system: bool = False) -> Path:
    root = norm(value)
    required = (
        root / "DSClient.exe",
        root / "DS" / "Binaries" / "Win64" / "DSClient-Win64-Shipping.exe",
        root / "DS" / "Content" / "Paks",
    )
    if not required[0].is_file() or not required[1].is_file() or not required[2].is_dir():
        raise InstallerError("Select the DragonSword game root containing DSClient.exe and the DS folder.")
    running = [item for item in processes() if item[1].casefold() in {"dsclient.exe", "dsclient-win64-shipping.exe"}]
    if running:
        raise InstallerError("DragonSword is running. Close the game before installing Hidden Heroes.")
    return root


def _other_product_receipt(root: Path, data_name: str) -> dict:
    key = hashlib.sha256(os.path.normcase(str(norm(root))).encode("utf-8")).hexdigest()[:24]
    path = LOCAL_APPDATA / data_name / "receipts" / f"{key}.json"
    if not path.is_file():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {}
    return value if value.get("status") == "installed" and same_path(value.get("game_root", ""), root) else {}


def product_conflicts(root: Path) -> list[str]:
    conflicts: list[str] = []
    if _other_product_receipt(root, "GGOInstaller"):
        conflicts.append("an installed GGO receipt")
    mods = root / "DS" / "Content" / "Paks" / "mods"
    if mods.is_dir():
        for pak in sorted(mods.glob("*.pak"), key=lambda value: value.name.casefold()):
            folded = pak.name.casefold()
            if folded == "ds_hidden_heroes_p.pak":
                continue
            if "ggo" in folded or "hidden_heroes" in folded or "hiddenheroes" in folded:
                conflicts.append(pak.name)
    return conflicts


kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
TH32CS_SNAPPROCESS = 0x00000002
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000

kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
kernel32.Process32FirstW.argtypes = [wintypes.HANDLE, wintypes.LPVOID]
kernel32.Process32FirstW.restype = wintypes.BOOL
kernel32.Process32NextW.argtypes = [wintypes.HANDLE, wintypes.LPVOID]
kernel32.Process32NextW.restype = wintypes.BOOL
kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
kernel32.OpenProcess.restype = wintypes.HANDLE
kernel32.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
kernel32.CloseHandle.restype = wintypes.BOOL


class PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
        ("th32ProcessID", wintypes.DWORD), ("th32DefaultHeapID", ctypes.c_size_t),
        ("th32ModuleID", wintypes.DWORD), ("cntThreads", wintypes.DWORD),
        ("th32ParentProcessID", wintypes.DWORD), ("pcPriClassBase", wintypes.LONG),
        ("dwFlags", wintypes.DWORD), ("szExeFile", wintypes.WCHAR * 260),
    ]


def processes() -> list[tuple[int, str, Path | None]]:
    snapshot = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if snapshot == wintypes.HANDLE(-1).value:
        return []
    result: list[tuple[int, str, Path | None]] = []
    entry = PROCESSENTRY32W()
    entry.dwSize = ctypes.sizeof(entry)
    try:
        ok = kernel32.Process32FirstW(snapshot, ctypes.byref(entry))
        while ok:
            pid = int(entry.th32ProcessID)
            image: Path | None = None
            handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
            if handle:
                try:
                    buffer = ctypes.create_unicode_buffer(32768)
                    size = wintypes.DWORD(len(buffer))
                    if kernel32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
                        image = Path(buffer.value)
                finally:
                    kernel32.CloseHandle(handle)
            result.append((pid, entry.szExeFile, image))
            ok = kernel32.Process32NextW(snapshot, ctypes.byref(entry))
    finally:
        kernel32.CloseHandle(snapshot)
    return result


def assert_writable(root: Path, files: list[FilePlan]) -> None:
    parents: set[Path] = set()
    for item in files:
        target = root / item.relative
        if target.is_file():
            try:
                with target.open("r+b"):
                    pass
            except OSError as error:
                raise InstallerError(f"Target file is locked or read-only: {target}") from error
        parent = target.parent
        while not parent.exists() and parent != parent.parent:
            parent = parent.parent
        parents.add(parent)
    for parent in parents:
        probe = parent / f".hidden_heroes_write_probe_{uuid.uuid4().hex}.tmp"
        try:
            probe.write_bytes(b"probe")
        except OSError as error:
            raise InstallerError(f"Target directory is not writable: {parent}") from error
        finally:
            probe.unlink(missing_ok=True)


def new_backup(root: Path, files: list[FilePlan], tag: str, reporter: Reporter) -> tuple[Path, list[dict]]:
    safe_tag = re.sub(r"[^0-9A-Za-z._-]", "_", tag)
    backup = DATA_ROOT / "backups" / f"{datetime.now():%Y%m%d_%H%M%S_%f}_{safe_tag}"
    records: list[dict] = []
    for index, item in enumerate(files, 1):
        target = root / item.relative
        existed = target.is_file()
        if existed:
            saved = backup / "files" / item.relative
            saved.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(target, saved)
        records.append({"relative": item.relative, "existed": existed})
        reporter.send(75 + int(6 * index / len(files)), "backup", "Creating rollback backup", item.relative)
    write_json(backup / "backup_manifest.json", {
        "installer_version": VERSION, "tag": tag, "game_root": str(root),
        "created_at": datetime.now().isoformat(), "files": records,
    })
    return backup, records


def configuration_state(root: Path, backup: Path, skip_system: bool) -> list[dict]:
    paths = [receipt_path(root)]
    if not skip_system:
        paths.append(root / UNINSTALL_SHORTCUT)
    records = []
    for index, path in enumerate(paths):
        saved = backup / "configuration" / f"{index}_{path.name}"
        existed = path.is_file()
        if existed:
            saved.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, saved)
        records.append({"path": str(path), "saved": str(saved), "existed": existed})
    write_json(backup / "configuration" / "manifest.json", records)
    return records


def configure_ue4ss(root: Path) -> None:
    ue4ss = root / "DS" / "Binaries" / "Win64" / "ue4ss"
    mods = ue4ss / "QualificationMods"
    settings_path = ue4ss / "UE4SS-settings.ini"
    settings = settings_path.read_text(encoding="utf-8")
    mods_text = str(mods).replace("\\", "/")
    mods_file = str(mods / "mods.txt").replace("\\", "/")
    settings, mods_count = re.subn(
        r"(?m)^ModsFolderPath\s*=.*$", f"ModsFolderPath = {mods_text}", settings
    )
    settings, control_count = re.subn(
        r"(?m)^ControllingModsTxt\s*=.*$", f"ControllingModsTxt = {mods_file}", settings
    )
    if mods_count != 1 or control_count != 1:
        raise InstallerError("Could not configure UE4SS paths.")
    settings_path.write_text(settings, encoding="utf-8", newline="")


def rollback(root: Path, backup: Path, records: list[dict], config_records: list[dict]) -> None:
    log("ROLLBACK begin")
    for record in sorted(records, key=lambda item: len(item["relative"]), reverse=True):
        target = root / record["relative"]
        if record["existed"]:
            source = backup / "files" / record["relative"]
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
        else:
            target.unlink(missing_ok=True)
    for record in config_records:
        target = Path(record["path"])
        if record["existed"]:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(Path(record["saved"]), target)
        else:
            target.unlink(missing_ok=True)
    log("ROLLBACK complete")


def apply_files(root: Path, files: list[FilePlan], reporter: Reporter) -> None:
    for index, item in enumerate(files, 1):
        target = root / item.relative
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(target.name + f".hidden_heroes_new_{uuid.uuid4().hex}")
        try:
            shutil.copy2(item.staged, temporary)
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)
        reporter.send(81 + int(7 * index / len(files)), "install", "Installing files", item.relative)


def verify_files(root: Path, files: list[FilePlan], reporter: Reporter) -> None:
    for index, item in enumerate(files, 1):
        target = root / item.relative
        if not target.is_file() or target.stat().st_size != item.size or sha256(target) != sha256(item.staged):
            raise InstallerError(f"Installed file verification failed: {target}")
        reporter.send(88 + int(6 * index / len(files)), "verify", "Verifying installed files", item.relative)


def ps_quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def create_shortcut(
    path: Path,
    target: Path,
    arguments: str,
    working: Path,
    description: str = "Run Hidden Heroes maintenance",
) -> None:
    script = (
        "$w=New-Object -ComObject WScript.Shell;"
        f"$s=$w.CreateShortcut({ps_quote(str(path))});"
        f"$s.TargetPath={ps_quote(str(target))};"
        f"$s.Arguments={ps_quote(arguments)};"
        f"$s.WorkingDirectory={ps_quote(str(working))};"
        f"$s.WindowStyle=7;$s.Description={ps_quote(description)};$s.IconLocation={ps_quote(str(target) + ',0')};$s.Save()"
    )
    completed = subprocess.run(
        [str(POWERSHELL), "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", script],
        capture_output=True, creationflags=CREATE_NO_WINDOW,
    )
    if completed.returncode or not path.is_file():
        raise InstallerError(f"Could not create shortcut {path.name}.")


def receipt_path(root: Path) -> Path:
    key = hashlib.sha256(os.path.normcase(str(norm(root))).encode("utf-8")).hexdigest()[:24]
    return DATA_ROOT / "receipts" / f"{key}.json"


def owned_directory_records(root: Path, files: list[FilePlan]) -> list[dict]:
    ue4ss = os.path.join("DS", "Binaries", "Win64", "ue4ss")
    if not (root / ue4ss).is_dir():
        relatives = {ue4ss}
    else:
        relatives = {
            os.path.join(ue4ss, "QualificationMods", "CharacterShowSetInfoGuard"),
            os.path.join(ue4ss, "QualificationMods", "DSAbuseCompat"),
        }
    return [
        {"relative": relative, "existed": (root / relative).is_dir()}
        for relative in sorted(relatives, key=str.casefold)
    ]


def installed_directory_records(root: Path, files: list[FilePlan]) -> list[dict]:
    relatives: set[Path] = set()
    for item in files:
        current = Path(item.relative).parent
        while current != Path("."):
            relatives.add(current)
            current = current.parent
    return [
        {"relative": str(relative).replace(os.sep, "/"), "existed": (root / relative).is_dir()}
        for relative in sorted(relatives, key=lambda value: (len(value.parts), str(value).casefold()))
    ]


def write_install_receipt(
    root: Path,
    release: Release,
    files: list[FilePlan],
    backup: Path,
    records: list[dict],
    config_records: list[dict],
    owned_directories: list[dict],
    skip_system: bool,
    directories: list[dict] | None = None,
) -> None:
    previous: dict = {}
    previous_path = receipt_path(root)
    if previous_path.is_file():
        try:
            candidate = json.loads(previous_path.read_text(encoding="utf-8"))
            if candidate.get("status") == "installed" and same_path(candidate.get("game_root", ""), root):
                previous = candidate
        except (OSError, ValueError, TypeError):
            previous = {}
    previous_files = {item["relative"].replace("\\", "/"): item for item in previous.get("files", [])}
    previous_directories = {item["relative"].replace("\\", "/"): item for item in previous.get("directories", [])}
    record_by_relative = {item["relative"]: item for item in records}
    installed_files = []
    for item in files:
        record = record_by_relative[item.relative]
        relative = item.relative.replace(os.sep, "/")
        prior = previous_files.get(relative)
        existed_before = bool(prior.get("existed_before")) if prior else bool(record["existed"])
        restore_source = ""
        if existed_before:
            if prior:
                restore_source = str(prior.get("restore_source") or (Path(previous["backup_root"]) / "files" / _relative_path(relative)))
            else:
                restore_source = str(backup / "files" / item.relative)
        installed_files.append({
            "relative": relative,
            "sha256": sha256(root / item.relative),
            "existed_before": existed_before,
            "restore_source": restore_source,
        })
    installed_directories = []
    for item in directories or []:
        relative = str(item["relative"]).replace("\\", "/")
        prior = previous_directories.get(relative)
        installed_directories.append({
            "relative": relative,
            "existed_before": bool(prior.get("existed_before")) if prior else bool(item["existed"]),
        })
    value = {
        "schema": 1,
        "status": "installed",
        "installer_version": VERSION,
        "installed_at": datetime.now().isoformat(),
        "game_root": str(root),
        "tag": release.tag,
        "asset": release.main.name,
        "asset_sha256": release.main.sha256,
        "backup_root": str(backup),
        "files": installed_files,
        "directories": installed_directories,
        "owned_directories": owned_directories,
        "configuration": config_records,
        "system_integration": not skip_system,
    }
    write_json(previous_path, value)
    if not skip_system:
        executable = Path(sys.executable).resolve()
        if not getattr(sys, "frozen", False) or not same_path(executable.parent, root):
            raise InstallerError("Hidden_Heroes_Installer.exe must be running from the game folder to create the uninstaller.")
        create_shortcut(
            root / UNINSTALL_SHORTCUT,
            executable,
            "--uninstall",
            root,
            "Move Hidden Heroes files into deleted_hidden_heroes and restore the previous game state",
        )


def guard_state() -> dict:
    path = GUARD_ROOT / "state.json"
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def guard_active_for(root: Path) -> bool:
    state = guard_state()
    return bool(state.get("locked") and (GUARD_ROOT / "active.lock").is_file() and state.get("game") and same_path(state["game"], root))


def run_guard(action: str, root: Path) -> None:
    executable = root / "DragonSwordGuard.exe"
    if not executable.is_file():
        raise InstallerError("The installed Update Guard executable is missing.")
    completed = subprocess.run([str(executable), action, "--quiet"], creationflags=CREATE_NO_WINDOW, timeout=180)
    if completed.returncode:
        raise InstallerError(f"Update Guard action {action!r} failed with exit code {completed.returncode}.")


def install_main(
    game_dir: Path | str,
    reporter: Reporter,
    *,
    skip_system: bool = False,
    allow_guard_disable: bool = False,
    metadata_path: Path | None = None,
    asset_file: Path | None = None,
) -> tuple[InstallResult, Release]:
    if LOG_PATH is None:
        initialize_log()
    root = validate_game_root(game_dir, skip_system)
    log(f"game_root={root}")
    conflicts = product_conflicts(root)
    if conflicts:
        raise InstallerError(
            "Hidden Heroes is mutually exclusive with GGO and older Hidden Heroes products. "
            f"Remove this conflict first: {conflicts[0]}."
        )
    reporter.send(2, "release", "Resolving latest release", REPOSITORY)
    release = resolve_release(metadata_path)
    reporter.send(4, "release", f"Release {release.tag}", release.main.name)
    if (GUARD_ROOT / "active.lock").is_file() and not guard_state().get("game"):
        raise InstallerError("Update Guard is active but its protected game path cannot be read. Disable Guard before installing.")
    guard_was_active = guard_active_for(root)
    if guard_was_active:
        if not allow_guard_disable:
            raise InstallerError("Update Guard protects this game. Allow the installer to disable it temporarily.")
        reporter.send(1, "preflight", "Disabling Update Guard temporarily", str(root))
        run_guard("unlock", root)
        if guard_active_for(root):
            raise InstallerError("Update Guard remained active after the unlock request.")

    backup: Path | None = None
    records: list[dict] = []
    config_records: list[dict] = []
    completed = False
    guard_restored = False
    warning = ""
    try:
        with tempfile.TemporaryDirectory(prefix="HiddenHeroesInstaller_") as raw_work:
            work = Path(raw_work)
            archive = work / release.main.name
            staging = work / "staging"
            download(release.main, archive, reporter, 5, 55, asset_file)
            reporter.send(56, "verify", "Verifying GitHub SHA-256", release.main.sha256)
            actual = sha256(archive)
            if actual != release.main.sha256:
                raise InstallerError(f"SHA-256 mismatch. Expected {release.main.sha256}, received {actual}. No game files were changed.")
            plans = extract_main_archive(archive, staging, reporter)
            owned_directories = owned_directory_records(root, plans)
            directories = installed_directory_records(root, plans)
            reporter.cancellable()
            assert_writable(root, plans)
            backup, records = new_backup(root, plans, release.tag, reporter)
            config_records = configuration_state(root, backup, skip_system)
            try:
                apply_files(root, plans, reporter)
                verify_files(root, plans, reporter)
                reporter.send(94, "configure", "Configuring UE4SS paths", str(root))
                configure_ue4ss(root)
                reporter.send(95, "install", "Creating uninstall support", str(root))
                write_install_receipt(
                    root, release, plans, backup, records, config_records,
                    owned_directories, skip_system, directories,
                )
            except Exception:
                rollback(root, backup, records, config_records)
                raise

            if guard_was_active:
                reporter.send(99, "guard", "Restoring Update Guard", str(root))
                try:
                    run_guard("lock", root)
                    guard_restored = guard_active_for(root)
                    if not guard_restored:
                        raise InstallerError("Guard did not confirm the protected state.")
                except Exception as error:
                    warning = f"Hidden Heroes installed, but Update Guard could not be restored: {error}"
                    log(warning)
            reporter.send(100, "complete", "Installation complete", f"{len(plans)} files verified")
            completed = True
            result = InstallResult(
                release.tag, release.main.name, actual, len(plans), str(root), str(backup),
                str(LOG_PATH), not skip_system, guard_restored, warning,
            )
            return result, release
    finally:
        if guard_was_active and not completed and not guard_active_for(root):
            try:
                run_guard("lock", root)
            except Exception as error:
                log(f"failed to restore Guard after install failure: {error!r}")


def _relative_path(value: str) -> Path:
    pure = PurePosixPath(value.replace("\\", "/"))
    if pure.is_absolute() or not pure.parts or any(part in ("", ".", "..") or ":" in part for part in pure.parts):
        raise InstallerError(f"Unsafe install receipt path: {value!r}.")
    return Path(*pure.parts)


def _move_to_deleted(source: Path, destination: Path) -> int:
    if not source.exists():
        return 0
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        raise InstallerError(f"Uninstall destination already exists: {destination}")
    count = sum(1 for item in source.rglob("*") if item.is_file()) if source.is_dir() else 1
    shutil.move(str(source), str(destination))
    return count


def _prune_empty(path: Path, stop: Path) -> None:
    current = path
    while inside(current, stop) and current != stop:
        try:
            current.rmdir()
        except OSError:
            return
        current = current.parent


def uninstall_main(
    game_dir: Path | str,
    *,
    allow_guard_disable: bool = False,
) -> UninstallResult:
    if LOG_PATH is None:
        initialize_log()
    root = validate_game_root(game_dir, True)
    path = receipt_path(root)
    if not path.is_file():
        raise InstallerError("Hidden Heroes installation receipt was not found. Nothing was changed.")
    receipt = json.loads(path.read_text(encoding="utf-8"))
    if receipt.get("schema") != 1 or receipt.get("status") != "installed" or not same_path(receipt.get("game_root", ""), root):
        raise InstallerError("The Hidden Heroes installation receipt is missing, stale, or belongs to another game folder.")

    guard_was_active = guard_active_for(root)
    if guard_was_active:
        if not allow_guard_disable:
            raise InstallerError("Update Guard protects this game. Confirm temporary protection disable to uninstall Hidden Heroes.")
        run_guard("unlock", root)
        if guard_active_for(root):
            raise InstallerError("Update Guard remained active after the unlock request.")

    deleted = root / "deleted_hidden_heroes" / f"{datetime.now():%Y%m%d-%H%M%S}"
    if deleted.exists():
        raise InstallerError(f"Uninstall destination already exists: {deleted}")
    deleted.mkdir(parents=True)
    moved = 0
    restored = 0
    guard_restored = False
    uninstall_report: dict = {
        "status": "IN_PROGRESS",
        "game_root": str(root),
        "deleted_root": str(deleted),
        "started_at": datetime.now().isoformat(),
        "moved": [],
        "restored": [],
    }

    def record_move(source: Path, destination: Path) -> None:
        nonlocal moved
        count = _move_to_deleted(source, destination)
        if count:
            moved += count
            uninstall_report["moved"].append({"source": str(source), "destination": str(destination), "files": count})
            write_json(deleted / "uninstall-report.json", uninstall_report)

    try:
        backup = norm(receipt["backup_root"])
        if not inside(backup, DATA_ROOT / "backups") or not (backup / "backup_manifest.json").is_file():
            raise InstallerError("The rollback backup referenced by the receipt is unavailable or unsafe.")

        owned_directories = sorted(
            receipt.get("owned_directories", []),
            key=lambda item: len(str(item.get("relative", ""))),
            reverse=True,
        )
        for item in owned_directories:
            if item.get("existed"):
                continue
            relative = _relative_path(str(item["relative"]))
            source = norm(root / relative)
            if not inside(source, root):
                raise InstallerError(f"Unsafe owned directory in receipt: {relative}")
            record_move(source, deleted / relative)

        for item in receipt.get("files", []):
            relative = _relative_path(str(item["relative"]))
            source = norm(root / relative)
            if not inside(source, root):
                raise InstallerError(f"Unsafe file in receipt: {relative}")
            record_move(source, deleted / relative)

        receipt_file = receipt_path(root)
        allowed_external: set[Path] = set()
        for index, item in enumerate(receipt.get("configuration", [])):
            source = norm(item["path"])
            if source == receipt_file:
                continue
            if not inside(source, root) and source not in allowed_external:
                raise InstallerError(f"Unsafe configuration path in receipt: {source}")
            relative = _relative_path(str(source.relative_to(root)).replace(os.sep, "/")) if inside(source, root) else Path("_system") / f"{index}_{source.name}"
            record_move(source, deleted / relative)

        for item in receipt.get("files", []):
            if not item.get("existed_before"):
                continue
            relative = _relative_path(str(item["relative"]))
            source = norm(item.get("restore_source") or (backup / "files" / relative))
            target = root / relative
            if not source.is_file() or not inside(source, DATA_ROOT / "backups"):
                raise InstallerError(f"Rollback file is missing or unsafe: {source}")
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
            restored += 1
            uninstall_report["restored"].append(str(target))

        for item in receipt.get("configuration", []):
            target = norm(item["path"])
            if target == receipt_file or not item.get("existed"):
                continue
            source = norm(item["saved"])
            if not source.is_file() or not inside(source, backup):
                raise InstallerError(f"Configuration rollback file is missing or unsafe: {source}")
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
            restored += 1
            uninstall_report["restored"].append(str(target))

        directories = sorted(
            receipt.get("directories", []),
            key=lambda item: len(_relative_path(str(item["relative"])).parts),
            reverse=True,
        )
        for item in directories:
            if item.get("existed_before"):
                continue
            relative = _relative_path(str(item["relative"]))
            directory = norm(root / relative)
            if not inside(directory, root):
                raise InstallerError(f"Unsafe directory in receipt: {relative}")
            _prune_empty(directory, root)

        receipt["status"] = "uninstalled"
        receipt["uninstalled_at"] = datetime.now().isoformat()
        receipt["deleted_root"] = str(deleted)
        write_json(path, receipt)
        uninstall_report.update({
            "status": "PASS",
            "completed_at": datetime.now().isoformat(),
            "files_moved": moved,
            "files_restored": restored,
            "guard_restore_requested": guard_was_active,
        })
        write_json(deleted / "uninstall-report.json", uninstall_report)

        # The Guard makes the game tree read-only. Finish every write inside the
        # game root before restoring it; otherwise the final report itself turns
        # a successful uninstall into a false PermissionError failure.
        if guard_was_active:
            run_guard("lock", root)
            guard_restored = guard_active_for(root)
            if not guard_restored:
                raise InstallerError("Hidden Heroes was removed, but Update Guard could not be restored.")

        log(f"UNINSTALL PASS moved={moved} restored={restored} deleted={deleted}")
        return UninstallResult(str(root), str(deleted), moved, restored, guard_restored, str(LOG_PATH))
    except Exception:
        uninstall_report["status"] = "FAIL"
        uninstall_report["failed_at"] = datetime.now().isoformat()
        try:
            write_json(deleted / "uninstall-report.json", uninstall_report)
        except OSError as report_error:
            log(f"failed to update uninstall report after error: {report_error!r}")
        if guard_was_active and not guard_active_for(root):
            try:
                run_guard("lock", root)
            except Exception as error:
                log(f"failed to restore Guard after uninstall failure: {error!r}")
        raise


def steam_games() -> list[Path]:
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam") as key:
            steam = Path(str(winreg.QueryValueEx(key, "SteamPath")[0]).replace("/", "\\"))
    except OSError:
        return []
    libraries = [steam]
    vdf = steam / "steamapps" / "libraryfolders.vdf"
    if vdf.is_file():
        text = vdf.read_text(encoding="utf-8", errors="replace")
        libraries.extend(Path(raw.replace(r"\\", "\\")) for raw in re.findall(r'^\s*"path"\s+"([^"]+)"', text, re.MULTILINE))
    result = []
    seen: set[str] = set()
    for library in libraries:
        manifest = library / "steamapps" / f"appmanifest_{APP_ID}.acf"
        if not manifest.is_file():
            continue
        text = manifest.read_text(encoding="utf-8", errors="replace")
        match = re.search(r'^\s*"installdir"\s+"([^"]+)"', text, re.MULTILINE)
        if match:
            game = library / "steamapps" / "common" / match.group(1)
            if (game / "DSClient.exe").is_file():
                normalized = norm(game)
                key = os.path.normcase(str(normalized))
                if key not in seen:
                    seen.add(key)
                    result.append(normalized)
    return result


def can_install_guard(root: Path) -> bool:
    games = steam_games()
    return len(games) == 1 and same_path(games[0], root)


def install_guard(
    release: Release,
    root: Path,
    reporter: Reporter,
    local_asset: Path | None = None,
    *,
    enable_now: bool = True,
) -> str:
    if release.guard is None:
        raise InstallerError(f"Release {release.tag} does not contain Update Guard.")
    if not can_install_guard(root):
        raise InstallerError("Update Guard can only be enabled for the selected registered Steam installation.")
    reporter.send(2, "guard", "Preparing Update Guard", release.guard.name)
    with tempfile.TemporaryDirectory(prefix="HiddenHeroesGuard_") as raw_work:
        work = Path(raw_work)
        archive = work / release.guard.name
        download(release.guard, archive, reporter, 5, 70, local_asset)
        reporter.send(72, "guard", "Verifying Guard SHA-256", release.guard.sha256)
        actual = sha256(archive)
        if actual != release.guard.sha256:
            raise InstallerError("Update Guard SHA-256 verification failed.")
        with zipfile.ZipFile(archive) as package:
            infos = package.infolist()
            for info in infos:
                raw = info.filename.replace("\\", "/")
                path = PurePosixPath(raw)
                if path.is_absolute() or any(part in (".", "..") or ":" in part for part in path.parts):
                    raise InstallerError(f"Unsafe Update Guard ZIP path: {raw!r}.")
            matches = [info for info in infos if PurePosixPath(info.filename).name.casefold() == "dragonswordguard.exe"]
            if len(matches) != 1:
                raise InstallerError("Update Guard ZIP must contain exactly one DragonSwordGuard.exe.")
            staged_executable = work / "DragonSwordGuard.exe"
            with package.open(matches[0]) as source, staged_executable.open("wb") as target:
                shutil.copyfileobj(source, target, 1024 * 1024)
        executable = root / "DragonSwordGuard.exe"
        temporary = root / f"DragonSwordGuard.exe.new-{uuid.uuid4().hex}"
        try:
            shutil.copy2(staged_executable, temporary)
            os.replace(temporary, executable)
        finally:
            temporary.unlink(missing_ok=True)
        action = "install" if enable_now else "setup"
        status_text = "Enabling update protection" if enable_now else "Creating Guard shortcuts"
        reporter.send(80, "guard", status_text, str(root))
        completed = subprocess.run([str(executable), action, "--quiet"], creationflags=CREATE_NO_WINDOW, timeout=240)
        if completed.returncode:
            raise InstallerError(f"Update Guard {action} failed with exit code {completed.returncode}.")
        if enable_now and not guard_active_for(root):
            raise InstallerError("Update Guard did not confirm protection for the selected game.")
        final_status = "Update protection enabled" if enable_now else "Guard ready; protection remains off"
        reporter.send(100, "guard", final_status, str(root))
        return actual


class InstallerApp(tk.Tk):
    STEPS = (("release", "RELEASE"), ("download", "DOWNLOAD"), ("install", "INSTALL"), ("guard", "UPDATE GUARD"))

    def __init__(self, game_root: Path):
        super().__init__()
        self.title("Hidden Heroes Installer")
        self.geometry("780x600")
        self.resizable(False, False)
        self.configure(bg=COLORS["bg"])
        self.events: queue.Queue = queue.Queue()
        self.cancel_event = threading.Event()
        self.worker: threading.Thread | None = None
        self.result: InstallResult | None = None
        self.release: Release | None = None
        self.game_root = game_root
        self.folder = tk.StringVar(value=str(game_root))
        self.status = tk.StringVar(value="Ready to install")
        self.detail = tk.StringVar(value="The GitHub SHA-256 is verified before game files are changed.")
        self.progress = tk.DoubleVar(value=0)
        self.step_labels: dict[str, tk.Label] = {}
        self._styles()
        self._build()
        self.after(100, self._drain)

    def _styles(self) -> None:
        style = ttk.Style(self)
        style.theme_use("clam")
        style.configure("HiddenHeroes.Horizontal.TProgressbar", troughcolor=COLORS["surface2"], background=COLORS["gold"], bordercolor=COLORS["surface2"], thickness=12)

    def _build(self) -> None:
        header = tk.Frame(self, bg=COLORS["surface"], height=104)
        header.pack(fill=tk.X)
        header.pack_propagate(False)
        tk.Label(header, text="HIDDEN HEROES", bg=COLORS["surface"], fg=COLORS["text"], font=("Segoe UI Semibold", 19)).place(x=30, y=24)
        tk.Label(header, text="VERIFIED GITHUB INSTALLER  •  DRAGONSWORD: AWAKENING", bg=COLORS["surface"], fg=COLORS["muted"], font=("Segoe UI", 9)).place(x=32, y=61)
        tk.Label(header, text=f"v{VERSION}", bg="#253044", fg=COLORS["muted"], font=("Segoe UI Semibold", 9), padx=12, pady=5).place(x=697, y=37)

        content = tk.Frame(self, bg=COLORS["bg"])
        content.pack(fill=tk.BOTH, expand=True, padx=30, pady=24)
        tk.Label(content, text="GAME FOLDER", bg=COLORS["bg"], fg=COLORS["muted"], font=("Segoe UI Semibold", 9)).pack(anchor="w")
        row = tk.Frame(content, bg=COLORS["bg"])
        row.pack(fill=tk.X, pady=(8, 20))
        entry = tk.Entry(row, textvariable=self.folder, state="readonly", readonlybackground=COLORS["surface2"], fg=COLORS["text"], relief=tk.FLAT, font=("Segoe UI", 10))
        entry.pack(side=tk.LEFT, fill=tk.X, expand=True, ipady=10)

        steps = tk.Frame(content, bg=COLORS["surface"], highlightbackground=COLORS["border"], highlightthickness=1)
        steps.pack(fill=tk.X, pady=(0, 20))
        for key, label in self.STEPS:
            item = tk.Label(steps, text=f"○  {label}", bg=COLORS["surface"], fg=COLORS["faint"], font=("Segoe UI Semibold", 8), padx=10, pady=13)
            item.pack(side=tk.LEFT, expand=True)
            self.step_labels[key] = item

        tk.Label(content, textvariable=self.status, bg=COLORS["bg"], fg=COLORS["text"], font=("Segoe UI Semibold", 13)).pack(anchor="w")
        ttk.Progressbar(content, variable=self.progress, maximum=100, style="HiddenHeroes.Horizontal.TProgressbar").pack(fill=tk.X, pady=(12, 8))
        tk.Label(content, textvariable=self.detail, bg=COLORS["bg"], fg=COLORS["muted"], font=("Segoe UI", 9), anchor="w").pack(fill=tk.X)

        self.log_box = tk.Text(content, height=9, bg="#070A10", fg="#BBC5D4", insertbackground=COLORS["text"], relief=tk.FLAT, font=("Consolas", 8), state=tk.DISABLED, padx=10, pady=8)
        self.log_box.pack(fill=tk.BOTH, expand=True, pady=(16, 18))

        buttons = tk.Frame(content, bg=COLORS["bg"])
        buttons.pack(fill=tk.X)
        self.cancel = tk.Button(buttons, text="Cancel", command=self.cancel_event.set, state=tk.DISABLED, bg=COLORS["surface2"], fg=COLORS["muted"], disabledforeground=COLORS["faint"], relief=tk.FLAT, bd=0, padx=22, pady=11, font=("Segoe UI Semibold", 10))
        self.cancel.pack(side=tk.LEFT)
        self.install = tk.Button(buttons, text="Download and install", command=self._start, bg=COLORS["gold"], fg="#171109", activebackground=COLORS["gold2"], activeforeground="#171109", relief=tk.FLAT, bd=0, padx=28, pady=11, font=("Segoe UI Semibold", 10), cursor="hand2")
        self.install.pack(side=tk.RIGHT)
        tk.Label(
            content,
            text="github.com/Sai-Hakuto  •  SHA-256 verified  •  no administrator rights",
            bg=COLORS["bg"], fg=COLORS["faint"], font=("Segoe UI", 8),
        ).pack(anchor="e", pady=(8, 0))

    def _append(self, text: str) -> None:
        self.log_box.configure(state=tk.NORMAL)
        self.log_box.insert(tk.END, text + "\n")
        self.log_box.see(tk.END)
        self.log_box.configure(state=tk.DISABLED)

    def _progress_callback(self, percent, stage, status, detail) -> None:
        self.events.put(("progress", percent, stage, status, detail))

    def _start(self) -> None:
        try:
            root = validate_game_root(self.game_root, False)
        except Exception as error:
            messagebox.showerror("Hidden Heroes Installer", str(error), parent=self)
            return
        allow_guard = False
        if guard_active_for(root):
            allow_guard = messagebox.askyesno(
                "Update Guard is active",
                "Update Guard must be disabled during installation. Disable it now and restore protection automatically when installation finishes?",
                parent=self,
            )
            if not allow_guard:
                return
        self.install.configure(state=tk.DISABLED)
        self.cancel.configure(state=tk.NORMAL)
        self.cancel_event.clear()
        self.worker = threading.Thread(target=self._main_worker, args=(root, allow_guard), daemon=True)
        self.worker.start()

    def _main_worker(self, root: Path, allow_guard: bool) -> None:
        try:
            def main_progress(percent, stage, status, detail):
                self._progress_callback(int(percent * 0.95), stage, status, detail)
            result, release = install_main(root, Reporter(main_progress, self.cancel_event), allow_guard_disable=allow_guard)
            self.events.put(("main_done", result, release))
        except Exception as error:
            self.events.put(("error", error))

    def _guard_worker(self, root: Path, release: Release, enable_now: bool) -> None:
        try:
            def guard_progress(percent, _stage, status, detail):
                self._progress_callback(95 + int(percent * 0.05), "guard", status, detail)
            digest = install_guard(release, root, Reporter(guard_progress), enable_now=enable_now)
            self.events.put(("guard_done", digest, enable_now))
        except Exception as error:
            self.events.put(("guard_error", error))

    def _set_stage(self, active: str) -> None:
        active = {
            "preflight": "release", "extract": "install", "verify": "install",
            "backup": "install", "complete": "install",
        }.get(active, active)
        keys = [key for key, _ in self.STEPS]
        active_index = keys.index(active) if active in keys else -1
        for index, (key, label) in enumerate(self.STEPS):
            if index < active_index:
                text, color = f"●  {label}", COLORS["green"]
            elif index == active_index:
                text, color = f"●  {label}", COLORS["gold"]
            else:
                text, color = f"○  {label}", COLORS["faint"]
            self.step_labels[key].configure(text=text, fg=color)

    def _drain(self) -> None:
        try:
            while True:
                event = self.events.get_nowait()
                kind = event[0]
                if kind == "progress":
                    _, percent, stage, status, detail = event
                    self.progress.set(percent)
                    self.status.set(status)
                    self.detail.set(detail)
                    self._set_stage(stage)
                    self._append(f"[{percent:3}%] {status}  {detail}")
                elif kind == "main_done":
                    _, self.result, self.release = event
                    self.cancel.configure(state=tk.DISABLED)
                    root = Path(self.result.game_root)
                    if self.result.warning:
                        messagebox.showwarning("Hidden Heroes installed with a warning", self.result.warning, parent=self)
                    if not self.result.guard_was_restored and self.release.guard and can_install_guard(root):
                        enable_now = messagebox.askyesno("Enable Update Guard?", "Hidden Heroes is installed. Enable DragonSword Update Guard now?\n\nSafe Launch and Toggle shortcuts will be created either way. Choosing No leaves protection off until you use Toggle Update Protection. Safe Launch only starts the game without Steam.", parent=self)
                        self.worker = threading.Thread(target=self._guard_worker, args=(root, self.release, enable_now), daemon=True)
                        self.worker.start()
                        continue
                    self._finish(self.result.guard_was_restored, self.result.guard_was_restored)
                elif kind == "guard_done":
                    self._finish(bool(event[2]), True)
                elif kind == "guard_error":
                    messagebox.showwarning("Hidden Heroes installed; Guard was not enabled", str(event[1]), parent=self)
                    self._finish(False, False)
                elif kind == "error":
                    self.cancel.configure(state=tk.DISABLED)
                    self.install.configure(state=tk.NORMAL)
                    self.status.set("Installation stopped safely")
                    self.detail.set(str(event[1]))
                    self._append(f"ERROR: {event[1]}")
                    messagebox.showerror("Hidden Heroes Installer", f"{event[1]}\n\nLog: {LOG_PATH}", parent=self)
        except queue.Empty:
            pass
        self.after(100, self._drain)

    def _finish(self, guard_enabled: bool, guard_ready: bool) -> None:
        assert self.result is not None
        self.progress.set(100)
        self.status.set("Hidden Heroes installation complete")
        self.detail.set(f"{self.result.files_verified} files verified • Backup: {self.result.backup_root}")
        for key, label in self.STEPS:
            if key != "guard" or guard_ready or self.result.guard_was_restored:
                self.step_labels[key].configure(text=f"●  {label}", fg=COLORS["green"])
        self.install.configure(text="Installed", state=tk.DISABLED)
        self.cancel.configure(state=tk.DISABLED)
        message = (
            f"Hidden Heroes {self.result.tag} installed successfully.\n\n"
            f"{self.result.files_verified} files verified.\n"
            f"Update Guard: {'enabled' if guard_enabled or self.result.guard_was_restored else ('ready, protection off' if guard_ready else 'not installed')}.\n\n"
            f"Backup: {self.result.backup_root}"
        )
        messagebox.showinfo("Installation complete", message, parent=self)


def write_result(path: Path | None, value: dict) -> None:
    text = json.dumps(value, ensure_ascii=False, indent=2)
    if path:
        write_json(path, value)
    elif sys.stdout is not None:
        print(text)


def executable_game_root() -> Path | None:
    base = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else Path.cwd().resolve()
    required = (
        base / "DSClient.exe",
        base / "DS" / "Binaries" / "Win64" / "DSClient-Win64-Shipping.exe",
        base / "DS" / "Content" / "Paks",
    )
    if required[0].is_file() and required[1].is_file() and required[2].is_dir():
        return norm(base)
    return None


def startup_error(message: str) -> None:
    window = tk.Tk()
    window.withdraw()
    try:
        messagebox.showerror("Hidden Heroes Installer — Game not found", message, parent=window)
    finally:
        window.destroy()


def game_not_found_message() -> str:
    return (
        "Dragon Sword: Awakening was not found. Place Hidden_Heroes_Installer.exe in the game folder "
        "next to DSClient.exe and run it again. No files were changed."
    )


def run_uninstall_gui(root: Path) -> int:
    receipt = receipt_path(root)
    if not receipt.is_file():
        messagebox.showinfo("Uninstall Hidden Heroes", "Hidden Heroes is not installed in this game folder. Nothing was changed.")
        return 0
    value = json.loads(receipt.read_text(encoding="utf-8"))
    if value.get("status") != "installed":
        messagebox.showinfo("Uninstall Hidden Heroes", "Hidden Heroes is already uninstalled. Nothing was changed.")
        return 0
    protected = guard_active_for(root)
    guard_note = "\n\nUpdate Guard will be disabled temporarily and restored afterwards." if protected else ""
    confirmed = messagebox.askyesno(
        "Uninstall Hidden Heroes?",
        "Move all Hidden Heroes files out of the game folder?\n\n"
        "Files will be preserved under deleted_hidden_heroes with their original folder hierarchy. "
        "Vanilla game files and DS\\Saved will not be changed."
        + guard_note,
    )
    if not confirmed:
        return 0
    try:
        result = uninstall_main(root, allow_guard_disable=protected)
    except Exception as error:
        messagebox.showerror("Hidden Heroes uninstall stopped", f"{error}\n\nLog: {LOG_PATH}")
        return 1
    messagebox.showinfo(
        "Hidden Heroes uninstalled",
        f"Hidden Heroes was removed from the game.\n\n"
        f"Files moved: {result.files_moved}\n"
        f"Previous files restored: {result.files_restored}\n"
        f"Preserved at: {result.deleted_root}",
    )
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Hidden Heroes Installer")
    parser.add_argument("--non-interactive", action="store_true")
    parser.add_argument("--check-only", action="store_true")
    parser.add_argument("--uninstall", action="store_true")
    parser.add_argument("--game-dir", type=Path)
    parser.add_argument("--skip-system-integration", action="store_true")
    parser.add_argument("--allow-guard-disable", action="store_true")
    parser.add_argument("--enable-guard", action="store_true")
    parser.add_argument("--release-metadata", type=Path)
    parser.add_argument("--asset-file", type=Path)
    parser.add_argument("--guard-asset-file", type=Path)
    parser.add_argument("--result-json", type=Path)
    return parser.parse_args()


def main() -> int:
    arguments = parse_args()
    root = executable_game_root()
    if getattr(sys, "frozen", False):
        invalid_override = arguments.game_dir is not None and root is not None and not same_path(arguments.game_dir, root)
        if root is None or invalid_override:
            message = game_not_found_message()
            if arguments.non_interactive:
                if sys.stdout is not None:
                    print(json.dumps({"status": "FAIL", "error": message}, ensure_ascii=False, indent=2))
            else:
                startup_error(message.replace(". Place", ".\n\nPlace"))
            return 2
        arguments.game_dir = root
    if not arguments.non_interactive:
        if root is None:
            startup_error(game_not_found_message().replace(". Place", ".\n\nPlace"))
            return 2
        initialize_log()
        if arguments.uninstall:
            return run_uninstall_gui(root)
        app = InstallerApp(root)
        app.mainloop()
        return 0
    initialize_log()
    try:
        if arguments.check_only:
            release = resolve_release(arguments.release_metadata)
            write_result(arguments.result_json, {"status": "PASS", "release": asdict(release), "log_path": str(LOG_PATH)})
            return 0
        if not arguments.game_dir:
            raise InstallerError("--game-dir is required in non-interactive mode.")
        if arguments.uninstall:
            result = uninstall_main(arguments.game_dir, allow_guard_disable=arguments.allow_guard_disable)
            value = asdict(result)
            value["status"] = "PASS"
            write_result(arguments.result_json, value)
            return 0
        reporter = Reporter(lambda p, s, status, detail: log(f"cli [{p}] {s} {status} {detail}"))
        result, release = install_main(
            arguments.game_dir, reporter,
            skip_system=arguments.skip_system_integration,
            allow_guard_disable=arguments.allow_guard_disable,
            metadata_path=arguments.release_metadata,
            asset_file=arguments.asset_file,
        )
        guard_digest = ""
        if arguments.enable_guard:
            guard_digest = install_guard(release, Path(result.game_root), reporter, arguments.guard_asset_file)
        value = asdict(result)
        value.update({"status": "PASS", "guard_sha256": guard_digest})
        write_result(arguments.result_json, value)
        return 0
    except Exception as error:
        log(f"ERROR {error!r}")
        write_result(arguments.result_json, {"status": "FAIL", "error": str(error), "log_path": str(LOG_PATH)})
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
