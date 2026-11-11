# Deploying hermes-hindsight-plus

Two halves ship together: the **agent half** (`*.py`, `plugin.yaml`, `dashboard/`) lands in
`<HERMES_HOME>/plugins/hermes-hindsight-plus/`, the **desktop half** (`desktop/plugin.js`) in
`<HERMES_HOME>/desktop-plugins/hermes-hindsight-plus/plugin.js`.

The agent half is installed by Hermes. The desktop half is published by Hermes' reconciler
on install — but only when the copy it already has is provably the package's own file (see
the trap below).

## Topology

The two halves do not live on the same machine, and that is not obvious from inside the app:

* the **desktop half** (`plugin.js`) loads **locally**, from the `desktop-plugins/` of the
  machine running the Desktop app;
* the **plugin API** (`/api/plugins/hermes-hindsight-plus/...`, `review.py`) is served by the
  **gateway of the host the app is connected to**. A Desktop app can be a plain remote client:
  its `desktop.json` `connectionMode` and `remoteUrl` may even be stale, so trust the boot line
  in `logs/desktop.log` (`Connecting to remote Hermes backend at ...`) over the config file.

So a route the new version adds is only reachable once **the backend gateway** restarted, even
though the app that asked for it runs locally. A version bump must therefore reach both the
backend install and the local desktop half; a new route additionally needs the backend gateway
restarted. Measured 2026-11-11 on this deployment: the Windows Desktop app boots with
`[boot] Connecting to remote Hermes backend at https://oracle.blue-avior.ts.net`, so that
tailnet front is the endpoint serving `/api/plugins/...` — there is no local API host to restart
on Windows. `scripts/deploy.ps1 -Target all` covers both halves, and `-RestartApi` restarts the
backend gateway and proves the restart took.

## Fresh host

```bash
hermes plugins install https://github.com/atostivint/hermes-hindsight-plus.git --force --yes-deps --enable
```

This clones the repo, copies the agent half, reloads the gateway's plugin hooks (see
[New HTTP routes need a restart](#new-http-routes-need-a-restart)), and publishes the desktop
half. It is the only mechanism that works on both real hosts (Linux `oracle`, this Windows
machine).

## New version

Same command: `hermes plugins install <git url> --force --yes-deps --enable`. Use
`pwsh -File scripts/deploy.ps1 -Target all` (or `-Target oracle` / `-Target windows`, add
`-DryRun` to preview). It runs that install, then repairs and checks the desktop half.

Do **not** use `hermes plugins update <name> --yes` (`--yes` is only valid with no name). And
`hermes plugins update <name>` answers "already up to date" when the clone already tracks
`origin/main`, without refreshing the desktop half.

## New HTTP routes need a restart

`hermes plugins install` reloads the plugin **hooks** in the running gateway — the install says
so itself ("active in the running gateway now: hooks"). It does **not** rebuild the
dashboard/HTTP **route table**: that table is built when the process serving the plugin API
starts. A version that adds a route therefore answers, on that route,

```
404 {"detail":"Not Found"}
```

while the routes that already existed keep working. Measured on 2026-11-11 with v0.6.0: the
install was clean, the list route was fine, and the new card route 404'd on the first click.
The code was correct and installed; the server had not been restarted.

Two cases:

* **systemd host** (the gateway runs as `hermes-gateway.service`, e.g. a headless backend) —
  `hermes gateway restart` refuses without root: use
  `sudo systemctl restart hermes-gateway.service` (passwordless sudo as the service user on
  the known host), then `systemctl is-active hermes-gateway.service` until it prints `active`.
* **Desktop app without that service** — when it really serves the plugin API itself, quit it
  completely, tray icon included, then relaunch it. `scripts/deploy.ps1` prints this step and
  never does it for you: that window is yours.

`pwsh -File scripts/deploy.ps1 -Target all -RestartApi` does the restart and proves it. On the
Linux host it runs `sudo systemctl restart hermes-gateway.service`, waits for `active`, then
checks one invariant: **the gateway process must have started after `dashboard/plugin_api.py`
was last written**, because that file is what builds the route table.

```bash
PID=$(systemctl show -p MainPID --value hermes-gateway.service)
stat -c %Y /proc/$PID                                        # process start, epoch seconds
stat -c %Y "$HOME/.hermes/plugins/hermes-hindsight-plus/dashboard/plugin_api.py"
```

If the process start is older than the file's mtime, the script fails with
`route table is older than the plugin: restart did not take`; otherwise it prints both epochs
and the delta between them. On Windows there is nothing to restart: the Desktop app is a remote
client of the oracle backend, so `/api/plugins/...` is not served by a gateway on that machine.
The script says so, never kills a process by name, and never touches the Desktop app.

There is deliberately **no HTTP probe**. On this deployment an unauthenticated request proves
nothing: the tailnet front answers `401` for *every* path — a bogus
`/api/plugins/hermes-hindsight-plus/zzz-nope` included — so a `401` cannot distinguish an
existing route from a missing one. The mtime/start-time comparison is auth-free and
deterministic.

How to tell this apart from a routing bug: the route exists in the code and in the installed
package, older routes on the same router answer, the new one is the only 404 — and after a
restart the gateway process start time is newer than `plugin_api.py`, while a stale table means
the process started before the file was written.

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
