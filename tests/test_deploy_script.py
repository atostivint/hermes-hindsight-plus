"""Guard the deploy script.

Three things are easy to break and invisible in the pytest suite otherwise:

* the script must stay dry-runnable under Windows PowerShell 5.1 as well as pwsh 7. A native
  command whose stderr is redirected (merged or nulled) throws under 5.1 with
  ``$ErrorActionPreference = 'Stop'``, so a successful deploy would exit 1;
* it must never need the network, ssh or a real install in ``-DryRun``;
* installing is not the same as serving. ``hermes plugins install`` reloads the plugin hooks
  only, so a route the new version adds answers 404 until the API host restarts (measured
  2026-11-11, v0.6.0: the list route worked, the new route 404'd). The dry run must always
  print that step, in both of its concrete cases, and ``-RestartApi`` must stay an intent in
  a dry run — nothing may be stopped or started. On the Linux host the restart is the systemd
  unit (``sudo systemctl restart hermes-gateway.service``, since ``hermes gateway restart``
  refuses without root) and it is only trusted through one auth-free invariant: the gateway
  process must have started after ``dashboard/plugin_api.py`` was last written
  (``stat -c %Y /proc/<MainPID>`` vs the file's mtime). There is no HTTP probe: measured
  2026-11-11, the tailnet front answers 401 for every path, a bogus one included, so a 401
  proves nothing about whether a route exists.


No network, no ssh: only ``-DryRun``, the oracle half included (it prints the script it would
pipe to ``ssh``).
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

# The acceptance command on this repo is powershell.exe (5.1); pwsh is the fallback.
INTERPRETER = POWERSHELL or PWSH


def _version():
    for line in MANIFEST.read_text(encoding="utf-8").splitlines():
        if line.startswith("version:"):
            return line.split(":", 1)[1].strip()
    raise AssertionError("no version: line in plugin.yaml")


def _dry_run(interpreter, target="windows", extra=()):
    completed = subprocess.run(
        [
            interpreter,
            "-NoProfile",
            "-File",
            str(SCRIPT),
            "-Target",
            target,
            "-DryRun",
            *extra,
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


@pytest.mark.skipif(
    INTERPRETER is None, reason="neither pwsh nor powershell.exe is installed"
)
def test_deploy_dry_run_all_prints_route_restart_guidance():
    """`-Target all` must carry the restart step for BOTH hosts, with both concrete cases."""
    code, out = _dry_run(INTERPRETER, target="all")
    assert code == 0, out

    # The step is named, and it is not optional-looking.
    assert "restart" in out.lower()
    assert out.count("NEW HTTP ROUTES NEED THE API HOST RESTARTED") == 2, out

    # Why: hooks reload, the route table does not; the symptom is a 404 on a route that exists.
    assert 'HTTP 404 {"detail":"Not Found"}' in out
    assert "route table" in out

    # The two concrete cases.
    assert "Desktop app" in out
    assert "remote client" in out
    assert "quit the app" in out.lower() and "relaunch" in out
    assert "headless host" in out
    assert "`hermes serve` / `hermes dashboard`" in out
    assert "hermes serve --stop" in out
    assert "sudo systemctl restart hermes-gateway.service" in out

    # The one line added to the final summary.
    assert "Routes: restart the backend gateway before a new route answers" in out


@pytest.mark.skipif(
    INTERPRETER is None, reason="neither pwsh nor powershell.exe is installed"
)
def test_deploy_dry_run_restart_api_prints_intent_only():
    """`-RestartApi` under `-DryRun` says what it would run, and runs nothing."""
    code, out = _dry_run(INTERPRETER, extra=("-RestartApi",))
    assert code == 0, out

    # On Windows there is nothing to restart: the Desktop app is a remote client of the
    # oracle backend, and that is what the script has to say instead of probing.
    assert "[restart] API host (windows): -RestartApi" in out
    assert "would run: nothing" in out
    assert "remote client of the oracle" in out
    assert "https://oracle.blue-avior.ts.net" in out
    assert "No local hermes serve to stop, no route probe, no file touched" in out

    # Nothing actually ran: no process listing, no HTTP client, no probe URL.
    assert "hermes serve --status" not in out
    assert "No hermes dashboard or serve processes running" not in out
    assert "curl" not in out
    assert "127.0.0.1" not in out
    assert "Bitwarden" not in out


@pytest.mark.skipif(
    INTERPRETER is None, reason="neither pwsh nor powershell.exe is installed"
)
def test_deploy_dry_run_all_restart_api_prints_restart_and_mtime_check():
    """`-Target all -DryRun -RestartApi` must SHOW the oracle restart and the mtime check.

    The oracle API host is a systemd unit: `hermes gateway restart` refuses without root, so
    the intent has to be `sudo systemctl restart hermes-gateway.service` followed by a
    deterministic, auth-free check: the gateway process started after plugin_api.py was last
    written. An HTTP probe cannot do that job -- every path answers 401 on this host.
    A dry run still touches nothing, ssh included.
    """
    code, out = _dry_run(INTERPRETER, target="all", extra=("-RestartApi",))
    assert code == 0, out

    # The service restart, not `hermes serve --stop` as the only story.
    assert "systemctl" in out
    assert "sudo systemctl restart hermes-gateway.service" in out
    assert "systemctl is-active" in out
    assert "[restart] API host (oracle): -RestartApi" in out

    # The invariant, in words and as the exact remote commands.
    assert 'systemctl show -p MainPID --value "$SVC"' in out
    assert "stat -c %Y" in out
    assert "/proc/$PID" in out
    assert "dashboard/plugin_api.py" in out
    assert "route table is older than the plugin: restart did not take" in out
    assert "epoch seconds" in out

    # No HTTP probe survives anywhere: 401 proves nothing behind the auth gate.
    assert "curl" not in out
    assert "Invoke-WebRequest" not in out
    assert "127.0.0.1:9119" not in out
    assert "200 or 401 = the route exists" not in out

    # Both hosts are covered in one run.
    assert "[restart] API host (windows): -RestartApi" in out
    assert "[restart] API host (oracle): -RestartApi" in out


def test_deploy_script_never_redirects_native_stderr():
    text = SCRIPT.read_text(encoding="utf-8")
    assert "2>&1" not in text
    assert "2>$null" not in text


def test_deploy_script_has_no_http_probe():
    """The restart is proven by process start time, never by an HTTP status code."""
    text = SCRIPT.read_text(encoding="utf-8")
    for banned in ("curl", "Invoke-WebRequest", "Invoke-RestMethod", "http://"):
        assert banned not in text, f"{banned} must not appear in the deploy script"


def test_deploy_script_never_kills_processes_by_name():
    """The restart path goes through `hermes serve`, never through the process table."""
    text = SCRIPT.read_text(encoding="utf-8")
    for banned in ("Stop-Process", "Restart-Computer", "taskkill", "killall"):
        assert banned not in text, f"{banned} must not appear in the deploy script"


def test_deploy_script_documents_restart_api_switch():
    text = SCRIPT.read_text(encoding="utf-8")
    assert ".PARAMETER RestartApi" in text
    assert "[switch]$RestartApi" in text
