# hermes-hindsight-plus

> **Heads up: this is still a work in progress.** I use it with Hindsight day to day, but
> it isn't release-ready and it is **not listed in the Hermes plugin catalog**. Expect
> rough edges, no support, and no guarantees. Copy whatever's helpful, fork it, or take
> just the parts you need.

Inject [Hindsight](https://github.com/vectorize-io/hindsight) **mental models** into the
first turn of every Hermes Agent conversation, so each session starts with stable,
cron-refreshed context instead of a hand-maintained `MEMORY.md` / `USER.md`.

Version 0.5.0 — MIT.

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
- It does **not** retain anything back into Hindsight.
- Its only write is **create-if-missing**: if a configured model returns `404`, it creates
  that model (never updates, patches, or deletes an existing one).

## How it works

1. On `pre_llm_call`, if this is the first turn (default `mode: first_turn`), the plugin
   reads the configured Hindsight bank and mental models.
2. It caches each model on disk and only hits the network when the cache is stale.
3. It assembles the models (each under a `## <name>` heading) below a short header that
   flags the context as historical and non-authoritative over the current message.
4. It returns `{"context": ...}`, which Hermes appends to the **current turn's user
   message** — the system prompt and prompt cache are untouched.
5. It records a bounded local audit entry (time, turn, models, sizes, and optionally the
   injected text) for the Desktop memory pane. The audit is local only; see
   [Desktop memory pane](#desktop-memory-pane).

### Desktop memory pane

The plugin adds a **right-edge pane** to Hermes Desktop named **Memory**. Its tab title shows
how many injections are recorded for the focused chat. Open it to see the session's injection
history, newest first:

- when each injection happened and whether it was the first or a later turn;
- the mental model name(s), injected size, assembled size, and truncation state;
- the user-message preview and the rendered Markdown that was appended, when content
  recording is enabled;
- a **Copy** button for the exact injected text and a **Raw** toggle for the source.

The pane reads only this plugin's audit; it does not inspect other memory providers or the
chat transcript. A new chat shows the empty state until its first injection. The default
`mode: first_turn` produces one entry per chat; use `mode: every_turn` if you want the full
per-turn timeline.

In the Desktop app, enable the Desktop half under **Capabilities → Plugins** if it is off.
If the pane was closed, reopen it with the command palette: **Show injected Hindsight
memory**. Closing the pane disables the Desktop half, so the pane is not recreated until you
enable the plugin again.

### What the audit stores

The audit lives at:

```text
$HERMES_HOME/plugin-data/hermes-hindsight-plus/injection-audit.json
```

It is a single bounded JSON file with per-session, total-entry, and session-count limits. The
defaults keep at most 200 entries across 50 sessions, with 20 entries per session. Eviction is
oldest-first: per-session excess first, then older sessions, then the oldest total entries.

- `record_content: true` (default) stores the injected text (capped at 10,000 characters) and
  a 180-character user-message preview. The injected text is already sent to the model, but
  this file is local and readable by anything that can read your Hermes home.
- `record_content: false` stores metadata only: time, turn, platform/model, sizes, model names,
  and truncation state. Content and previews are omitted entirely.
- The API key, request headers, and raw Hindsight responses are never written to this file.
- The Desktop pane receives an injection event for an immediate refresh. The event carries
  only IDs and counts, never message content. If the event bridge is unavailable (for example
  under `plugins.isolation: host`), the pane's 15-second polling still picks up new entries.
- Deleting `injection-audit.json` clears the history. The plugin recreates it on the next
  injection.

Injection content is appended to the model's user message, so it still is not an inline chat
card; the pane is the sanctioned way to inspect exactly what the plugin added.

## Creating / refreshing the mental model in Hindsight

The plugin can bootstrap a missing model for you. On the first turn, it `GET`s each
configured model; if Hindsight returns `404` and `create_missing` is enabled (the
default), it `POST`s a new model with:

- the configured `id` (the value from `mental_model_ids`),
- `create_name` (default **Stable operating context**),
- `create_source_query` (the prompt Hindsight's reflect agent uses to generate content),
- `create_max_tokens` (default 2048),
- `trigger.refresh_cron` (default **`0 3 * * *`**, daily at 03:00 UTC),
- `trigger.mode` (`full` or `delta`).

Hindsight generates the content asynchronously in the background and returns an operation
id. The first turn that creates the model injects nothing (there is no content yet); once
the reflect run finishes, the next first turn reads and injects it.

**Existing models are never modified.** The plugin only issues `POST` on a `404`; it never
sends `PATCH`, `PUT`, or `DELETE`. If you already created a model, its name, source query,
schedule, and content stay exactly as you configured them. If you want a different
schedule for an existing model, change it in Hindsight.

The Hindsight endpoint must accept writes with the configured credentials. If creation is
forbidden (`401`/`403`) or fails for any reason, the plugin logs a warning and falls back
to the cache or injects nothing — it never raises.

> Creation happens in the `pre_llm_call` hook (first turn), not during `register()`:
> Hermes requires plugin registration to be I/O-free.

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

| Key                    | Default | Description |
|------------------------|---------|-------------|
| `mode`                 | `first_turn` | `first_turn`, `every_turn`, or `off`. |
| `mental_model_ids`     | `["operator-context"]` | Hindsight mental model ids to inject, concatenated in order. |
| `max_chars`            | `6000` | Truncation budget for the assembled injection (incl. marker). |
| `cache_ttl_s`          | `21600` | Seconds a cached model stays fresh (no network call). |
| `timeout_s`            | `3.0` | Total wall-clock budget for the whole injection attempt. |
| `skip_platforms`       | `["cron"]` | Platforms on which no injection happens. |
| `context_note`         | `""` | Optional static text injected before the model content. Use it for deployment/architecture facts. Never put secrets here. |
| `record_content`       | `true` | Keep the injected text and a short user-message preview in the local audit. `false` keeps metadata only. |
| `audit_max_entries`    | `200` | Maximum injection entries retained across all sessions (1–2000). |
| `audit_max_per_session`| `20` | Maximum injection entries retained per session (1–200). |
| `audit_max_sessions`   | `50` | Maximum sessions retained in the audit (1–500). |
| `create_missing`       | `true` | Create a configured model when Hindsight returns `404`. Existing models are never modified. |
| `create_name`          | `Stable operating context` | Name used only when creating a missing model. |
| `create_source_query`  | built-in prompt | Query Hindsight's reflect agent uses to generate a newly created model's content; the default asks for the deployment topology when known. |
| `create_refresh_cron`  | `0 3 * * *` | UTC 5-field cron for newly created models. Empty disables the schedule. |
| `create_max_tokens`    | `2048` | Maximum generated tokens for a newly created model (256–8192). |
| `create_mode`          | `full` | `full` or `delta` for newly created models. |

> Hermes parses YAML 1.1, where an unquoted `off` is the boolean `false`. The plugin
> normalizes both forms, but quoting it (`mode: "off"`) is clearest in `config.yaml`.

## Optimizing for your deployment

The plugin ships **generic** defaults: no hostnames, profile names, or bank names are
hard-coded. Put deployment-specific facts in settings, per profile:

- `context_note`: static text injected before the mental model content on every first
  turn. Use it for instance/profile/architecture facts. Empty by default. Never put
  secrets here — it is sent to the model.
- `create_source_query`: prompt Hindsight uses when creating a missing model. The default
  asks for the deployment topology when known; override it to match your own wording.
- `create_name`, `create_refresh_cron`, `create_max_tokens`, `create_mode`: shape the
  created model.

Example (`config.yaml`, per profile):

```yaml
plugins:
  entries:
    hermes-hindsight-plus:
      settings:
        context_note: |
          Deployment: this assistant is the `default` profile on the
          `primary` instance. The host named `secondary` is a separate
          instance. Profiles: default, work, personal.
```

The note is injected only when at least one mental model has content; it never triggers a
Hindsight write.

## Behaviour notes

- **Injection location:** the context is appended to the *current turn's user message*, not
  the system prompt, so Hermes' prompt caching stays intact.
- **Truncation:** content is truncated at the last complete line that fits within
  `max_chars`, then a marker line lists the full mental model id(s). The final text never
  exceeds `max_chars` (default 6000, well under Hermes' 10,000-character per-callback cap).
- **Create-if-missing only:** a `404` triggers one `POST` with the configured `create_*`
  options. An existing model is never patched. Hindsight generates the content
  asynchronously, so the creating turn injects nothing; the next first turn reads it.
- **Caching:** each model is cached at
  `$HERMES_HOME/plugin-data/hermes-hindsight-plus/<bank>--<model_id>.json`. On a network or
  parse error it falls back to the stale cache (age surfaced in the header); with no cache
  it injects nothing and logs a warning. The API key is never written to disk or logs.
- **Fail-open & non-blocking:** the hook catches all exceptions, logs, and returns `None`;
  it never raises, and the whole attempt (all models) shares one `timeout_s` budget.
- **No double injection:** a bounded per-session guard ensures a retried first turn does
  not inject the context twice.

## Verifying the injection

The context is appended to the first **user message** at API time; it is not shown in the
chat UI and it is not written to `MEMORY.md`. Three ways to confirm it ran:

1. Open the Desktop **Memory** pane. The current chat's entry shows the exact injected text,
   model names, and sizes. A new chat is empty until its first injection.
2. In a new conversation, ask the agent to repeat the first line of the stable-context
   block it received. A working injection starts with
   `## Stable context (Hindsight mental model, refreshed ...)`.
3. Read the plugin's success log line:
   ```bash
   grep 'hindsight-plus: injected' "$HERMES_HOME/logs/agent.log" | tail
   ```
   It reports the char count, `first_turn`, platform, and session id — never the content
   or the API key. A first-turn injection looks like:
   ```text
   hindsight-plus: injected 5767 chars (first_turn=True, platform=desktop, session=...)
   ```

The cache file's `fetched_at` / mtime is only a fetch signal, not proof of injection: a
fresh cache can be used without any network call.

## Limitations / what this does not do

- **First-turn injection into the user message, not the system prompt.** The context is
  appended to the user message of the current turn; it is not part of the system prompt.
- **No streaming update.** The context is fixed at the moment the turn starts; it is not
  refreshed mid-conversation.
- **Create-only, never update.** It creates a missing model (on `404`) but never patches,
  renames, reschedules, or deletes an existing one. Changing an existing model's content or
  schedule is done in Hindsight.
- **Async content generation.** A newly created model has no content until Hindsight's
  reflect run finishes; the creating turn injects nothing.
- **Write permission required.** If Hindsight forbids writes, creation fails open (warning,
  no injection) and the model must be created manually.
- **The inspector is a Desktop pane, not an inline chat card.** Hermes' plugin SDK has no
  per-message contribution slot, so the injected text appears in the right-edge **Memory**
  pane instead of inside the transcript. The pane is Desktop-only.
- **The audit is a bounded local history, not an archive.** It keeps the most recent entries
  allowed by `audit_max_*`; older entries are evicted. It starts when the plugin is updated,
  so earlier injections are not backfilled.
- **Settings use the standard plugin form.** They are rendered from `config_schema` under
  Desktop **Capabilities → Plugins**, or edited in `config.yaml`; there is no separate web
  dashboard.
- **Reads a pre-existing Hindsight setup.** It assumes the official Hindsight plugin (or an
  equivalent `config.json`) already exposes `api_url` / `bank_id`; it does not bootstrap
  Hindsight itself.
- **Work in progress.** Behaviour is pinned to the Hermes and Hindsight APIs as they were
  when this was written. Later changes to either may break it.

## Compatibility

At the time of writing, this was validated against the live Hermes loader
(`hermes_cli.plugin_validate`, the same code path as `hermes plugins validate`). It has
**not** been submitted to the plugin catalog — it isn't release-ready yet — and it is not
kept in sync with newer Hermes or Hindsight releases:

- manifest v2 parses, `config_schema` shape is valid, and the settings form renders all
  seventeen keys with the right defaults and `mode` / `create_mode` choices;
- the capability probe imports the plugin, runs `register()` in isolation, and confirms the
  declared `pre_llm_call` hook matches what is actually registered;
- the Desktop surface passes the admission lint (no dynamic code, remote imports, or app DOM
  access), and the dashboard manifest mounts the read-only `history`, `sessions`, and
  `health` routes;
- the security scan is `safe` and the plugin-host isolation verdict is `host` (host-ready).

## Development

Stdlib only, no third-party runtime dependency. Target Python 3.11+.

```bash
python -m compileall .
python -m pytest -q
```

