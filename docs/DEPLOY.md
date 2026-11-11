# Deploying hermes-hindsight-plus

Two halves ship together: the **agent half** (`*.py`, `plugin.yaml`, `dashboard/`) lands in
`<HERMES_HOME>/plugins/hermes-hindsight-plus/`, the **desktop half** (`desktop/plugin.js`) in
`<HERMES_HOME>/desktop-plugins/hermes-hindsight-plus/plugin.js`.

The agent half is installed by Hermes. The desktop half is published by Hermes' reconciler
on install — but only when the copy it already has is provably the package's own file (see
the trap below).

## Fresh host

```bash
hermes plugins install https://github.com/atostivint/hermes-hindsight-plus.git --force --yes-deps --enable
```

This clones the repo, copies the agent half, reloads the gateway plugins, and publishes the
desktop half. It is the only mechanism that works on both real hosts (Linux `oracle`, this
Windows machine).

## New version

Same command: `hermes plugins install <git url> --force --yes-deps --enable`. Use
`pwsh -File scripts/deploy.ps1 -Target all` (or `-Target oracle` / `-Target windows`, add
`-DryRun` to preview). It runs that install, then repairs and checks the desktop half.

Do **not** use `hermes plugins update <name> --yes` (`--yes` is only valid with no name). And
`hermes plugins update <name>` answers "already up to date" when the clone already tracks
`origin/main`, without refreshing the desktop half.

## The `.hermes-package.json` trap

`<HERMES_HOME>/desktop-plugins/<id>/` is refreshed by the reconciler **only** when the copy
carries the package marker `.hermes-package.json`. An unmarked copy whose `plugin.js` differs
from the package's `desktop/plugin.js` is treated as the user's own plugin: never overwritten,
never refreshed. That is how a stale desktop half survived two installs — on **both** hosts
(2026-11-11).

The marker is written — and the copy adopted — when the published file is byte-identical to
`<HERMES_HOME>/plugins/<id>/desktop/plugin.js`. So the repair is always the same:

1. compare the two files **by hash**, never by byte size: the git checkout stores LF, a hand
   copy of the Windows working tree stores CRLF, so identical content differs by hundreds of
   bytes — and different content can share a size;
2. if the hashes differ, back the published folder up to
   `<HERMES_HOME>/backups/desktop-plugin-halves/<id>-<timestamp>` (never inside
   `desktop-plugins/`, which the loader serves: a backup there is offered as a second copy of
   the same plugin id) and copy the package's `desktop/plugin.js` over the published one;
3. re-compare; Hermes stamps `.hermes-package.json` on the next reconcile and future versions
   then update themselves.

Compare with `sha256sum <package half> <published half>` on any host.

## What cannot be verified from here

The script proves both halves are installed, that `hermes plugins show` reports the expected
version on each host, and that the two `plugin.js` files hash the same. It cannot prove that the
Desktop pane renders: with no Electron on the Linux host (headless web UI), only a human looking
at the pane confirms it. A hard refresh or a gateway restart may be needed before the browser
picks the new file up.
