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

Reference checkout: `the local Hermes Agent checkout`. Relevant surfaces:

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
```

## Repo rules

- Work only inside this repo. The private remote is
  `origin` = `https://github.com/atostivint/hermes-hindsight-plus` (never public).
- Commit to `main` with a clear message.
