# Orchestration policy - hermes-hindsight-plus

The orchestrator reads this file at triage time. Workers only get what the brief quotes from it.

## Hard rules (from AGENTS.md, repeated because they are not negotiable)
- The orchestrator **never edits an application file itself**. It triages, writes briefs, checks
  proofs, and writes only in `.orchestrator/` and `.opencode/domains/`. Application edits are denied
  on purpose.
- Never commit or push unless the user asks; the user decides.
- Answer in French, short.
- Python **standard library only** at runtime. `core.py`, `review.py`, `__init__.py` and
  `dashboard/plugin_api.py` may not gain a third-party import (pytest/ruff are dev tools only).
- Never log, print or persist the Hindsight API key.
- No `assert` as a runtime guard: it disappears under `python -O`. Raise explicitly; the hook catches
  broadly and returns `None`.
- `pre_llm_call` never raises and never blocks a turn longer than `timeout_s`.
- Writes toward Hindsight are **create-only** (`404` -> one `POST .../mental-models`) plus the opt-in
  Memory review carve-outs (soft `PATCH` on a memory fact, one manual `POST .../memories`).
  `DELETE` is forbidden everywhere, on every endpoint.

## Proof contract
A task is accepted only if its PROOF block contains:
1. `git diff --stat` (files changed).
2. Every command run, with exit code and the relevant output. Minimum for any code change:
   - `python -m pytest -q` (whole suite: 100 tests, ~10 s - see the note below)
   - `python -m compileall .`
   - `ruff check <each changed .py file>` (the repo has ~40 pre-existing findings; never
     `ruff check .` as a gate, and never "fix" files outside the brief)
   - `node --check desktop/plugin.js` for any change to `desktop/plugin.js`
   - `hermes plugins validate .` when `plugin.yaml`, the hook registration or the desktop surface changed
3. Tests that ran, **by name**, for anything new.
4. `gaps`: what could not be verified (live Hindsight bank, Hermes Desktop rendering, real `hermes`
   session, anything requiring the user's credentials).

**Do not set `PYTHONPATH` to the hermes-agent checkout for the suite**: it makes 5 tests in
`tests/test_plugin.py` die with `hermes_bootstrap.RelaunchExit: 1` (measured 2026-11-11). A plain
`python -m pytest -q` is 100 passed in ~9 s. If a brief needs that environment, it says so explicitly
and expects those 5 known failures.

**On this machine, `pytest` needs a fresh `--basetemp`**: without it, 48 tests die with
`PermissionError` on `%TEMP%\pytest-of-elekt` (stale ACL, nothing to do with the code, measured
2026-11-11). Every brief's acceptance command is
`python -m pytest -q --basetemp=$env:TEMP\hhp-$(Get-Date -Format HHmmss)` → 100 passed.

A summary without commands and a diff is rejected once. A second time, the task is closed as failed.

## No silent death
- A worker writes the **code first**, in its first tool calls, then runs the checks, then reports.
  A report without a modified file is a failed task, however polite it is.
- Every brief ends with: "if you run out of steps, leave the file in a working state and say exactly
  what is missing; never end with only a plan".
- The orchestrator checks the tree (`git status --porcelain`, `git diff --stat`) after EVERY worker
  returns, before reading its report. A worker's "it passes" is worth nothing without a diff.
- A worker that dies twice on the same task gets it split in two, not restarted unchanged.

## Scope rules
- The brief lists **exact files**. No file outside the list may be touched.
- Shared files, one active task at a time on each:
  - `core.py` (used by `__init__.py`, `dashboard/plugin_api.py` and `review.py` consumers)
  - `plugin.yaml` (`config_schema` and `version` move together)
  - `desktop/plugin.js` (single blob-loaded file: pane and review page live inside it)
  - `tests/conftest.py`
- Two tasks in parallel need **disjoint files**. Worktrees are cheap but the tests import the package
  from `__file__`, so a second worktree is required whenever both tasks edit `tests/`.
- Before creating a task: `git worktree list`, `git status --porcelain`, and read
  `.orchestrator/registry.md` + `.orchestrator/inbox.md`. The work may already exist.
- Untracked `assets/` is the user's. Do not delete, move or commit it without asking.

## Routing
| The item is about | Domain |
|---|---|
| `pre_llm_call`, config resolution, HTTP/cache/create-if-missing, injection text, local audit | `plugin-core` |
| dashboard REST routes, `review.py` (list, revoke, restore, update, create, apply_document, verdicts) | `dashboard-backend` |
| Desktop pane, Memory review page, rendering rules of the plugin SDK surface | `desktop-ui` |
| `plugin.yaml` version/description, `README.md`, `docs/`, `.gitignore`, packaging | `packaging-docs` |

## Reply format (triage)
At most 8 lines:
```
#ID (one-line restatement) - related: #OTHER.
Domain: <domain> (session: <id or new>).
Covers: <what the fix changes>.
Unverified: <live bank / Desktop / hermes session>.
```

## After each task
- Update `.orchestrator/registry.md` (one row per domain).
- Update the domain note's "Open threads" if it moved.
- Update the item's status in `.orchestrator/inbox.md`.
- Record a dated line in `.orchestrator/decisions.md` when the user arbitrated something.
- The worker reports; the orchestrator writes. No application file is edited by the orchestrator.
