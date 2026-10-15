"""Install Android apps on the device: single APKs and split-APK bundles.

Meant for Android without a Play Store (redroid): install Aurora Store from
F-Droid with one tap, then install the game from Google Play through it. Or
install an .apk / .xapk / .apks file from a link or an upload.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

from . import __version__
from .device.base import Device, DeviceError

log = logging.getLogger(__name__)

FDROID_API = "https://f-droid.org/api/v1/packages/{package}"
FDROID_APK = "https://f-droid.org/repo/{package}_{code}.apk"
PACKAGE_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)+$")
MAX_DOWNLOAD = 4 * 1024**3
USER_AGENT = f"dokkan-idle-farmer/{__version__}"

Opener = Callable[..., Any]
DeviceRunner = Callable[[Callable[[Device], Any]], Any]


class InstallError(ValueError):
    """The app could not be downloaded or unpacked."""


def check_source(source: str) -> str:
    """Validate ``fdroid:<package>`` or an http(s) link without going online."""
    source = source.strip()
    if source.startswith("fdroid:"):
        package = source.removeprefix("fdroid:")
        if not PACKAGE_RE.match(package):
            raise InstallError(f"Invalid package name {package!r}")
        return source
    parts = urllib.parse.urlsplit(source)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        raise InstallError("Enter an http(s) link to an .apk, .xapk or .apks file")
    return source


def resolve_source(source: str, opener: Opener = urllib.request.urlopen) -> str:
    """Turn ``fdroid:<package>`` or an http(s) link into a download URL."""
    source = check_source(source)
    if not source.startswith("fdroid:"):
        return source
    package = source.removeprefix("fdroid:")
    request = urllib.request.Request(
        FDROID_API.format(package=package), headers={"User-Agent": USER_AGENT}
    )
    with opener(request, timeout=30) as response:
        data = json.load(response)
    code = data.get("suggestedVersionCode") or max(
        (p.get("versionCode", 0) for p in data.get("packages", [])), default=0
    )
    if not code:
        raise InstallError(f"F-Droid has no build of {package}")
    return FDROID_APK.format(package=package, code=int(code))


def _safe_name(name: str, default: str = "download.apk") -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]", "_", name).strip("._")[:120]
    return cleaned or default


def download(
    url: str,
    directory: Path,
    progress: Callable[[int, int], None] | None = None,
    opener: Opener = urllib.request.urlopen,
) -> Path:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with opener(request, timeout=60) as response:
        total = int(response.headers.get("Content-Length") or 0)
        if total > MAX_DOWNLOAD:
            raise InstallError("The file is too large")
        disposition = response.headers.get("Content-Disposition") or ""
        match = re.search(r"filename\*?=(?:UTF-8'')?\"?([^\";]+)", disposition)
        name = match.group(1) if match else PurePosixPath(urllib.parse.urlsplit(url).path).name
        target = directory / _safe_name(urllib.parse.unquote(name))
        done = 0
        with open(target, "wb") as fh:
            while chunk := response.read(1 << 20):
                done += len(chunk)
                if done > MAX_DOWNLOAD:
                    raise InstallError("The file is too large")
                fh.write(chunk)
                if progress is not None:
                    progress(done, total)
    return target


@dataclass
class Bundle:
    apks: list[Path]
    #: (local file, path on the device) for expansion files in .xapk bundles
    obbs: list[tuple[Path, str]] = field(default_factory=list)


def unpack(path: Path, workdir: Path) -> Bundle:
    """An APK as is, or the APKs (and OBB files) inside a bundle archive."""
    if not zipfile.is_zipfile(path):
        raise InstallError("This is not an APK or an app bundle (.xapk / .apks)")
    with zipfile.ZipFile(path) as archive:
        if "AndroidManifest.xml" in archive.namelist():
            return Bundle([path])  # a plain APK is a zip with a manifest
        bundle = Bundle([])
        try:
            for info in archive.infolist():
                if info.is_dir():
                    continue
                name = info.filename.replace("\\", "/")
                lower = name.lower()
                if lower.endswith(".apk"):
                    target = workdir / f"{len(bundle.apks):02d}-{_safe_name(Path(name).name)}"
                elif lower.endswith(".obb") and "android/obb/" in lower:
                    relative = PurePosixPath(name[lower.index("android/obb/") :])
                    if ".." in relative.parts:
                        continue
                    target = workdir / f"obb-{len(bundle.obbs):02d}-{_safe_name(relative.name)}"
                    bundle.obbs.append((target, f"/sdcard/{relative}"))
                else:
                    continue
                with archive.open(info) as src, open(target, "wb") as dst:
                    shutil.copyfileobj(src, dst)
                if lower.endswith(".apk"):
                    bundle.apks.append(target)
        except RuntimeError as exc:  # zipfile: "File ... is encrypted, password required"
            raise InstallError(
                "This bundle is encrypted (APKMirror .apkm files are): use an .xapk / .apks "
                "file or install the game through Aurora Store"
            ) from exc
    if not bundle.apks:
        raise InstallError("No .apk file inside this archive")
    return bundle


@dataclass
class InstallJob:
    id: int
    source: str
    state: str = "queued"  # queued, downloading, installing, done, error
    message: str = ""
    downloaded: int = 0
    total: int = 0
    started_at: float = field(default_factory=time.time)
    finished_at: float | None = None


class InstallManager:
    """Runs one install at a time in the background and reports its progress."""

    def __init__(
        self,
        workdir: Path,
        run_on_device: DeviceRunner,
        opener: Opener = urllib.request.urlopen,
    ) -> None:
        self.workdir = Path(workdir)
        # Leftovers of an install that was cut short by a restart.
        shutil.rmtree(self.workdir, ignore_errors=True)
        self.run_on_device = run_on_device
        self.opener = opener
        self._lock = threading.Lock()
        self._job: InstallJob | None = None
        self._thread: threading.Thread | None = None
        self._next_id = 1

    @property
    def busy(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def current(self) -> dict[str, Any] | None:
        with self._lock:
            return None if self._job is None else asdict(self._job)

    def upload_path(self, filename: str) -> Path:
        """A fresh private file in the work directory for an uploaded app."""
        self.workdir.mkdir(parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(
            dir=self.workdir, prefix="upload-", suffix=f"-{_safe_name(filename)}"
        )
        os.close(fd)
        return Path(name)

    def start_url(self, source: str) -> dict[str, Any]:
        source = check_source(source)
        return self._start(source, lambda job, tmp: self._fetch(job, source, tmp))

    def start_file(self, path: Path, display_name: str) -> dict[str, Any]:
        def provide(job: InstallJob, tmp: Path) -> Path:
            return path

        return self._start(display_name, provide, cleanup=path)

    def wait(self, timeout: float | None = None) -> None:
        thread = self._thread
        if thread is not None:
            thread.join(timeout)

    # ------------------------------------------------------------- internals
    def _start(
        self,
        source: str,
        provide: Callable[[InstallJob, Path], Path],
        cleanup: Path | None = None,
    ) -> dict[str, Any]:
        with self._lock:
            if self.busy:
                if cleanup is not None:
                    cleanup.unlink(missing_ok=True)
                raise RuntimeError("Another install is still running")
            job = InstallJob(self._next_id, source)
            self._next_id += 1
            self._job = job
            self._thread = threading.Thread(
                target=self._run, args=(job, provide, cleanup), name="installer", daemon=True
            )
            self._thread.start()
            return asdict(job)

    def _set(self, job: InstallJob, **changes: Any) -> None:
        with self._lock:
            for key, value in changes.items():
                setattr(job, key, value)

    def _fail(self, job: InstallJob, message: str) -> None:
        self._set(job, state="error", message=message, finished_at=time.time())
        log.warning("Install of %s failed: %s", job.source, message)

    def _fetch(self, job: InstallJob, source: str, tmp: Path) -> Path:
        url = resolve_source(source, self.opener)
        self._set(job, state="downloading", message=f"Downloading {url}")
        log.info("Downloading %s", url)

        def progress(done: int, total: int) -> None:
            self._set(job, downloaded=done, total=total)

        return download(url, tmp, progress, self.opener)

    def _run(
        self,
        job: InstallJob,
        provide: Callable[[InstallJob, Path], Path],
        cleanup: Path | None,
    ) -> None:
        self.workdir.mkdir(parents=True, exist_ok=True)
        try:
            with tempfile.TemporaryDirectory(dir=self.workdir) as tmp_name:
                tmp = Path(tmp_name)
                path = provide(job, tmp)
                self._set(job, state="installing", message=f"Installing {path.name}")
                bundle = unpack(path, tmp)
                log.info("Installing %s (%d APK file(s))", path.name, len(bundle.apks))
                self.run_on_device(lambda d: d.install(bundle.apks))
                for local, remote in bundle.obbs:
                    self.run_on_device(lambda d, local=local, remote=remote: d.push(local, remote))
            message = f"Installed {job.source}"
            self._set(job, state="done", message=message, finished_at=time.time())
            log.info("%s", message)
        except urllib.error.HTTPError as exc:
            self._fail(job, f"Download failed: HTTP {exc.code} {exc.reason}")
        except urllib.error.URLError as exc:
            self._fail(job, f"Download failed: {exc.reason}")
        except (InstallError, DeviceError, OSError, ValueError) as exc:
            self._fail(job, str(exc) or type(exc).__name__)
        finally:
            if cleanup is not None:
                cleanup.unlink(missing_ok=True)
