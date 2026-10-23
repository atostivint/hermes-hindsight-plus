# AGENTS.md — operating brief

Hermes Agent plugin (`hermes-hindsight-plus`) that injects Hindsight mental models into
the first turn of every conversation, replacing hand-maintained `MEMORY.md` / `USER.md`.

## Hard constraints (do not violate)

- **Stdlib only.** `json`, `urllib.request` / `urllib.parse`, `pathlib`, `threading`,
  `time`, `logging`, `os`, `datetime`. No third-party runtime dependency. Target Python 3.11+.
- **Never log or persist the API key.** The key comes from `HINDSIGHT_API_KEY` or
  `hindsight/config.json`'s `api_key`. Keep it out of logs, out of the injected text, and
  out of the on-disk cache.
- **No `assert` as a runtime guard** anywhere in plugin code (it disappears under
  `python -O`). Raise explicit errors; for the hook, catch broadly and return `None`.
- **Never raise from the `pre_llm_call` hook.** It must never block a turn longer than
  `timeout_s` and must never crash a conversation.
- **Read-only toward Hindsight.** Do not write `MEMORY.md`/`USER.md`, do not touch
  `memory.*` config, do not retain anything into the bank.
- **Stay under Hermes' 10,000-char per-callback cap.** `max_chars` default 6000 already
  does; `core.build_injection` clamps `max_chars` to that ceiling defensively.

## Layout

- `__init__.py` — entry point (`register`), config resolution via
  `ctx.get_config(key, default)`, the hook, the per-session guard. Loaded as a package;
  keep `from . import core` (the directory name has a hyphen, so the module is not
  importable by its on-disk name).
- `core.py` — endpoint resolution, HTTP transport, cache read/write, truncation, header,
  and `build_injection`. Uses `plugins.plugin_storage.plugin_data_dir` when running inside
  Hermes and falls back to `<HERMES_HOME>/plugin-data/<id>/` otherwise.
- `plugin.yaml` — manifest v2: `provides_hooks: [pre_llm_call]` and a `config_schema`
  **mapping** of `key -> {type, description, default, choices?}` (not JSON-Schema
  `properties`). Hermes parses YAML 1.1, so quote `"off"` in `choices`.
- `tests/` — pytest suite. `conftest.py` loads the package under a valid module name.

## Live Hermes integration

Reference checkout: `the local Hermes Agent checkout`. Relevant surfaces:

- `plugins.entries.<id>.settings.<key>` is read through `ctx.get_config`.
- `pre_llm_call` receives `is_first_turn`, `session_id`, `platform`, `turn_id`, etc.;
  return `{"context": "..."}` to append to the current user message.
- `hermes plugins validate <dir>` (admission) and `hermes plugins doctor <dir>` run a
  capability probe against a recording context. The plugin must import cleanly, declare
  `provides_hooks`, and perform no I/O during `register()`.

## Commands

```bash
python -m compileall .
python -m pytest -q
```

## Repo rules

- Work only inside this repo. The private remote is
  `origin` = `https://github.com/atostivint/hermes-hindsight-plus` (never public).
- Commit to `main` with a clear message.
