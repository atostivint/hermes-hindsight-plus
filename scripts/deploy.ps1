# PowerShell 7 is the target (pwsh), but the script stays 5.1-compatible so that the
# documented acceptance command (`powershell -NoProfile -File scripts/deploy.ps1 ...`) works.
# 5.1 rules this file obeys: never redirect a native command's stderr (merging or nulling
# stderr throws under $ErrorActionPreference='Stop'), and never emit non-ASCII into the ssh
# pipe (the remote script below stays pure ASCII). Text piped to a native command is encoded
# with $OutputEncoding, which may carry a UTF-8 preamble: see Deploy-Oracle.
<#
.SYNOPSIS
    Deploy a new version of the hermes-hindsight-plus plugin to the real hosts.

.DESCRIPTION
    Two halves ship together:
      * the agent half (Python package + plugin.yaml), installed by `hermes plugins install`
        into <HERMES_HOME>/plugins/hermes-hindsight-plus/;
      * the desktop half (desktop/plugin.js), published by Hermes' reconciler into
        <HERMES_HOME>/desktop-plugins/hermes-hindsight-plus/.

    `hermes plugins install` refreshes the agent half and reloads the plugin HOOKS in the
    running gateway; it does NOT rebuild the dashboard/HTTP route table, so a route the new
    version adds keeps answering 404 until the API host is restarted (measured 2026-11-11 on
    the v0.6.0 deploy: the install reported "active in the running gateway now: hooks", the
    list route kept working, and the new route 404'd until the API host was restarted).
    Measured the same day: the Desktop app on Windows runs as a remote CLIENT of the oracle
    backend ("[boot] Connecting to remote Hermes backend at https://oracle.blue-avior.ts.net"),
    so its /api/plugins/... calls land there while the desktop half loads locally from
    desktop-plugins/. A new route therefore needs the backend gateway restarted; on the Linux
    host `hermes gateway restart` refuses without root, so the restart is
    `sudo systemctl restart hermes-gateway.service`. -RestartApi then checks, without any HTTP
    call, that the gateway process started after dashboard/plugin_api.py was last written: an
    unauthenticated 401 proves nothing behind the tailnet auth gate (every path answers 401,
    a bogus one included).
    It also does NOT refresh an already published desktop half whose bytes differ from the
    package's:
    such a copy carries no .hermes-package.json marker, so the reconciler treats it as the
    user's own plugin and leaves it alone. This script compares the two by SHA256 (never by
    byte size: the checkout normalises CRLF to LF, so sizes differ for identical content),
    backs the published folder up outside the served tree
    (<HERMES_HOME>/backups/desktop-plugin-halves/) and copies the package half over it.
    Hermes then stamps the marker itself on the next reconcile, after which future installs
    refresh it.

    See docs/DEPLOY.md.

.PARAMETER Target
    oracle (the Linux host), windows (this machine), or all. Default: all.

.PARAMETER OracleHost
    ssh alias of the Linux host. Default: oracle.

.PARAMETER DryRun
    Print the planned commands instead of running them. No network, no write.

.PARAMETER RestartApi
    After each host's deploy, restart the API host and prove the route table is current.
    oracle: `sudo systemctl restart hermes-gateway.service` (`hermes gateway restart` refuses
    without root), wait for `systemctl is-active` to report active, then compare the gateway
    process start time (`stat -c %Y /proc/<MainPID>`) with the mtime of
    dashboard/plugin_api.py and fail when the process started before the file was written
    ("route table is older than the plugin: restart did not take").
    windows: nothing to restart -- the Desktop app is a remote client of the oracle backend,
    so /api/plugins/... is served there, not by a gateway on this machine; the script says so.
    It never kills a process by name, never makes an HTTP request, and never touches the
    Desktop app, so relaunching that app stays a human step. With -DryRun it only prints what
    it would run. Default: off.

.EXAMPLE
    pwsh -File scripts/deploy.ps1 -Target all -DryRun

.EXAMPLE
    pwsh -File scripts/deploy.ps1 -Target windows -RestartApi

.EXAMPLE
    pwsh -File scripts/deploy.ps1 -Target oracle -OracleHost oracle
#>
[CmdletBinding()]
param(
    [ValidateSet('oracle', 'windows', 'all')]
    [string]$Target = 'all',

    [string]$OracleHost = 'oracle',

    [switch]$DryRun,

    [switch]$RestartApi
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$PluginId = 'hermes-hindsight-plus'
$RepoUrl = 'https://github.com/atostivint/hermes-hindsight-plus.git'
$RepoRoot = Split-Path -Parent $PSScriptRoot
$Manifest = Join-Path $RepoRoot 'plugin.yaml'

# The restart is judged by one deterministic, auth-free invariant: the gateway process must
# have started after dashboard/plugin_api.py was last written. Measured 2026-11-11, no HTTP
# code can stand in for it: the tailnet front answers 401 for every path (a bogus
# /api/plugins/hermes-hindsight-plus/zzz-nope included), and nothing listens on 127.0.0.1:9119
# on the oracle host (the gateway speaks over the Unix socket
# /var/lib/hermes-agent/.hermes/gateway.sock; 127.0.0.1:8787 is the separate webui, which also
# 401s everything).

# ---------------------------------------------------------------- version banner

$versionLine = Select-String -Path $Manifest -Pattern '^version:\s*(\S+)\s*$' | Select-Object -First 1
if (-not $versionLine) {
    Write-Host "error: no 'version:' line in $Manifest" -ForegroundColor Red
    exit 1
}
$Version = $versionLine.Matches[0].Groups[1].Value
Write-Host "$PluginId version: $Version (from plugin.yaml)" -ForegroundColor Green
if ($DryRun) { Write-Host 'DRY RUN: nothing will be installed, copied or connected.' -ForegroundColor Yellow }

# ------------------------------------------------- uncommitted changes (warn only)

# `hermes plugins install` clones the git URL, so it deploys what is committed on the
# remote branch, never the dirty working copy. Say so, do not fail.
try {
    $dirty = @(git -C $RepoRoot status --porcelain)
} catch {
    $dirty = @()
}
if ($dirty.Count -gt 0) {
    Write-Warning 'working tree has uncommitted changes; only committed content is deployed:'
    $dirty | ForEach-Object { Write-Warning "  $_" }
}

# ----------------------------------------------------------------------- helpers

function Get-Sha256 {
    param([Parameter(Mandatory)][string]$Path)
    (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLowerInvariant()
}

function Invoke-Install {
    Write-Host "  hermes plugins install $RepoUrl --force --yes-deps --enable"
    if ($DryRun) { return }
    & hermes plugins install $RepoUrl --force --yes-deps --enable
    if ($LASTEXITCODE -ne 0) {
        Write-Host "  error: hermes plugins install exited $LASTEXITCODE" -ForegroundColor Red
        exit 1
    }
}

# ---------------------------------------------------------- api host restart guidance
#
# Measured 2026-11-11 (v0.6.0 deploy). `hermes plugins install` reloads the plugin HOOKS in
# the running gateway only ("Gateway reloaded plugins ... active in the running gateway now:
# hooks"). The dashboard/HTTP route table is built when the process serving the plugin API
# starts, so a route the new version adds answers 404 {"detail":"Not Found"} while routes that
# already existed keep working -- the shape of the bug: the list route fine, the new route
# 404 on first click. On a headless host it is the gateway service (or `hermes serve` /
# `hermes dashboard`) that must restart. The Desktop app can be a remote CLIENT of that host:
# its /api/plugins/... calls then land there, while the desktop half of the plugin loads from
# desktop-plugins/ on the machine running the app -- hence both hosts, and the backend restart.
#
# This script never kills a process by name and never touches the Desktop app: it prints that
# step; with -RestartApi it restarts the backend service on the Linux host, then proves the
# table was rebuilt by comparing the gateway process start time with plugin_api.py's mtime.
# No HTTP probe: measured 2026-11-11, the tailnet front answers 401 for every path, a bogus
# one included, so a 401 says nothing about whether a route exists.

function Write-RestartGuidance {
    param([Parameter(Mandatory)][string]$HostLabel)

    Write-Host ''
    Write-Host "  !! NEW HTTP ROUTES NEED THE API HOST RESTARTED ($HostLabel) !!" -ForegroundColor Yellow
    Write-Host '  The install above reloaded the plugin HOOKS in the running gateway. It did NOT'
    Write-Host '  rebuild the dashboard/HTTP route table, so a route this version adds answers'
    Write-Host '    HTTP 404 {"detail":"Not Found"}'
    Write-Host '  until the process that serves the plugin API is restarted. Routes that already'
    Write-Host '  existed keep working -- that is exactly why the old list route looks fine and'
    Write-Host '  the new route 404s.'
    Write-Host '  * headless host (systemd unit hermes-gateway.service): `hermes gateway restart`'
    Write-Host '    refuses without root -- use `sudo systemctl restart hermes-gateway.service`,'
    Write-Host '    then `systemctl is-active` until it says active.'
    Write-Host '  * no systemd (`hermes serve` / `hermes dashboard`): `hermes serve --stop`,'
    Write-Host '    then start it again (or let the app start it).'
    Write-Host '  * Desktop app: it can be a remote client of that backend, so its /api/plugins/...'
    Write-Host '    calls go to the backend and the backend is what must restart. Quit the app'
    Write-Host '    completely (tray icon too) and relaunch it only when it serves the API itself.'
    Write-Host '    This script never does that.'
    Write-Host '  Proof: after the restart, the gateway process must have started after'
    Write-Host '  dashboard/plugin_api.py was last written. -RestartApi checks that with stat'
    Write-Host '  and prints both epochs; an HTTP 401 proves nothing here (every path answers'
    Write-Host '  401 behind the tailnet auth gate, a bogus path included).'
}

function Write-StatusLines {
    param([string]$Text)
    foreach ($line in ($Text -split "`r?`n")) {
        $trimmed = $line.Trim()
        if ($trimmed) { Write-Host "    $trimmed" }
    }
}

function Invoke-RestartApiWindows {
    Write-Host '  [restart] API host (windows): -RestartApi' -ForegroundColor Cyan

    # Measured 2026-11-11: this Desktop app is a remote client of the oracle backend (boot
    # line "[boot] Connecting to remote Hermes backend at https://oracle.blue-avior.ts.net"),
    # so /api/plugins/... is served by that gateway, not by anything running here. There is
    # nothing to restart and nothing to prove locally: the oracle half of -RestartApi counts.
    if ($DryRun) {
        Write-Host '    would run: nothing -- this host is a remote client, not the API host'
    }
    Write-Host '    nothing to restart here: the Desktop app is a remote client of the oracle'
    Write-Host '    backend (boot line: [boot] Connecting to remote Hermes backend at'
    Write-Host '    https://oracle.blue-avior.ts.net), so /api/plugins/... is served there.'
    Write-Host '    No local hermes serve to stop, no route probe, no file touched.'
    Write-Host '    (a gateway that did serve the API here would still need the restart above)'
}


# --------------------------------------------------------------- windows target

function Deploy-Windows {
    Write-Host ''
    Write-Host '== windows (this machine) ==' -ForegroundColor Cyan

    $hermesHome = if ($env:HERMES_HOME) { $env:HERMES_HOME } else { Join-Path $env:LOCALAPPDATA 'hermes' }
    $pkgDir = Join-Path $hermesHome "plugins\$PluginId"
    $dpDir = Join-Path $hermesHome "desktop-plugins\$PluginId"
    $pkgJs = Join-Path $pkgDir 'desktop\plugin.js'
    $dstJs = Join-Path $dpDir 'plugin.js'
    # Outside <HERMES_HOME>/desktop-plugins/: that directory is a served root, and a backup
    # left in it is offered to the loader as a second copy of the same plugin id.
    $bakRoot = Join-Path $hermesHome 'backups\desktop-plugin-halves'

    Write-Host "  HERMES_HOME: $hermesHome"
    Write-Host '  [1/3] install the agent half (reloads the gateway plugin hooks, not the HTTP routes)'
    Invoke-Install

    Write-Host '  [2/3] desktop half'
    if ($DryRun) {
        Write-Host "    would compare sha256('$pkgJs') with sha256('$dstJs')"
        Write-Host "    and on mismatch: back up '$dpDir' to '$bakRoot\$PluginId-<timestamp>', then copy the package half over '$dstJs'"
    } else {
        if (-not (Test-Path -LiteralPath $pkgJs)) {
            Write-Host "  error: the installed package has no desktop/plugin.js at $pkgJs" -ForegroundColor Red
            exit 1
        }
        $srcHash = Get-Sha256 -Path $pkgJs
        $dstHash = if (Test-Path -LiteralPath $dstJs) { Get-Sha256 -Path $dstJs } else { '' }
        if ($dstHash -eq $srcHash) {
            Write-Host '    published copy already identical to the package (sha256)'
        } else {
            if (Test-Path -LiteralPath $dpDir) {
                $bak = Join-Path $bakRoot ("$PluginId-" + (Get-Date -Format 'yyyyMMdd-HHmmss'))
                if (Test-Path -LiteralPath $bak) {
                    Write-Host "  error: backup path already exists: $bak" -ForegroundColor Red
                    exit 1
                }
                New-Item -ItemType Directory -Path $bakRoot -Force | Out-Null
                Copy-Item -LiteralPath $dpDir -Destination $bak -Recurse -Force
                Write-Host "    backed up $dpDir -> $bak"
            }
            New-Item -ItemType Directory -Path $dpDir -Force | Out-Null
            Copy-Item -LiteralPath $pkgJs -Destination $dstJs -Force
            Write-Host "    copied package desktop half over $dstJs"
            $dstHash = Get-Sha256 -Path $dstJs
            if ($dstHash -ne $srcHash) {
                Write-Host '  error: desktop half still differs from the package after the copy' -ForegroundColor Red
                exit 1
            }
            Write-Host '    Hermes adopts and stamps .hermes-package.json on the next reconcile'
        }
    }

    Write-Host '  [3/3] version and hashes'
    if ($DryRun) {
        Write-Host "    would assert: 'hermes plugins show $PluginId' reports v$Version"
        Write-Host '    would print: sha256(package) and sha256(published)'
    } else {
        $show = & hermes plugins show $PluginId | Out-String
        if ($LASTEXITCODE -ne 0) {
            Write-Host "  error: 'hermes plugins show $PluginId' exited $LASTEXITCODE" -ForegroundColor Red
            exit 1
        }
        $expected = "$PluginId v$Version"
        if (-not $show.Contains($expected)) {
            Write-Host "  error: 'hermes plugins show $PluginId' does not report $expected" -ForegroundColor Red
            Write-Host ($show.Trim())
            exit 1
        }
        Write-Host "    hermes plugins show: $expected"

        $pkgHash = Get-Sha256 -Path $pkgJs
        $pubHash = Get-Sha256 -Path $dstJs
        Write-Host "    package   $pkgHash  $pkgJs"
        Write-Host "    published $pubHash  $dstJs"
        if ($pkgHash -ne $pubHash) {
            Write-Host '  error: desktop half differs from the package half (see docs/DEPLOY.md)' -ForegroundColor Red
            exit 1
        }
        Write-Host '  ok: windows deployed (installed and checked, not "active": see the routes step)' -ForegroundColor Green
    }

    # Installed is not the same as serving: the route table is only rebuilt when the API host
    # starts. Print that, always, even in a dry run, and never restart the Desktop app here.
    Write-RestartGuidance -HostLabel 'windows, this machine'
    if ($RestartApi) { Invoke-RestartApiWindows }
}

# ---------------------------------------------------------------- oracle target

# One ssh invocation: this whole script goes to `bash -s` on stdin, so no quoting hell.
# It must stay ASCII-only so that every possible $OutputEncoding encodes it faithfully.
#
# Line 1 is `:` and not `set`, on purpose. A UTF-8 BOM on stdin welds onto the first token,
# so a BOM would turn `set -euo pipefail` into `bomset: command not found`, that command would
# not run, and the whole remote half would proceed with no -e, -u or pipefail (measured
# 2026-11-11). With `:` first, the worst case is a failed no-op whose diagnostic the remote
# shell (this is bash, not PowerShell, so the no-piped-stderr rule does not apply) discards,
# and `set -euo pipefail` still runs. Measured under bash 5.3: BOM + `set -euo pipefail`
# prints "command not found", does not set the guard, and lets a later `false` through;
# BOM + this line prints nothing and stops at the `false`.
$OracleScript = @'
: 2>/dev/null; set -euo pipefail
export PATH="$HOME/.local/bin:/usr/local/bin:$PATH"

HERMES_BIN="$(command -v hermes || true)"
if [ -z "$HERMES_BIN" ]; then echo "error: hermes not found on PATH" >&2; exit 1; fi

# HERMES_HOME is not exported in a non-interactive ssh shell; the value is $HOME/.hermes.
H="${HERMES_HOME:-$HOME/.hermes}"
PKG="$H/plugins/hermes-hindsight-plus"
DP="$H/desktop-plugins/hermes-hindsight-plus"
SRC="$PKG/desktop/plugin.js"
DST="$DP/plugin.js"
# Outside the served desktop-plugins root: a backup left inside it would be loaded as a
# second copy of the same plugin id.
BAKROOT="$H/backups/desktop-plugin-halves"

sha() { sha256sum "$1" | cut -d' ' -f1; }

echo "  hermes home: $H"
echo "  hermes: $HERMES_BIN"

echo "  [1/3] install the agent half (reloads the gateway plugin hooks, not the HTTP routes)"
"$HERMES_BIN" plugins install https://github.com/atostivint/hermes-hindsight-plus.git --force --yes-deps --enable

echo "  [2/3] desktop half"
if [ ! -f "$SRC" ]; then
  echo "error: the installed package has no desktop/plugin.js at $SRC" >&2
  exit 1
fi
if [ -f "$DST" ] && [ "$(sha "$SRC")" = "$(sha "$DST")" ]; then
  echo "    published copy already identical to the package (sha256)"
else
  if [ -d "$DP" ]; then
    BAK="$BAKROOT/hermes-hindsight-plus-$(date +%Y%m%d-%H%M%S)"
    if [ -e "$BAK" ]; then echo "error: backup path already exists: $BAK" >&2; exit 1; fi
    mkdir -p "$BAKROOT"
    cp -a "$DP" "$BAK"
    echo "    backed up $DP -> $BAK"
  fi
  mkdir -p "$DP"
  cp "$SRC" "$DST"
  echo "    copied package desktop half over $DST"
  if [ "$(sha "$SRC")" != "$(sha "$DST")" ]; then
    echo "error: desktop half still differs from the package after the copy" >&2
    exit 1
  fi
  echo "    Hermes adopts and stamps .hermes-package.json on the next reconcile"
fi

echo "  [3/3] version and hashes"
V="$(awk '/^version:/{print $2; exit}' "$PKG/plugin.yaml")"
if [ -z "$V" ]; then echo "error: no version: line in $PKG/plugin.yaml" >&2; exit 1; fi
echo "    version: $V"
SHOW="$("$HERMES_BIN" plugins show hermes-hindsight-plus)"
case "$SHOW" in
  *"hermes-hindsight-plus v$V"*)
    echo "    hermes plugins show: hermes-hindsight-plus v$V"
    ;;
  *)
    echo "error: 'hermes plugins show hermes-hindsight-plus' does not report hermes-hindsight-plus v$V" >&2
    printf '%s\n' "$SHOW" >&2
    exit 1
    ;;
esac
echo "    package   $(sha "$SRC")  $SRC"
echo "    published $(sha "$DST")  $DST"
echo "  ok: oracle deployed (installed and checked; new routes need the API host restarted)"
'@
# bash on stdin must never see a CR: normalise whichever line endings this file has.
$OracleScript = ($OracleScript -replace "`r`n", "`n").TrimEnd()

# ---------------------------- oracle: restart the gateway, then prove the route table is new
#
# Measured 2026-11-11: `hermes gateway restart` refuses on this host ("requires root"), while
# `sudo systemctl restart hermes-gateway.service` works (passwordless sudo). The restart goes
# through the service, the unit must come back active, and then one invariant decides whether
# the route table was rebuilt: the gateway process must have started after
# dashboard/plugin_api.py was last written, because that file is what builds the table. No HTTP
# probe -- measured the same day, the tailnet front answers 401 for EVERY path (a bogus
# /api/plugins/hermes-hindsight-plus/zzz-nope included), nothing listens on 127.0.0.1:9119 on
# the host (the gateway speaks over a Unix socket the ssh user cannot read, and 127.0.0.1:8787
# is the separate webui, which also 401s everything), so a 401 proves nothing about a route.
# ASCII only, same BOM guard as $OracleScript.
$OracleRestartCheckScript = @'
: 2>/dev/null; set -u
export PATH="$HOME/.local/bin:/usr/local/bin:$PATH"
H="${HERMES_HOME:-$HOME/.hermes}"
SVC="hermes-gateway.service"
API_FILE="$H/plugins/hermes-hindsight-plus/dashboard/plugin_api.py"

echo "  sudo systemctl restart $SVC   ('hermes gateway restart' refuses without root)"
if ! sudo systemctl restart "$SVC"; then
  echo "error: 'sudo systemctl restart $SVC' failed" >&2
  exit 1
fi

# A restart that comes back dead is not a success: wait for the unit to report active.
STATE=""
N=0
while [ "$N" -lt 30 ]; do
  STATE="$(systemctl is-active "$SVC" 2>/dev/null || true)"
  if [ "$STATE" = "active" ]; then break; fi
  N=$((N + 1))
  sleep 1
done
echo "  $SVC: ${STATE:-unknown}"
if [ "$STATE" != "active" ]; then
  echo "error: $SVC is '${STATE:-unknown}' after 30s, not 'active'" >&2
  exit 1
fi

# Deterministic and auth-free: the route table is built from plugin_api.py when the gateway
# process starts, so that process must have started at or after the file's last write.
# /proc/<PID> carries the process start time as its directory mtime; stat -c %Y is epoch secs.
if [ ! -f "$API_FILE" ]; then
  echo "error: plugin API file not found: $API_FILE" >&2
  exit 1
fi

PID="$(systemctl show -p MainPID --value "$SVC" 2>/dev/null || true)"
case "$PID" in
  ''|0)
    echo "error: $SVC reports no MainPID ('${PID:-empty}')" >&2
    exit 1
    ;;
esac
if [ ! -d "/proc/$PID" ]; then
  echo "error: /proc/$PID does not exist for $SVC" >&2
  exit 1
fi

START="$(stat -c %Y "/proc/$PID" 2>/dev/null || true)"
FILE="$(stat -c %Y "$API_FILE" 2>/dev/null || true)"
case "$START" in
  ''|*[!0-9]*) echo "error: could not read the start time of /proc/$PID" >&2; exit 1 ;;
esac
case "$FILE" in
  ''|*[!0-9]*) echo "error: could not read the mtime of $API_FILE" >&2; exit 1 ;;
esac

echo "  gateway MainPID $PID"
echo "  gateway started  $START (epoch seconds)"
echo "  plugin_api.py    $FILE (epoch seconds, last write)"

if [ "$START" -lt "$FILE" ]; then
  echo "error: route table is older than the plugin: restart did not take" >&2
  echo "       gateway start $START < plugin_api.py mtime $FILE" >&2
  exit 1
fi

DELTA=$((START - FILE))
HOURS=$((DELTA / 3600))
MINS=$((DELTA % 3600 / 60))
SECS=$((DELTA % 60))
echo "  gateway started ${DELTA}s (${HOURS}h ${MINS}m ${SECS}s) after plugin_api.py was written"
echo "  ok: the running gateway is newer than the installed route table"
exit 0
'@
# bash on stdin must never see a CR. Note the trailing newline: PowerShell appends
# [Environment]::NewLine to each object piped to a native command, so a script ending
# without one ends its LAST line as "exit 0<CR>" -- bash then reads the argument as
# "0<CR>" and prints "exit: 0: numeric argument required" (measured 2026-11-11). With the
# explicit newline, `exit 0` is a clean line and bash leaves before the appended CR.
$OracleRestartCheckScript = ($OracleRestartCheckScript -replace "`r`n", "`n").TrimEnd() + "`n"

# Run one of the ASCII scripts above on the remote host and return (exit, output).
function Invoke-OracleScript {
    param([Parameter(Mandatory)][string]$Script)

    # Same $OutputEncoding precaution as Deploy-Oracle: no UTF-8 preamble may reach bash.
    $prevOutputEncoding = $OutputEncoding
    $OutputEncoding = [System.Text.UTF8Encoding]::new($false)
    try {
        $text = ($Script | & ssh $OracleHost 'bash -s' | Out-String)
        $remoteExit = $LASTEXITCODE
    } finally {
        $OutputEncoding = $prevOutputEncoding
    }
    [pscustomobject]@{ ExitCode = $remoteExit; Output = $text }
}

function Invoke-RestartApiOracle {
    Write-Host "  [restart] API host ($OracleHost): -RestartApi" -ForegroundColor Cyan

    if ($DryRun) {
        Write-Host "    would run over ssh: $OracleHost 'bash -s' (script below)"
        Write-Host '    it restarts the systemd unit, waits for it to be active, then compares the'
        Write-Host '    gateway process start time with dashboard/plugin_api.py s mtime:'
        foreach ($line in ($OracleRestartCheckScript -split "`n")) { Write-Host "      $line" }
        Write-Host '    gateway started after the file was written = the route table is current;'
        Write-Host '    started before it = "route table is older than the plugin: restart did not take"'
        return
    }

    $check = $null
    try {
        $check = Invoke-OracleScript -Script $OracleRestartCheckScript
    } catch {
        Write-Host "    could not reach ${OracleHost}: $($_.Exception.Message)" -ForegroundColor Yellow
        Write-Host '    restart the API host by hand, as in the guidance above.' -ForegroundColor Yellow
        return
    }
    Write-StatusLines -Text $check.Output
    if ($check.ExitCode -eq 0) {
        Write-Host "  ok: $OracleHost restarted; its gateway is newer than the installed route table" -ForegroundColor Green
        return
    }
    Write-Host "  error: restart check failed on ${OracleHost} (ssh exit $($check.ExitCode))" -ForegroundColor Red
    Write-Host '    "route table is older than the plugin" above means the restart did not take.' -ForegroundColor Yellow
    exit 1
}

function Deploy-Oracle {
    Write-Host ''
    Write-Host "== oracle ($OracleHost) ==" -ForegroundColor Cyan
    if ($DryRun) {
        Write-Host "  ssh $OracleHost <<'SH'   (one invocation, script on stdin)"
        foreach ($line in ($OracleScript -split "`n")) { Write-Host "    $line" }
        Write-Host '  SH'
        Write-RestartGuidance -HostLabel "oracle, $OracleHost"
        if ($RestartApi) { Invoke-RestartApiOracle }
        return
    }
    Write-Host "  ssh $OracleHost   (one invocation, script on stdin)"
    # Text piped to a native command is encoded with $OutputEncoding; UTF-8 encodings built
    # with the BOM flag prepend a preamble, and bash then reads line 1 as one word with the
    # BOM glued to it. Pipe UTF-8 with no preamble, and restore the previous value afterwards
    # (this assignment is function-scoped, so the caller's value is never touched).
    $prevOutputEncoding = $OutputEncoding
    $OutputEncoding = [System.Text.UTF8Encoding]::new($false)
    try {
        $OracleScript | & ssh $OracleHost 'bash -s'
        $sshExit = $LASTEXITCODE
    } finally {
        $OutputEncoding = $prevOutputEncoding
    }
    if ($sshExit -ne 0) {
        Write-Host "  error: remote deploy failed (ssh exit $sshExit)" -ForegroundColor Red
        exit 1
    }
    Write-Host '  ok: oracle deployed (installed and checked, not "active": see the routes step)' -ForegroundColor Green
    Write-RestartGuidance -HostLabel "oracle, $OracleHost"
    if ($RestartApi) { Invoke-RestartApiOracle }
}

# ------------------------------------------------------------------------ run

if ($Target -in @('windows', 'all')) { Deploy-Windows }
if ($Target -in @('oracle', 'all')) { Deploy-Oracle }

Write-Host ''
Write-Host "deploy complete: $Target, version $Version (installed and checked)" -ForegroundColor Green
Write-Host 'Routes: restart the backend gateway before a new route answers'
Write-Host '  (systemd: sudo systemctl restart hermes-gateway.service; else hermes serve --stop + start)'
Write-Host '  -RestartApi does it and proves the gateway process is newer than plugin_api.py.'
Write-Host 'Not verified here: that the Desktop pane actually renders. Open it and look.'
