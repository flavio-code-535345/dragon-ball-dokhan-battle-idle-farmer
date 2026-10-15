"""docker/host-setup.sh with fake /proc files and a fake modprobe."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "docker" / "host-setup.sh"

pytestmark = pytest.mark.skipif(
    sys.platform == "win32" or shutil.which("sh") is None, reason="needs a POSIX shell"
)

FAKE_MODPROBE = """#!/bin/sh
echo "$@" >> "$FAKE_ROOT/modprobe.args"
if [ "${FAKE_MODPROBE_FAIL:-}" = "1" ]; then
  echo "FATAL: Module binder_linux not found in directory /lib/modules/x" >&2
  exit 1
fi
printf 'nodev\\tbinder\\n' >> "$FAKE_ROOT/proc/filesystems"
echo "binder_linux 249856 0 - Live 0x0000000000000000" >> "$FAKE_ROOT/proc/modules"
"""


@pytest.fixture
def host(tmp_path: Path):
    for directory in ("proc", "dev", "modules", "etc/modules-load.d", "etc/modprobe.d", "bin"):
        (tmp_path / directory).mkdir(parents=True)
    (tmp_path / "proc" / "filesystems").write_text("nodev\tsysfs\nnodev\tproc\n\text4\n")
    (tmp_path / "proc" / "modules").write_text("overlay 212992 1 - Live 0x0\n")
    modprobe = tmp_path / "bin" / "modprobe"
    modprobe.write_text(FAKE_MODPROBE)
    modprobe.chmod(0o755)
    kernel = subprocess.run(["uname", "-r"], capture_output=True, text=True).stdout.strip()
    (tmp_path / "modules" / kernel).mkdir()

    def run(**env: str) -> subprocess.CompletedProcess[str]:
        environment = {
            **os.environ,
            "PATH": f"{tmp_path / 'bin'}{os.pathsep}{os.environ['PATH']}",
            "FAKE_ROOT": str(tmp_path),
            "HOST_SETUP_PROC": str(tmp_path / "proc"),
            "HOST_SETUP_DEV": str(tmp_path / "dev"),
            "HOST_SETUP_MODULES": str(tmp_path / "modules"),
            "HOST_SETUP_ETC": str(tmp_path / "etc"),
            **env,
        }
        return subprocess.run(
            ["sh", str(SCRIPT)], capture_output=True, text=True, env=environment, timeout=30
        )

    return tmp_path, run


def test_loads_binder_and_persists_it(host):
    root, run = host
    result = run()
    assert result.returncode == 0, result.stdout + result.stderr
    assert "binder is ready" in result.stdout
    assert (root / "modprobe.args").read_text().split() == [
        "binder_linux",
        "devices=binder,hwbinder,vndbinder",
    ]
    load = (root / "etc" / "modules-load.d" / "redroid.conf").read_text()
    assert load.splitlines()[-1] == "binder_linux"
    options = (root / "etc" / "modprobe.d" / "redroid.conf").read_text()
    assert options.splitlines()[-1] == "options binder_linux devices=binder,hwbinder,vndbinder"

    # Second run (e.g. after a redeploy): already loaded, nothing to do but persisting.
    (root / "modprobe.args").unlink()
    result = run()
    assert result.returncode == 0 and "binder is available" in result.stdout
    assert not (root / "modprobe.args").exists()


def test_built_in_binder_needs_nothing(host):
    root, run = host
    with open(root / "proc" / "filesystems", "a") as fh:
        fh.write("nodev\tbinder\n")
    result = run()
    assert result.returncode == 0 and "binder is available" in result.stdout
    assert not (root / "modprobe.args").exists()
    assert not (root / "etc" / "modules-load.d" / "redroid.conf").exists()


def test_missing_driver_explains_what_to_do(host):
    root, run = host
    result = run(FAKE_MODPROBE_FAIL="1")
    assert result.returncode == 1
    assert "Module binder_linux not found" in result.stdout
    assert "linux-modules-extra" in result.stdout and "DEVICE_SETUP.md" in result.stdout
    assert not (root / "etc" / "modules-load.d" / "redroid.conf").exists()


def test_works_without_the_host_etc_mounts(host):
    root, run = host
    shutil.rmtree(root / "etc")
    result = run()
    assert result.returncode == 0 and "binder is ready" in result.stdout
    assert "boot" not in result.stdout  # nothing claimed about reboots
