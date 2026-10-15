"""App installs: sources, downloads, bundle unpacking and the background job."""

from __future__ import annotations

import io
import json
import threading
import urllib.error
import zipfile
from pathlib import Path

import pytest

from dokkan_farmer.device.base import DeviceError
from dokkan_farmer.installer import (
    InstallError,
    InstallManager,
    check_abis,
    check_source,
    download,
    native_abis,
    resolve_source,
    unpack,
)

AURORA_API = "https://f-droid.org/api/v1/packages/com.aurora.store"
AURORA_APK = "https://f-droid.org/repo/com.aurora.store_70.apk"


class FakeResponse(io.BytesIO):
    def __init__(self, data: bytes, headers: dict[str, str] | None = None) -> None:
        super().__init__(data)
        self.headers = headers or {}


def make_opener(routes: dict[str, tuple[bytes, dict[str, str]] | Exception]):
    seen: list[tuple[str, str | None]] = []

    def opener(request, timeout=None):
        seen.append((request.full_url, request.get_header("User-agent")))
        route = routes[request.full_url]
        if isinstance(route, Exception):
            raise route
        body, headers = route
        return FakeResponse(body, headers)

    return opener, seen


def make_zip(path: Path, files: dict[str, bytes]) -> Path:
    with zipfile.ZipFile(path, "w") as archive:
        for name, data in files.items():
            archive.writestr(name, data)
    return path


def make_apk(path: Path) -> Path:
    return make_zip(path, {"AndroidManifest.xml": b"\x03\x00", "classes.dex": b"dex"})


def fdroid_json(**data) -> bytes:
    return json.dumps({"packageName": "com.aurora.store", **data}).encode()


# ---------------------------------------------------------------- sources
def test_resolve_fdroid_source_uses_the_suggested_version():
    opener, seen = make_opener({AURORA_API: (fdroid_json(suggestedVersionCode=70), {})})
    assert resolve_source(" fdroid:com.aurora.store ", opener) == AURORA_APK
    assert seen[0][0] == AURORA_API and seen[0][1].startswith("dokkan-idle-farmer/")


def test_resolve_fdroid_source_falls_back_to_the_newest_build():
    body = fdroid_json(packages=[{"versionCode": 68}, {"versionCode": 70}])
    opener, _ = make_opener({AURORA_API: (body, {})})
    assert resolve_source("fdroid:com.aurora.store", opener) == AURORA_APK
    opener, _ = make_opener({AURORA_API: (fdroid_json(packages=[]), {})})
    with pytest.raises(InstallError, match="no build"):
        resolve_source("fdroid:com.aurora.store", opener)


@pytest.mark.parametrize(
    "source",
    ["fdroid:bad package", "fdroid:", "ftp://example.com/x.apk", "file:///etc/passwd", "game.apk"],
)
def test_invalid_sources_are_rejected_offline(source):
    with pytest.raises(InstallError):
        check_source(source)


def test_links_pass_through():
    url = "https://example.com/files/dokkan.xapk?token=1"
    assert resolve_source(url, opener=None) == url


# --------------------------------------------------------------- download
def test_download_names_the_file_and_reports_progress(tmp_path):
    data = b"x" * (3 << 20)
    headers = {
        "Content-Length": str(len(data)),
        "Content-Disposition": 'attachment; filename="Dokkan Battle_5.0.xapk"',
    }
    opener, _ = make_opener({"https://cdn.example/d": (data, headers)})
    progress: list[tuple[int, int]] = []
    path = download("https://cdn.example/d", tmp_path, lambda *a: progress.append(a), opener)
    assert path.name == "Dokkan_Battle_5.0.xapk" and path.read_bytes() == data
    assert progress[-1] == (len(data), len(data)) and len(progress) == 3


def test_download_takes_the_name_from_the_link_and_refuses_huge_files(tmp_path):
    opener, _ = make_opener({"https://x.example/a/../my%20app.apk": (b"apk", {})})
    path = download("https://x.example/a/../my%20app.apk", tmp_path, opener=opener)
    assert path.parent == tmp_path and path.name == "my_app.apk"
    opener, _ = make_opener({"https://x.example/big": (b"", {"Content-Length": str(5 << 30)})})
    with pytest.raises(InstallError, match="too large"):
        download("https://x.example/big", tmp_path, opener=opener)


# ----------------------------------------------------------------- unpack
def test_unpack_plain_apk(tmp_path):
    apk = make_apk(tmp_path / "app.apk")
    bundle = unpack(apk, tmp_path)
    assert bundle.apks == [apk] and bundle.obbs == []


def test_unpack_xapk_with_splits_and_obb(tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    xapk = make_zip(
        tmp_path / "game.xapk",
        {
            "manifest.json": b"{}",
            "icon.png": b"png",
            "com.example.game.apk": b"base",
            "config.arm64_v8a.apk": b"abi",
            "../../evil.apk": b"evil",
            "Android/obb/com.example.game/main.7.com.example.game.obb": b"obb",
            "Android/obb/../../../etc/passwd.obb": b"nope",
        },
    )
    bundle = unpack(xapk, work)
    assert [p.read_bytes() for p in bundle.apks] == [b"base", b"abi", b"evil"]
    assert all(p.parent == work for p in bundle.apks)
    assert [(local.read_bytes(), remote) for local, remote in bundle.obbs] == [
        (b"obb", "/sdcard/Android/obb/com.example.game/main.7.com.example.game.obb")
    ]


def test_unpack_rejects_non_apps(tmp_path):
    with pytest.raises(InstallError, match=r"No \.apk"):
        unpack(make_zip(tmp_path / "x.zip", {"readme.txt": b"hi"}), tmp_path)
    (tmp_path / "notes.txt").write_text("not a zip")
    with pytest.raises(InstallError, match="not an APK"):
        unpack(tmp_path / "notes.txt", tmp_path)


def test_unpack_explains_encrypted_bundles(tmp_path, monkeypatch):
    archive = make_zip(tmp_path / "game.apkm", {"base.apk": b"base"})

    def encrypted(*args, **kwargs):
        raise RuntimeError("File 'base.apk' is encrypted, password required for extraction")

    monkeypatch.setattr(zipfile.ZipFile, "open", encrypted)
    with pytest.raises(InstallError, match="encrypted"):
        unpack(archive, tmp_path)


# ---------------------------------------------------------------- manager
class FakeDevice:
    def __init__(self) -> None:
        self.runnable: list[str] | None = None
        self.installed: list[list[bytes]] = []
        self.pushed: list[tuple[bytes, str]] = []
        self.fail: str | None = None
        self.gate: threading.Event | None = None

    def install(self, apks):
        if self.gate is not None:
            self.gate.wait(5)
        if self.fail:
            raise DeviceError(self.fail)
        self.installed.append([Path(p).read_bytes() for p in apks])
        return "Success"

    def push(self, local, remote):
        self.pushed.append((Path(local).read_bytes(), remote))

    def runnable_abis(self):
        return self.runnable


@pytest.fixture
def device() -> FakeDevice:
    return FakeDevice()


def manager(tmp_path: Path, device: FakeDevice, opener=None) -> InstallManager:
    return InstallManager(tmp_path / "apk-tmp", lambda fn: fn(device), opener=opener)


def test_install_from_fdroid(tmp_path, device):
    apk = make_apk(tmp_path / "aurora.apk").read_bytes()
    opener, seen = make_opener(
        {
            AURORA_API: (fdroid_json(suggestedVersionCode=70), {}),
            AURORA_APK: (apk, {"Content-Length": str(len(apk))}),
        }
    )
    installer = manager(tmp_path, device, opener)
    job = installer.start_url("fdroid:com.aurora.store")
    assert job["state"] == "queued" and job["source"] == "fdroid:com.aurora.store"
    installer.wait(5)
    job = installer.current()
    assert job["state"] == "done", job
    assert job["downloaded"] == job["total"] == len(apk) and job["finished_at"]
    assert device.installed == [[apk]]
    assert [url for url, _ in seen] == [AURORA_API, AURORA_APK]
    assert list((tmp_path / "apk-tmp").iterdir()) == []  # temporary files are gone


def test_install_uploaded_bundle_and_clean_up(tmp_path, device):
    installer = manager(tmp_path, device)
    upload = installer.upload_path("../My Game.xapk")
    assert upload.parent == tmp_path / "apk-tmp" and upload.name.endswith("My_Game.xapk")
    make_zip(
        upload,
        {
            "base.apk": b"base",
            "split_config.xxhdpi.apk": b"dpi",
            "Android/obb/com.x/main.1.com.x.obb": b"obb",
        },
    )
    installer.start_file(upload, "My Game.xapk")
    installer.wait(5)
    assert installer.current()["state"] == "done"
    assert device.installed == [[b"base", b"dpi"]]
    assert device.pushed == [(b"obb", "/sdcard/Android/obb/com.x/main.1.com.x.obb")]
    assert not upload.exists()


def test_install_failures_are_reported(tmp_path, device):
    device.fail = "Install failed: INSTALL_FAILED_NO_MATCHING_ABIS"
    installer = manager(tmp_path, device)
    upload = make_apk(installer.upload_path("game.apk"))
    installer.start_file(upload, "game.apk")
    installer.wait(5)
    job = installer.current()
    assert job["state"] == "error" and "NO_MATCHING_ABIS" in job["message"]
    assert not upload.exists()

    opener, _ = make_opener(
        {
            "https://x.example/gone.apk": urllib.error.HTTPError(
                "https://x.example/gone.apk", 404, "Not Found", {}, None
            ),
            "https://offline.example/a.apk": urllib.error.URLError("Name or service not known"),
        }
    )
    installer = manager(tmp_path, device, opener)
    installer.start_url("https://x.example/gone.apk")
    installer.wait(5)
    assert installer.current()["message"] == "Download failed: HTTP 404 Not Found"
    installer.start_url("https://offline.example/a.apk")
    installer.wait(5)
    assert installer.current()["message"] == "Download failed: Name or service not known"
    with pytest.raises(InstallError):
        installer.start_url("javascript:alert(1)")


def test_one_install_at_a_time(tmp_path, device):
    device.gate = threading.Event()
    installer = manager(tmp_path, device)
    first = make_apk(installer.upload_path("a.apk"))
    installer.start_file(first, "a.apk")
    second = make_apk(installer.upload_path("b.apk"))
    with pytest.raises(RuntimeError, match="still running"):
        installer.start_file(second, "b.apk")
    assert installer.busy and not second.exists()
    device.gate.set()
    installer.wait(5)
    assert not installer.busy and installer.current()["state"] == "done"


def test_leftovers_of_a_previous_run_are_removed(tmp_path, device):
    stale = tmp_path / "apk-tmp" / "upload-123-game.apk"
    stale.parent.mkdir()
    stale.write_bytes(b"half an upload")
    manager(tmp_path, device)
    assert not stale.exists()


# -------------------------------------------------------------------- ABIs
def test_native_abis_and_check(tmp_path):
    base = make_zip(tmp_path / "base.apk", {"AndroidManifest.xml": b"m", "classes.dex": b"d"})
    arm32 = make_zip(tmp_path / "config.armeabi_v7a.apk", {"lib/armeabi-v7a/libgame.so": b"so"})
    arm64 = make_zip(tmp_path / "config.arm64_v8a.apk", {"lib/arm64-v8a/libgame.so": b"so"})
    assert native_abis([base]) == set()
    assert native_abis([base, arm32, arm64]) == {"armeabi-v7a", "arm64-v8a"}
    redroid = ["x86_64", "x86", "arm64-v8a"]
    check_abis([base], redroid)  # no native code: fine
    check_abis([base, arm64], redroid)
    check_abis([base, arm32], None)  # unknown device: don't block
    with pytest.raises(InstallError, match="only contains armeabi-v7a code"):
        check_abis([base, arm32], redroid)


def test_32_bit_only_game_is_refused_on_redroid(tmp_path, device):
    device.runnable = ["x86_64", "x86", "arm64-v8a"]
    installer = manager(tmp_path, device)
    upload = installer.upload_path("dokkan.xapk")
    make_zip(
        upload,
        {
            "com.bandainamcogames.dbzdokkanww.apk": make_apk(tmp_path / "b.apk").read_bytes(),
            "config.armeabi_v7a.apk": make_zip(
                tmp_path / "s.apk", {"lib/armeabi-v7a/libcocos2dcpp.so": b"so"}
            ).read_bytes(),
        },
    )
    installer.start_file(upload, "dokkan.xapk")
    installer.wait(5)
    job = installer.current()
    assert job["state"] == "error" and "arm64-v8a version" in job["message"]
    assert device.installed == []
