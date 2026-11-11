"""Guard the deploy script.

Two things are easy to break and invisible in the pytest suite otherwise:

* the script must stay dry-runnable under Windows PowerShell 5.1 as well as pwsh 7. A native
  command whose stderr is redirected (merged or nulled) throws under 5.1 with
  ``$ErrorActionPreference = 'Stop'``, so a successful deploy would exit 1;
* it must never need the network, ssh or a real install in ``-DryRun``.

No network, no ssh: only ``-Target windows -DryRun``.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "deploy.ps1"
MANIFEST = ROOT / "plugin.yaml"

PWSH = shutil.which("pwsh")
POWERSHELL = shutil.which("powershell")


def _version():
    for line in MANIFEST.read_text(encoding="utf-8").splitlines():
        if line.startswith("version:"):
            return line.split(":", 1)[1].strip()
    raise AssertionError("no version: line in plugin.yaml")


def _dry_run(interpreter):
    completed = subprocess.run(
        [
            interpreter,
            "-NoProfile",
            "-File",
            str(SCRIPT),
            "-Target",
            "windows",
            "-DryRun",
        ],
        capture_output=True,
        text=True,
        errors="replace",
        timeout=120,
        check=False,
    )
    return completed.returncode, completed.stdout


@pytest.mark.skipif(PWSH is None, reason="pwsh (PowerShell 7) is not installed")
def test_deploy_dry_run_under_pwsh():
    code, out = _dry_run(PWSH)
    assert code == 0, out
    assert f"version: {_version()}" in out
    assert "plugins\\hermes-hindsight-plus\\desktop\\plugin.js" in out
    assert "desktop-plugins\\hermes-hindsight-plus\\plugin.js" in out


@pytest.mark.skipif(
    POWERSHELL is None, reason="powershell.exe (Windows PowerShell 5.1) is not installed"
)
def test_deploy_dry_run_under_windows_powershell():
    code, out = _dry_run(POWERSHELL)
    assert code == 0, out
    assert f"version: {_version()}" in out
    assert "plugins\\hermes-hindsight-plus\\desktop\\plugin.js" in out
    assert "desktop-plugins\\hermes-hindsight-plus\\plugin.js" in out


def test_deploy_script_never_redirects_native_stderr():
    text = SCRIPT.read_text(encoding="utf-8")
    assert "2>&1" not in text
    assert "2>$null" not in text
