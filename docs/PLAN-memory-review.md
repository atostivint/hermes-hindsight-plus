# Plan: opt-in "Memory review" page (v0.6.0 line, base 6c8e73c / v0.5.0)

Status legend: **live** = checked against the running Hindsight (read-only GET),
**schema** = read from its `/openapi.json` (Hindsight 1.1.0), **source** = read from
the Hermes Desktop source checkout, **unverified** = not tested.

## 1. Existing pane (what we must not break)

- `desktop/plugin.js` (610 lines): one default-exported plugin, id `hermes-hindsight-plus`.
  `PANES_AREA` pane `memory` (right dock, 360px) + one `PALETTE_AREA` item. Module-level
  `rest = ctx.rest`, `liveRevision` atom for the `plugin.<id>.injection.recorded` event.
  Reusable: the `styles` object, `rest()` (dashboard REST with `timeoutMs`), `formatWhen`/
  `fullWhen`/`formatNumber`, SDK `Badge/Button/Codicon/EmptyState/ErrorState/Loader/ScrollArea`.
- `dashboard/plugin_api.py`: read-only FastAPI router (`/health`, `/history`, `/sessions`)
  that loads `core.py` by path (`_core()`), mounted at `/api/plugins/hermes-hindsight-plus/`.
- `core.py`: `resolve_endpoint(hermes_home)` (env, then `hindsight/config.json`) returns
  `(api_url, bank_id, api_key)`; `_http_get_json` sets `Authorization: Bearer`. Reused for
  the URL and key. There is no PATCH helper, so the new module adds one.
- **Collisions:** (1) one plugin = one id = one toggle, so `defaultEnabled:false` also turns
  the existing injection pane off by default (see risks); (2) both register under the same
  `ctx`, so ids must differ (`page`, `nav`, `open-review`); (3) `rest` and `styles` are
  shared module state, the review section only reads them; (4) the audit routes stay as is.
- **Cannot be a separate file (source):** the loader reads `plugin.js` as text and imports
  it from a blob, so relative imports cannot resolve (`runtime-loader.ts:335`), and one file
  yields one plugin. The review UI is therefore a clearly delimited section
  (`// ==== Memory review ... ====`) appended before `export default`, with a one-block hook
  in `register()`. Backend logic lives in a new `review.py` (stdlib only, no `core` import).

## 2. Invariants (observed, from AGENTS.md, code and tests; none invented)

1. Stdlib only, Python 3.11+. 2. Never log/persist/return the API key. 3. No `assert` as a
runtime guard (suite must pass under `python -O`). 4. `pre_llm_call` never raises.
5. Create-only toward Hindsight: the only write is `POST .../mental-models` on 404 with
`create_missing`; never PATCH/PUT/DELETE a mental model; never retain. 6. `register()` does no
I/O. 7. Keep defaults generic: no hostnames/bank names in defaults. 8. Injection stays
under the 10,000-char callback cap. 9. Audit store never holds the key. 10. Tests load the
package via `conftest.py`; dashboard test fakes `fastapi`. 11. `plugin.yaml` `config_schema`
is a mapping; quote `"off"`.
**Conflict:** invariant 5 says "the only allowed write". The review feature adds user-initiated
PATCH on *memories* (facts), never on mental models. `AGENTS.md` gets a narrow carve-out in
the same series of commits. Needs your sign-off (open question 1).

## 3. STEP 1 verification

| Point | Result |
|---|---|
| `GET /v1/default/banks/{bank}/memories/list` returns an id and a fact date | **verified live**: bank `hermes` (44,930 facts) and `atlas` (6,721): 20/20 items carry `id`, `date`, `mentioned_at`, `state`, `fact_type`; `occurred_start` on only 6/20 and 11/20. `created_at` is absent from list items. `astro` exists but is empty (0). Newest first. |
| Pagination / filters | **verified live + schema**: `limit`, `offset`, `total`; `state`, `type`, `q`, `time_field`, `start_date`, `end_date` filters. |
| `PATCH /memories/{id}` takes `state` + `reason` | **verified from schema** (`UpdateMemoryRequest`: `state` 'invalidated'/'valid', `reason`, `text`). Description: soft, reversible, excluded from recall, kept for audit; only world/experience facts curatable. **Not tested live, by instruction**; exercised only against the stub server. |
| PATCH response body | **schema gives none** (empty 200). Design change: the backend GETs the fact first and returns that as `previous` (undo token), then GETs again for `current`. |
| `text` update side effects | schema: re-embeds, drops derived observations/links, re-consolidates. So Update is heavier than revoke; the UI says so in its confirm dialog. |
| No DELETE needed | verified: DELETE exists on `/memories` and observations; never called. |
| Auth | no key configured on this machine (`key set: False`); the server answered unauthenticated. The Bearer path is unit-tested against the stub. |
| Desktop page + nav (source) | verified: `ROUTES_AREA` (`data.path`, `render`) and `SIDEBAR_NAV_AREA` (`data: {codicon,label,path}`, no render) as the kanban plugin does. `defaultEnabled` is read from the default export. Note: your "geometry inside data" applies to panes only; routes/nav take `path`/`codicon,label,path`. |
| Start a chat from the plugin (source) | verified path exists, two ways: (A) `host.newChat()`, then `host.composer.setDraft('new', text)` (user presses Enter) or `host.composer.submit('new', text)`; (B) `host.request('session.create', {title})` returns a snapshot with `session_id`, then `host.request('prompt.submit', {session_id, text})`, then `host.openSession(id)`. **Choice: B** (the brief asked for `host.request`), A as fallback if B throws. Neither was run live (no Desktop session driven). |
| Read the verdict back | **unverified**. `session.resume` returns the transcript but extracting the final assistant message from a plugin was not exercised. Design: the prompt demands one fenced `json` block; the page polls `session.resume` and parses it; if that fails, a paste box accepts the block. Verdicts only ever render as badges. |

## 4. Design

Backend `/api/plugins/hermes-hindsight-plus/review/{bank}/...`, bank in `hermes|atlas|astro`
only (allowlist; the configured `bank_id` is ignored):
`GET memories` (limit<=200, offset, state, q, sort=`age|volatile|staleness`),
`POST memories/{id}/revoke` `{reason}` (must start `superseded by ` or `outdated: `),
`POST .../restore`, `POST .../update` `{text}`. Each mutation returns `{previous, current}`.
No route calls DELETE. The key is read server-side per request and never serialized.
Scoring: `age_days` from `date` (fallback `mentioned_at`), `age_score = min(1, age/365)`,
`volatile_score` (0..1, additive weights for IPv4, versions, commit hashes, time-relative
words EN/FR, LAN-host URLs) with `volatile_hits` labels, `staleness = (age+volatile)/2`.
Both numbers are returned; the UI sorts by either. Observations are `curatable:false`.
UI: page `/memory-review`, bank tabs, sort select, multi-select (check 1..10), per-row
Revoke/Edit/Restore with a confirm dialog each, an Undo toast built from `previous`,
"Check with Hermes" button, verdict badge per fact; nothing is ever auto-revoked.

## 5. Risks / open questions

1. Invariant 5 carve-out in AGENTS.md: ok? 2. `defaultEnabled:false` makes the existing
injection pane opt-in too (it is already opt-in when installed as an agent package, per the
loader). Splitting into two plugins needs a second folder under `desktop-plugins/`. 3.
Verdict read-back is unverified. 4. Banks hold 45k facts; the list is newest-first, so the
oldest facts need `offset` paging or `end_date`; the page scans a bounded window per load.
