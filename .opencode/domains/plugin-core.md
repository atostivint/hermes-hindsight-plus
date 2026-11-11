# Domain: plugin-core

## Owns
- `__init__.py` - entry point: `register()`, config resolution via `ctx.get_config`, the
  `pre_llm_call` hook, the per-session guard, the metadata-only broadcast.
- `core.py` - endpoint resolution, HTTP transport (GET + create POST), cache read/write,
  truncation, header, `context_note`, `create_spec`, `_create_missing_model`,
  `HindsightClient`, `build_injection[_details]`, and the local injection audit
  (`record_injection`, `read_injection_history`, `read_state_db_injections`,
  `list_injection_sessions`, `injection_audit_stats`).
- `plugin.yaml` - `config_schema` (every knob is declared there and consumed through
  `ctx.get_config`). `version` and `description` are bumped by `packaging-docs`.
- `tests/test_plugin.py` (47 tests) and `tests/test_injections_state_db.py` (5 tests).

## Does not own
- `dashboard/plugin_api.py`, `review.py` -> dashboard-backend (they load `core.py` **by path**,
  read-only, through `importlib`; they do not own it).
- `desktop/plugin.js`, `dashboard/manifest.json`, `assets/` -> desktop-ui.
- `README.md`, `docs/`, `LICENSE` -> packaging-docs.

## Invariants
- `register()` performs **no I/O**. Hermes requires it; `hermes plugins validate .` probes it.
- The hook never raises and never blocks longer than `timeout_s` (default 3.0 s).
- Injection is `{"context": "..."}`, appended to the **current user message**; system prompt and
  prompt cache are untouched. `max_chars` (default 6000) is clamped to Hermes' 10,000-char cap.
- The injected header flags the context as historical and non-authoritative. English wording is the
  reference; tests pin it, so changing the text means changing the tests in the same commit.
- The per-session guard makes a retried first turn inject nothing twice.
- Cheap off switches first (`mode: off`, empty `mental_model_ids`, `platform` in `skip_platforms`,
  `first_turn` and not `is_first_turn`) - no network, no disk read.
- Cache lives under the plugin data directory (`plugin_data_dir()` /
  `<HERMES_HOME>/plugin-data/<id>/`), never a hard-coded path.
- **Create-only toward Hindsight**: a `404` on a configured model may trigger exactly one
  `POST /v1/default/banks/{bank}/mental-models`. Never `PATCH`/`PUT`/`DELETE`, never touch
  `MEMORY.md`/`USER.md` or `memory.*` config, never retain into the bank. Invalid model ids are
  skipped, not POSTed.
- `trigger.refresh_cron` and `trigger.refresh_after_consolidation` are mutually exclusive: the
  plugin sets the cron and `refresh_after_consolidation: false`. Default cron `0 3 * * *`.
- The API key comes from `HINDSIGHT_API_KEY` or `hindsight/config.json`; it is never logged, never
  written in the injected text, never stored in the cache or the audit.
- No `assert` as a runtime guard (gone under `python -O`).

## Known traps
- `core.py` is loaded four ways: `from . import core`, a flat `import core` fallback, and
  `importlib` **by file path** from `dashboard/plugin_api.py`. A new relative import inside
  `core.py` breaks the by-path loaders. Keep it self-contained, stdlib only.
- Hermes parses YAML 1.1: an unquoted `off` arrives as the boolean `False`. `_normalize_mode`
  handles it, and string defaults that look boolean are quoted in `plugin.yaml`.
- The audit is fail-open: a broken audit must never lose the context, and content recording is
  opt-in (`record_content`).
- `PYTHONPATH` pointing at the hermes-agent checkout makes 5 tests here fail with
  `hermes_bootstrap.RelaunchExit: 1`. Measure with a plain `python -m pytest -q` (97 passed, ~9 s).
- `ruff check .` is **not** a gate: ~40 pre-existing findings. Lint only the files you touched.

## Proof
- `python -m pytest -q` (whole suite, ~9 s)
- `python -m compileall .`
- `ruff check <each changed .py file>`
- `hermes plugins validate .` when the manifest, the hook registration or `register()` changed
- named tests for anything new (e.g. `python -m pytest -q tests/test_plugin.py -k create_missing`)
- no live Hindsight call in a proof: state it as a gap instead

## Open threads
- Live bank behaviour (real `404`, real content generation) is only proven against the stub server.
