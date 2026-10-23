# hermes-hindsight-plus

Inject [Hindsight](https://github.com/vectorize-io/hindsight) **mental models** into the
first turn of every Hermes Agent conversation, so each session starts with stable,
cron-refreshed context instead of a hand-maintained `MEMORY.md` / `USER.md`.

Version 0.1.0 — MIT.

## Why mental models instead of MEMORY.md / USER.md

`MEMORY.md` and `USER.md` are files you must remember to keep in sync by hand. Hindsight
already stores the durable parts of that knowledge as "mental models": saved reflect
outputs that are refreshed automatically on a cron (for example one named
*Stable operating context* that holds your standing preferences and instructions).

This plugin reads one or more of those mental models at the start of every new
conversation and injects them into the **first turn's user message**. You stop editing
markdown files; the single source of truth stays in Hindsight, and Hermes always boots
with the same stable context.

It complements the official Hindsight memory provider (`memory.provider: hindsight`):
- It does **not** touch `memory.*` configuration.
- It does **not** write `MEMORY.md` or `USER.md`.
- It does **not** retain anything back into Hindsight (read-only).

## How it works

1. On `pre_llm_call`, if this is the first turn (default `mode: first_turn`), the plugin
   reads the configured Hindsight bank and mental models.
2. It caches each model on disk and only hits the network when the cache is stale.
3. It assembles the models (each under a `## <name>` heading) below a short header that
   flags the context as historical and non-authoritative over the current message.
4. It returns `{"context": ...}`, which Hermes appends to the **current turn's user
   message** — the system prompt and prompt cache are untouched.

## Creating / refreshing the mental model in Hindsight

This plugin only *reads* mental models; create them in Hindsight itself.

1. Create a mental model that captures your durable context. For example, in Hindsight
   use `create_mental_model` with a `source_query` that pulls your stable preferences and
   standing instructions (e.g. a query like *"my stable operating context: durable user
   preferences, standing instructions, tooling conventions"*). Give it a memorable name,
   such as **Stable operating context**.
2. Attach an automatic refresh trigger so the model stays current without manual edits —
   configure its refresh to run on a cron cadence that matches how often that context
   actually changes.
3. Note the model's **id** (the default config uses `operator-context`; set
   `mental_model_ids` to the id of your model, e.g. the id returned for *Stable operating
   context*).

> The exact Hindsight `create_mental_model` / refresh-trigger CLI or API surface should be
> confirmed against your Hindsight installation and version.

## Install

1. Copy this folder into your Hermes home:
   ```
   $HERMES_HOME/plugins/hermes-hindsight-plus/
   ```
2. Enable it, either with:
   ```
   hermes plugins enable hermes-hindsight-plus
   ```
   or by adding it to `plugins.enabled` in `config.yaml`.

The plugin reads the Hindsight endpoint from `$HERMES_HOME/hindsight/config.json`
(`api_url`, `bank_id`, and optionally `api_key`). If those are absent it falls back to the
environment variables `HINDSIGHT_API_URL`, `HINDSIGHT_BANK_ID`, and `HINDSIGHT_API_KEY`.

## Configuration

Settings live under `plugins.entries.hermes-hindsight-plus.settings` in `config.yaml`.
They are also rendered as a form in the Desktop app under **Capabilities → Plugins**
(the gear on this plugin's row), driven by the manifest's `config_schema`:

```bash
hermes config set plugins.entries.hermes-hindsight-plus.settings.mode first_turn
```

| Key               | Default            | Description                                                       |
|-------------------|--------------------|-------------------------------------------------------------------|
| `mode`            | `first_turn`       | `first_turn`, `every_turn`, or `off`.                             |
| `mental_model_ids`| `["operator-context"]` | Hindsight mental model ids to inject, concatenated in order.  |
| `max_chars`       | `6000`             | Truncation budget for the assembled injection (incl. marker).     |
| `cache_ttl_s`     | `21600`            | Seconds a cached model stays fresh (no network call).             |
| `timeout_s`       | `3.0`              | Total wall-clock budget for the whole injection attempt.          |
| `skip_platforms`  | `["cron"]`         | Platforms on which no injection happens.                          |

> Hermes parses YAML 1.1, where an unquoted `off` is the boolean `false`. The plugin
> normalizes both forms, but quoting it (`mode: "off"`) is clearest in `config.yaml`.

## Behaviour notes

- **Injection location:** the context is appended to the *current turn's user message*, not
  the system prompt, so Hermes' prompt caching stays intact.
- **Truncation:** content is truncated at the last complete line that fits within
  `max_chars`, then a marker line lists the full mental model id(s). The final text never
  exceeds `max_chars` (default 6000, well under Hermes' 10,000-character per-callback cap).
- **Caching:** each model is cached at
  `$HERMES_HOME/plugin-data/hermes-hindsight-plus/<bank>--<model_id>.json`. On a network or
  parse error it falls back to the stale cache (age surfaced in the header); with no cache
  it injects nothing and logs a warning. The API key is never written to disk or logs.
- **Fail-open & non-blocking:** the hook catches all exceptions, logs, and returns `None`;
  it never raises, and the whole attempt (all models) shares one `timeout_s` budget.
- **No double injection:** a bounded per-session guard ensures a retried first turn does
  not inject the context twice.

## Limitations / what this does not do

- **First-turn injection into the user message, not the system prompt.** The context is
  appended to the user message of the current turn; it is not part of the system prompt.
- **No streaming update.** The context is fixed at the moment the turn starts; it is not
  refreshed mid-conversation.
- **No write access to the bank.** This plugin is strictly read-only and never creates,
  updates, or deletes mental models in Hindsight.
- **No custom dashboard.** Settings use the standard plugin settings form rendered from
  `config_schema` (Desktop **Capabilities → Plugins**, or `config.yaml`); there is no
  separate web dashboard.
- **Reads a pre-existing Hindsight setup.** It assumes the official Hindsight plugin (or an
  equivalent `config.json`) already exposes `api_url` / `bank_id`; it does not bootstrap
  Hindsight itself.

## Compatibility

Validated against the live Hermes loader (`hermes_cli.plugin_validate`, the same code path
as `hermes plugins validate` / the plugin-catalog admission CI):

- manifest v2 parses, `config_schema` shape is valid, and the settings form renders all
  six keys with the right defaults and `mode` choices;
- the capability probe imports the plugin, runs `register()` in isolation, and confirms the
  declared `pre_llm_call` hook matches what is actually registered;
- the security scan is `safe` and the plugin-host isolation verdict is `host` (host-ready).

## Development

Stdlib only, no third-party runtime dependency. Target Python 3.11+.

```bash
python -m compileall .
python -m pytest -q
```

