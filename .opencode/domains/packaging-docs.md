# Domain: packaging-docs

## Owns
- `plugin.yaml` - `version`, `description`, `tags`, `homepage`, `author` (the `config_schema` itself
  belongs to plugin-core).
- `README.md` - the public landing page: why, how it works, configuration, install, verification.
- `docs/` - plans and handoffs (`docs/PLAN-memory-review.md`, `docs/DEPLOY.md`).
- `scripts/` - `deploy.ps1`, the deploy routine for the two real hosts.
- `tests/test_deploy_script.py` (3 tests) - dry-run under 5.1 and pwsh 7, plus the no-stderr-redirection guard.
- `LICENSE` (MIT), `.gitignore`.

## Does not own
- `config_schema` knobs and defaults -> plugin-core. A new knob is declared there, then documented
  here - two files, one task, listed in SCOPE.
- `desktop/plugin.js`, `dashboard/manifest.json`, `assets/` -> desktop-ui.

## Invariants
- The POSTURE is **work in progress**: used day to day, **not release-ready**, **not submitted to the
  Hermes plugin catalog**. Do not write "unmaintained", "abandoned", or promise a release.
- Version numbers are consistent: `plugin.yaml`, `dashboard/manifest.json` and the README line
  ("Version 0.5.0 - MIT") move together.
- Every knob documented in the README exists in `plugin.yaml` `config_schema`, with the same default.
- **No local filesystem path in a public doc.** Machine-specific facts belong in
  `MACHINE-AGENTS.md`, never in `README.md` or `AGENTS.md`.
- The injected behaviour described in the README matches the code: create-if-missing only, never
  update an existing model, no `memory.*` change, no `MEMORY.md`/`USER.md` write, no retain.
- English is the language of the README and the docs.

## Known traps
- Memory of the project disagrees with itself on posture across versions; the current wording is
  "work in progress, not in the catalog". Older "unmaintained / migrate away" framing is history.
- `plugin.yaml` is parsed as YAML 1.1 by Hermes: quote any string default that looks boolean
  (`"off"`), and keep `choices` quoted.
- `hermes plugins validate .` re-reads the manifest: a typo in `provides_hooks` or `config_schema`
  shape shows up there, not in pytest.
- Don't "refresh" the README with features that only exist on a branch or in a plan document.
- A PowerShell script that redirects a native command's stderr (`2>&1`, `2>$null`) throws under
  Windows PowerShell 5.1 with `$ErrorActionPreference = 'Stop'` - measured, and it made a
  successful deploy exit 1. `tests/test_deploy_script.py` pins both the behaviour and the ban.
- A backup of a desktop half must never sit inside `<HERMES_HOME>/desktop-plugins/`: the loader
  serves every child folder there, so the copy is offered as a second plugin with the same id.
  `scripts/deploy.ps1` writes backups to `<HERMES_HOME>/backups/desktop-plugin-halves/`.
- `hermes plugins install` deploys what is committed on the remote branch, never the dirty
  working copy; the script warns but cannot deploy uncommitted work.

## Proof
- `hermes plugins validate .`
- `python -m compileall .`
- `python -m pytest -q --basetemp=<fresh dir>` when a documented default changed (tests pin several;
  a bare `pytest` fails on this machine's temp ACL)
- `powershell -NoProfile -File scripts/deploy.ps1 -Target all -DryRun` for any `scripts/deploy.ps1`
  change, plus `pwsh -File ... -Target windows -DryRun`
- the exact README lines changed, and what stays unverified

## Open threads
- Nothing is published to the plugin catalog, and nothing should be until the user asks.
- `AGENTS.md` still says "97 tests" (100 since `tests/test_deploy_script.py`); needs a one-line fix.
- `scripts/deploy.ps1` has never run outside `-DryRun`: the install/backup/copy branches were done by
  hand on 2026-11-11, not by the script.
