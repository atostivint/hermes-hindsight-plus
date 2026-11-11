# AGENTS.md — operating brief

Hermes Agent plugin (`hermes-hindsight-plus`) that injects Hindsight mental models into
the first turn of every conversation, replacing hand-maintained `MEMORY.md` / `USER.md`.
It creates a configured model only when it is missing, with a daily refresh by default, and
never updates an existing model.

## Hard constraints (do not violate)

- **Stdlib only.** `json`, `urllib.request` / `urllib.parse` / `urllib.error`, `pathlib`,
  `threading`, `time`, `logging`, `os`, `re`, `datetime`. No third-party runtime dependency.
  Target Python 3.11+.
- **Never log or persist the API key.** The key comes from `HINDSIGHT_API_KEY` or
  `hindsight/config.json`'s `api_key`. Keep it out of logs, out of the injected text, and
  out of the on-disk cache.
- **No `assert` as a runtime guard** anywhere in plugin code (it disappears under
  `python -O`). Raise explicit errors; for the hook, catch broadly and return `None`.
- **Never raise from the `pre_llm_call` hook.** It must never block a turn longer than
  `timeout_s` and must never crash a conversation.
- **Create-only toward Hindsight.** The only allowed write is
  `POST /v1/default/banks/{bank}/mental-models` when a configured model returns `404` and
  `create_missing` is enabled. Never `PATCH`/`PUT`/`DELETE` an existing model, never write
  `MEMORY.md`/`USER.md`, never touch `memory.*` config, and never retain anything into the
  bank. An existing model's name, source query, schedule, and content are the user's.
  **Narrow carve-out (opt-in Memory review page, `review.py`):** only on an explicit user
  action in the Desktop page, `PATCH /v1/default/banks/{bank}/memories/{id}` may soft-invalidate
  (`state=invalidated` + reason), restore (`state=valid`), or rewrite the text of a *memory
  fact*, in banks `hermes|atlas|astro` only. Never `DELETE`, never automatically (an agent
  verdict is advice, not an action), never on mental models, never from the hook.
  **Named derogation — manual memory creation (Memory review page only):** the SOLE
  additional write allowed is `POST /v1/default/banks/{bank}/memories`, triggered by an
  explicit click on "Ajouter un souvenir" in the Memory review page. Never from
  `pre_llm_call`, never automatic, never in batch, banks `hermes|atlas|astro` only.
  `DELETE` is always forbidden, on every endpoint.
- **Creation is bounded.** Only on `404`; one POST per missing model; default
  `trigger.refresh_cron = "0 3 * * *"`; creation happens in the `pre_llm_call` hook, never
  in `register()` (Hermes requires registration to be I/O-free). Invalid model ids are
  skipped rather than POSTed.
- **Keep defaults generic.** No hostnames, profile names, or bank names in code or manifest
  defaults. Deployment-specific facts belong in per-profile settings: `context_note`
  (static text injected before the model content) and `create_source_query` (used only
  when creating a missing model).
- **Stay under Hermes' 10,000-char per-callback cap.** `max_chars` default 6000 already
  does; `core.build_injection` clamps `max_chars` to that ceiling defensively.

## Layout

- `__init__.py` — entry point (`register`), config resolution via
  `ctx.get_config(key, default)`, the hook, the per-session guard. Loaded as a package;
  keep `from . import core` (the directory name has a hyphen, so the module is not
  importable by its on-disk name).
- `core.py` — endpoint resolution, HTTP transport (GET + create POST), cache read/write,
  truncation, header, `context_note`, `create_spec`, `_create_missing_model`,
  `HindsightClient.create_model`, and `build_injection`. Uses
  `plugins.plugin_storage.plugin_data_dir` when running inside Hermes and falls back to
  `<HERMES_HOME>/plugin-data/<id>/` otherwise.
- `plugin.yaml` — manifest v2: `provides_hooks: [pre_llm_call]` and a `config_schema`
  **mapping** of `key -> {type, description, default, choices?}` (not JSON-Schema
  `properties`). The `context_note` / `create_*` settings carry deployment-specific
  customization. Hermes parses YAML 1.1, so quote `"off"` in `choices`.
- `tests/` — pytest suite, including a live HTTP round-trip for `404` → create POST → GET.
  `conftest.py` loads the package under a valid module name.

## Live Hermes integration

Reference checkout: a local clone of the Hermes Agent repository (path varies per
machine). Relevant surfaces:

- `plugins.entries.<id>.settings.<key>` is read through `ctx.get_config`.
- `pre_llm_call` receives `is_first_turn`, `session_id`, `platform`, `turn_id`, etc.;
  return `{"context": "..."}` to append to the current user message.
- `hermes plugins validate <dir>` (admission) and `hermes plugins doctor <dir>` run a
  capability probe against a recording context. The plugin must import cleanly, declare
  `provides_hooks`, and perform no I/O during `register()`.

## Hindsight create API

- `POST /v1/default/banks/{bank_id}/mental-models`
- Body: `{id, name, source_query, max_tokens, trigger: {mode, refresh_cron, refresh_after_consolidation}}`
- `trigger.refresh_cron` and `trigger.refresh_after_consolidation` are mutually exclusive;
  the plugin sets the cron and `refresh_after_consolidation: false`.
- Response: `{mental_model_id, operation_id}`. Content generation is asynchronous; the
  creating turn injects nothing.
- `PATCH /v1/default/banks/{bank_id}/mental-models/{id}` exists but is intentionally unused.

## Commands

```bash
python -m compileall .
python -m pytest -q
ruff check <each changed .py file>
node --check desktop/plugin.js          # after any desktop/plugin.js change
hermes plugins validate .               # after plugin.yaml / hook / desktop changes
```

- `python -m pytest -q` is the whole suite: 97 tests, about 9 s on this machine.
- **Do not set `PYTHONPATH` to the hermes-agent checkout for the suite.** It makes 5 tests in
  `tests/test_plugin.py` die with `hermes_bootstrap.RelaunchExit: 1` (measured 2026-11-11). The
  plugin imports nothing from that checkout; only the live integration needs it.
- `ruff check .` reports ~40 pre-existing findings. Lint the files you touched, and do not reformat
  files outside the brief.
- There is no Rust build and no `.cargo/`/`tools/` wrapper here: a Python test run is short and
  cannot starve the machine the way a cargo build can (see `MACHINE-AGENTS.md`). Still, do not run
  two full suites at once for no reason.

## Orchestration (one orchestrator, several workers)

The working contract lives in `.orchestrator/` (`POLICY.md` is authoritative) and the per-surface
ownership in `.opencode/domains/`. What this means for any agent working here:

- **The orchestrator never edits an application file.** It triages, writes briefs, checks proofs,
  and writes only in `.orchestrator/` and `.opencode/domains/`.
- **One worker per independent task**, in parallel only when the files are disjoint. Shared files,
  one task at a time: `core.py`, `plugin.yaml`, `desktop/plugin.js`, `tests/conftest.py`.
- **Every brief is GOAL / SCOPE (exact files) / ACCEPTANCE (exact commands) / PROOF / STOP.** No file
  outside SCOPE is touched, even to fix a typo.
- **A proof is a diff plus commands plus outputs.** `git diff --stat`, every command with its exit
  code, the named tests that ran, and a `gaps:` list. A report without them is rejected once, then
  closed as failed.
- If you run out of steps, leave the file in a working state and say exactly what is missing. Never
  end with only a plan.

## Repo rules

- Work only inside this repo. The remote is
  `origin` = `https://github.com/atostivint/hermes-hindsight-plus`. The plugin is a work in
  progress and is **not** submitted to the Hermes plugin catalog.
- Commit to `main` with a clear message.
- **Do not commit, push, or publish anything without the user asking.** Read `git status`
  first; the untracked `assets/` folder is the user's.
- Answer in **French, short**, unless the user writes in another language. Code, code comments and
  the README stay in English; UI copy inside the injected prompt is English and pinned by tests.
- Work in parallel means **one agent per file**. Declare your files before editing; merge by branch,
  never by copying files over each other.
- Do not change an existing mental model, `MEMORY.md`/`USER.md`, or `memory.*` config, and do not
  `DELETE` anything in Hindsight. The only writes are the ones listed in "Hard constraints".
- Documentation ships with the change: a new setting is declared in `plugin.yaml`, used through
  `ctx.get_config`, documented in `README.md`, and pinned by a test.
