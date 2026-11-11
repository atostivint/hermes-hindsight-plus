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

    `hermes plugins install` refreshes the agent half and reloads the gateway, but it does
    NOT refresh an already published desktop half whose bytes differ from the package's:
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

.EXAMPLE
    pwsh -File scripts/deploy.ps1 -Target all -DryRun

.EXAMPLE
    pwsh -File scripts/deploy.ps1 -Target oracle -OracleHost oracle
#>
[CmdletBinding()]
param(
    [ValidateSet('oracle', 'windows', 'all')]
    [string]$Target = 'all',

    [string]$OracleHost = 'oracle',

    [switch]$DryRun
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
    Write-Host '  [1/3] install the agent half (also reloads the gateway plugins)'
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
        return
    }

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
    Write-Host '  ok: windows deployed' -ForegroundColor Green
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

echo "  [1/3] install the agent half (also reloads the gateway plugins)"
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
echo "  ok: oracle deployed"
'@
# bash on stdin must never see a CR: normalise whichever line endings this file has.
$OracleScript = ($OracleScript -replace "`r`n", "`n").TrimEnd()

function Deploy-Oracle {
    Write-Host ''
    Write-Host "== oracle ($OracleHost) ==" -ForegroundColor Cyan
    if ($DryRun) {
        Write-Host "  ssh $OracleHost <<'SH'   (one invocation, script on stdin)"
        foreach ($line in ($OracleScript -split "`n")) { Write-Host "    $line" }
        Write-Host '  SH'
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
    Write-Host '  ok: oracle deployed' -ForegroundColor Green
}

# ------------------------------------------------------------------------ run

if ($Target -in @('windows', 'all')) { Deploy-Windows }
if ($Target -in @('oracle', 'all')) { Deploy-Oracle }

Write-Host ''
Write-Host "deploy complete: $Target, version $Version" -ForegroundColor Green
Write-Host 'Not verified here: that the Desktop pane actually renders. Open it and look.'
