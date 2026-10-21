"""Find or download the official nats-server binary."""

from __future__ import annotations

import hashlib
import os
import platform
import shutil
import stat
import subprocess
import tarfile
import tempfile
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

NATS_SERVER_VERSION = "2.15.1"
_RELEASE = f"https://github.com/nats-io/nats-server/releases/download/v{NATS_SERVER_VERSION}"


def release_asset(version: str, system: str, machine: str) -> tuple[str, str]:
    """Return ``(archive name, binary name inside it)`` for this operating system."""
    os_name = {"linux": "linux", "darwin": "darwin", "win32": "windows"}.get(system)
    arch = {
        "x86_64": "amd64",
        "amd64": "amd64",
        "aarch64": "arm64",
        "arm64": "arm64",
        "x86": "386",
    }.get(machine.lower())
    if not os_name or not arch:
        raise RuntimeError(f"No NATS server build is published for {system} {machine}")
    binary = "nats-server.exe" if os_name == "windows" else "nats-server"
    ext = "zip" if os_name == "windows" else "tar.gz"
    return f"nats-server-v{version}-{os_name}-{arch}.{ext}", binary


def parse_checksums(text: str) -> dict[str, str]:
    found: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        if len(parts) < 2:
            continue
        name = parts[-1].lstrip("*")
        if "/" in name:
            name = name.rsplit("/", 1)[-1]
        found[name] = parts[0].lower()
    return found


def ensure_nats_server(bin_dir: Path, status=None) -> Path:
    """Return a usable nats-server, downloading the official build when needed."""
    override = os.environ.get("NATS_SERVER")
    if override:
        path = Path(override)
        if _binary_ok(path):
            return path
        raise RuntimeError(f"NATS_SERVER does not point at a working nats-server: {override}")
    found = shutil.which("nats-server")
    if found and _binary_ok(Path(found)):
        return Path(found)
    bin_dir.mkdir(parents=True, exist_ok=True)
    binary_name = "nats-server.exe" if os.name == "nt" else "nats-server"
    dest = bin_dir / binary_name
    if _binary_ok(dest):
        return dest
    _download_release(dest, status)
    if not _binary_ok(dest):
        raise RuntimeError("The downloaded nats-server did not start")
    return dest


def _binary_ok(path: Path) -> bool:
    if not path.is_file() or not os.access(path, os.X_OK):
        return False
    try:
        result = subprocess.run(
            [str(path), "--version"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    output = f"{result.stdout} {result.stderr}".lower()
    return result.returncode == 0 and "nats-server" in output


def _download_release(dest: Path, status) -> None:
    system = {"linux": "linux", "darwin": "darwin", "windows": "win32"}.get(platform.system().lower())
    if system is None:
        raise RuntimeError(f"No NATS server build is published for {platform.system()}")
    asset, binary_name = release_asset(NATS_SERVER_VERSION, system, platform.machine())
    if status:
        status(f"Downloading NATS server {NATS_SERVER_VERSION}…")
    checksums = _fetch_text(f"{_RELEASE}/SHA256SUMS")
    digest = parse_checksums(checksums).get(asset)
    if not digest:
        raise RuntimeError(f"No checksum was published for {asset}")
    with tempfile.TemporaryDirectory(prefix="nats-emu-") as temp_name:
        archive = Path(temp_name) / asset
        _download(f"{_RELEASE}/{asset}", archive, status, label=f"NATS server {NATS_SERVER_VERSION}")
        actual = _sha256(archive)
        if actual != digest:
            raise RuntimeError("The NATS server download did not match its checksum")
        extracted = _extract_binary(archive, binary_name, Path(temp_name))
        staged = dest.with_suffix(dest.suffix + ".download")
        shutil.copyfile(extracted, staged)
        staged.chmod(staged.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        os.replace(staged, dest)


def _extract_binary(archive: Path, binary_name: str, temp: Path) -> Path:
    folder = temp / "unpack"
    folder.mkdir()
    if archive.suffix == ".zip":
        with zipfile.ZipFile(archive) as bundle:
            bundle.extractall(folder)
    else:
        with tarfile.open(archive, "r:gz") as bundle:
            bundle.extractall(folder, filter="data")
    matches = [path for path in folder.rglob(binary_name) if path.is_file()]
    if not matches:
        raise RuntimeError(f"The archive did not contain {binary_name}")
    return matches[0]


def _fetch_text(url: str) -> str:
    try:
        with _open(url) as response:
            return response.read().decode()
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Could not download {url}: {exc.reason}") from exc


def _download(url: str, dest: Path, status, label: str) -> None:
    try:
        with _open(url) as response, dest.open("wb") as handle:
            total = int(response.headers.get("Content-Length") or 0)
            read = 0
            reported = -10
            while True:
                chunk = response.read(1 << 16)
                if not chunk:
                    break
                handle.write(chunk)
                read += len(chunk)
                if total and status:
                    percent = int(read * 100 / total)
                    if percent >= reported + 10:
                        reported = percent
                        status(f"Downloading {label}… {percent}%")
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Could not download {url}: {exc.reason}") from exc


def _open(url: str):
    request = urllib.request.Request(url, headers={"User-Agent": "nats-emu"})
    return urllib.request.urlopen(request, timeout=60)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()
