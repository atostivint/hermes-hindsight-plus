# Orchestrator workspace - hermes-hindsight-plus

This directory is the orchestrator's memory. It is committed, so it survives sessions, compactions
and worktrees. It holds no application code.

| File | Role |
|---|---|
| `POLICY.md` | Hard rules, proof contract, scope rules, routing by domain, reply format. Read at triage. |
| `registry.md` | Domain -> worker session, files in play, status. The orchestrator's long-term memory. |
| `inbox.md` | Items to triage, their state (`ready`, `blocked`, `done`) and what proves it. |
| `decisions.md` | Dated user arbitrations; never erased, superseded lines are struck through. |
| `README.md` | This file: how to read the four above, and the order to start in. |

Domain notes live in `.opencode/domains/` (one per file surface).

## Reading the files, in this order
1. **`POLICY.md`** - the contract. Nothing below overrides it.
2. **`inbox.md`** - what is pending, and which items are blocked on the user.
3. **`registry.md`** - which domain already has a live worker session, and which files are in play.
   Reuse a session id instead of starting from scratch.
4. **`decisions.md`** - why the code looks the way it does. A decision is applied, not re-litigated.
5. **`.opencode/domains/<domain>.md`** - ownership, invariants, traps, proof, open threads for the
   surface the item landed in.

## Starting the session
Run OpenCode in this repository and keep **one** orchestrator session alive per project; do not open
a new one per item. Name it `hermes-hindsight - orchestrator`.

## The loop
1. `/triage <item>` or "take the next item from `.orchestrator/inbox.md`".
2. Classify it against `.opencode/domains/`, check `git worktree list` and `git status --porcelain`,
   then write the brief: GOAL / SCOPE (exact files) / ACCEPTANCE (exact commands) / PROOF / STOP.
3. One `worker` per independent task, in background when the files are disjoint.
4. No PROOF block -> send back once; otherwise close as failed. Check the diff yourself first.
5. One-pass review with `reviewer` on the resulting diff.
6. Update `registry.md`, the domain note and the item's inbox status.
7. Report to the user in French, short, with what could not be verified.

## What not to do
- Do not let the orchestrator edit an application file: it is denied by permission, on purpose.
- Do not run two items on the same shared file (`core.py`, `plugin.yaml`, `desktop/plugin.js`,
  `tests/conftest.py`).
- Do not duplicate work already present in a worktree or an unmerged branch.
- Do not commit or push without the user asking; do not touch the untracked `assets/` folder.
- Do not treat `ruff check .` as a gate (the repo has ~40 pre-existing findings).

## Switching back to direct work
OpenCode's agent picker switches to `build` for a normal coding session. The orchestrator stays
configured for the next triage.
