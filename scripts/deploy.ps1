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
    list route kept working, and the new route 404'd until the Desktop app was relaunched).
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
    After each host's deploy, run `hermes serve --status`; when it reports a running
    dashboard/serve process, stop it with `hermes serve --stop` and start `hermes serve`
    again (detached), on this machine and over ssh for `oracle`. When nothing is running it
    says so and does nothing. It never touches the Desktop app, which serves the plugin API
    itself on Windows: relaunching that app stays a human step. With -DryRun it only prints
    what it would run. Default: off.

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
# 404 on first click. On this machine the Desktop app serves that API itself (`hermes serve
# --status` reports no dashboard/serve process), so relaunching the app is what loads the new
# routes; on a headless host it is `hermes serve` / `hermes dashboard` that must restart.
#
# This script never kills a process by name and never touches the Desktop app: it prints that
# step, and with -RestartApi it drives only the operator's own `hermes serve` CLI.

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
    Write-Host '  * Desktop app on Windows (it serves the plugin API itself): quit the app'
    Write-Host '    completely (tray icon too) and relaunch it. This script never does that.'
    Write-Host '  * headless host (`hermes serve` / `hermes dashboard`): `hermes serve --stop`,'
    Write-Host '    then start it again (or let the app start it).'
    Write-Host '  -RestartApi performs that headless restart for you when a serve process is running.'
}

function Test-ApiServeRunning {
    param([string]$StatusText)

    # `hermes serve --status` (hermes_cli/main_dashboard.py, _report_dashboard_status) prints
    # "No hermes dashboard or serve processes running." with exit 0 when there is nothing to
    # restart, and "<n> hermes dashboard/serve process(es) running:" plus "PID ..." lines
    # otherwise. Match the running header only: the negative line contains "or serve".
    if ([string]::IsNullOrWhiteSpace($StatusText)) { return $false }
    return ($StatusText -match '(?i)dashboard/serve process')
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

    if ($DryRun) {
        Write-Host '    would run: hermes serve --status'
        Write-Host '    if it reports a dashboard/serve process: hermes serve --stop'
        Write-Host '      then start detached: hermes serve'
        Write-Host '    if nothing runs: nothing to stop, nothing to start'
        Write-Host '    the Desktop app is never touched, even when it is the API host'
        return
    }

    $status = ''
    try {
        $status = (& hermes serve --status | Out-String)
        $statusExit = $LASTEXITCODE
    } catch {
        Write-Host "    could not run 'hermes serve --status': $($_.Exception.Message)" -ForegroundColor Yellow
        Write-Host '    restart the API host by hand, as in the guidance above.' -ForegroundColor Yellow
        return
    }
    Write-StatusLines -Text $status

    if (-not (Test-ApiServeRunning -StatusText $status)) {
        Write-Host '    no hermes serve/dashboard process is running: nothing to restart.' -ForegroundColor Yellow
        Write-Host '    (when the Desktop app serves the API, quit and relaunch it by hand.)'
        return
    }

    Write-Host "    'hermes serve --status' (exit $statusExit) lists a serve process: restarting it"
    Write-Host '  hermes serve --stop'
    try {
        & hermes serve --stop
        $stopExit = $LASTEXITCODE
    } catch {
        Write-Host "    error: could not run 'hermes serve --stop': $($_.Exception.Message)" -ForegroundColor Red
        Write-Host '    start the API host by hand; new routes stay 404 until then.' -ForegroundColor Yellow
        return
    }
    if ($stopExit -ne 0) {
        Write-Host "    error: 'hermes serve --stop' exited $stopExit" -ForegroundColor Red
        Write-Host '    start the API host by hand; new routes stay 404 until then.' -ForegroundColor Yellow
        return
    }

    $hermesExe = $null
    try {
        $hermesExe = (Get-Command hermes -ErrorAction Stop).Source
    } catch {
        Write-Host "    error: hermes is not on PATH: $($_.Exception.Message)" -ForegroundColor Red
        return
    }
    try {
        Start-Process -FilePath $hermesExe -ArgumentList 'serve' -WindowStyle Hidden -ErrorAction Stop
    } catch {
        Write-Host "    error: could not start 'hermes serve': $($_.Exception.Message)" -ForegroundColor Red
        Write-Host '    start the API host by hand; new routes stay 404 until then.' -ForegroundColor Yellow
        return
    }
    Write-Host '    started detached: hermes serve'
    Write-Host '    (default flags; if the previous process used --port/--isolated, start it the same way)'

    Start-Sleep -Seconds 3
    $after = ''
    try {
        $after = (& hermes serve --status | Out-String)
    } catch {
        Write-Host "    could not re-check 'hermes serve --status': $($_.Exception.Message)" -ForegroundColor Yellow
        return
    }
    Write-StatusLines -Text $after
    if (Test-ApiServeRunning -StatusText $after) {
        Write-Host '  ok: API host restarted' -ForegroundColor Green
    } else {
        Write-Host '    the restarted process is not listed yet; check `hermes serve --status` shortly' -ForegroundColor Yellow
    }
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

# ------------------------------------------------ oracle: is a serve process running?
#
# Same two pieces of evidence as the local half, asked over ssh. `hermes serve --status` exits
# with the count of live processes, so the exit code is normalised to 0 here and the decision
# is taken on the text alone. ASCII only, same BOM guard as $OracleScript.
$OracleStatusScript = @'
: 2>/dev/null; set -u
export PATH="$HOME/.local/bin:/usr/local/bin:$PATH"
HERMES_BIN="$(command -v hermes || true)"
if [ -z "$HERMES_BIN" ]; then echo "error: hermes not found on PATH" >&2; exit 1; fi
STATUS="$("$HERMES_BIN" serve --status)"
printf '%s\n' "$STATUS"
exit 0
'@
$OracleStatusScript = ($OracleStatusScript -replace "`r`n", "`n").TrimEnd()

# The restart itself: stop, then start detached. `hermes serve` is a foreground server, so it
# is started with nohup and its stdout/stderr detached; otherwise ssh would wait on it.
$OracleRestartScript = @'
: 2>/dev/null; set -u
export PATH="$HOME/.local/bin:/usr/local/bin:$PATH"
HERMES_BIN="$(command -v hermes || true)"
if [ -z "$HERMES_BIN" ]; then echo "error: hermes not found on PATH" >&2; exit 1; fi
echo "  hermes serve --stop"
if ! "$HERMES_BIN" serve --stop; then
  echo "error: hermes serve --stop failed; start the API host by hand" >&2
  exit 1
fi
echo "  start detached: hermes serve"
nohup "$HERMES_BIN" serve >/dev/null 2>/dev/null &
echo "  started pid $!"
'@
$OracleRestartScript = ($OracleRestartScript -replace "`r`n", "`n").TrimEnd()

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
        Write-Host '    script: hermes serve --status; stop + start only when that lists a process'
        foreach ($line in ($OracleRestartScript -split "`n")) { Write-Host "      $line" }
        Write-Host '    if nothing runs: nothing to stop, nothing to start'
        return
    }

    $probe = $null
    try {
        $probe = Invoke-OracleScript -Script $OracleStatusScript
    } catch {
        Write-Host "    could not reach ${OracleHost}: $($_.Exception.Message)" -ForegroundColor Yellow
        Write-Host '    restart the API host by hand, as in the guidance above.' -ForegroundColor Yellow
        return
    }
    Write-StatusLines -Text $probe.Output
    if ($probe.ExitCode -ne 0) {
        Write-Host "    could not read 'hermes serve --status' on ${OracleHost} (ssh exit $($probe.ExitCode))" -ForegroundColor Yellow
        Write-Host '    restart the API host by hand, as in the guidance above.' -ForegroundColor Yellow
        return
    }
    if (-not (Test-ApiServeRunning -StatusText $probe.Output)) {
        Write-Host "    no hermes serve/dashboard process is running on ${OracleHost}: nothing to restart." -ForegroundColor Yellow
        return
    }

    $restart = $null
    try {
        $restart = Invoke-OracleScript -Script $OracleRestartScript
    } catch {
        Write-Host "    error: could not restart ${OracleHost}: $($_.Exception.Message)" -ForegroundColor Red
        return
    }
    Write-StatusLines -Text $restart.Output
    if ($restart.ExitCode -ne 0) {
        Write-Host "    error: restart failed on ${OracleHost} (ssh exit $($restart.ExitCode))" -ForegroundColor Red
        return
    }
    Start-Sleep -Seconds 3
    $after = $null
    try {
        $after = Invoke-OracleScript -Script $OracleStatusScript
    } catch {
        Write-Host "    could not re-check ${OracleHost}: $($_.Exception.Message)" -ForegroundColor Yellow
        return
    }
    Write-StatusLines -Text $after.Output
    if (Test-ApiServeRunning -StatusText $after.Output) {
        Write-Host "  ok: API host restarted on $OracleHost" -ForegroundColor Green
    } else {
        Write-Host "    the restarted process is not listed yet on ${OracleHost}; re-check by hand" -ForegroundColor Yellow
    }
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
Write-Host 'Routes: restart the API host (Desktop app or hermes serve) before a new route answers'
Write-Host 'Not verified here: that the Desktop pane actually renders. Open it and look.'
