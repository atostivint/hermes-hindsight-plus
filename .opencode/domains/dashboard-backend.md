# Domain: dashboard-backend

## Owns
- `dashboard/plugin_api.py` - the FastAPI router mounted at
  `/api/plugins/hermes-hindsight-plus/`. Read routes: `/health`, `/history`, `/injections`,
  `/sessions`. Opt-in Memory review routes: `GET /review/{bank}/memories`,
  `POST /review/{bank}/memories/{id}/revoke`, `/restore`, `/update`,
  `POST /review/{bank}/memories`, `POST /review/check-prompt`, `POST /review/verdicts`,
  `POST /review/{bank}/apply`.
- `review.py` - review logic: scoring (`volatile_score`, `age_score`, `score_item`, `sort_items`),
  validation (`check_bank`, `check_memory_id`, `check_reason`, `check_text`), `ReviewClient`,
  `list_review`, `revoke`, `restore`, `update`, `create_memory`, `apply_document`,
  `build_check_prompt`, `parse_verdicts`.
- `tests/test_review.py` (42 tests) and `tests/test_review_document.py` (3 tests).

## Does not own
- `core.py` and `__init__.py` -> plugin-core. This module **loads** `core.py` by path
  (`hermes_hindsight_plus_dashboard_core`) to reuse `resolve_endpoint`, `_hermes_home` and the audit
  readers; it never edits it.
- `desktop/plugin.js` -> desktop-ui (that is the caller of these routes).
- `dashboard/manifest.json` -> desktop-ui (surface declaration).
- `plugin.yaml` -> plugin-core.

## Invariants
- **Python standard library only** in `review.py`. `plugin_api.py` may import `fastapi` (provided by
  the host) and must reuse `core.py` instead of duplicating endpoint/auth code.
- The Hindsight API key stays server-side: never sent to the browser, never written, printed or
  logged.
- Banks are limited to `hermes`, `atlas`, `astro` (`BANKS`); anything else is a `404`
  (`check_bank` raises `ReviewError(404, ...)`; an earlier version of this note said 400 and was
  wrong - measured 2026-11-11).
- **No route calls `DELETE`.** The only writes are a soft `PATCH` (`state=invalidated` + reason, or
  `state=valid`, or a text rewrite on a memory fact) and one manual `POST .../memories` on an
  explicit click. Never on mental models, never automatic, never in batch.
- Every bound is server-side: `MAX_PAGE` 200, `MAX_TEXT_CHARS` 4000, `MAX_REASON_CHARS` 500,
  `MAX_MEMORY_CONTENT` 2000, `MAX_MEMORY_CONTEXT` 500, `MAX_MEMORY_TAGS` 5, `MAX_TAG_LENGTH` 40,
  `MAX_CHECK_FACTS` 10. Never trust a limit sent by the page.
- `ReviewError` maps to an HTTP status through `_review_call`; anything else becomes a generic 500
  with no internal detail.
- No route accepts a filesystem path: the inputs are a bank id, a memory id, bounded text and paging
  integers.
- An agent verdict is advice, not an action: the page shows a badge, the user decides.

## Known traps
- Reloading: `_core()` and `_review()` cache their module and are guarded by `_core_lock`; keep the
  double-checked load, it survives a host reload.
- `tests/test_plugin.py` ships a fake `fastapi` module (with `Body` and `post`). A new route that
  imports something else from `fastapi` breaks that stub: extend it in the same change.
- The stub HTTP servers in `tests/test_review.py` bypass `HTTPServer.server_bind`'s reverse-DNS
  lookup; keep that or the tests stall ~31 s on Windows.
- Real `PATCH`/`POST` on the live bank is never exercised in a proof; all write tests run against the
  stub. Say it in `gaps`.
- Unknown query values: `sort` must stay within `("age", "volatile", "staleness")` and `state` within
  `("valid", "invalidated")`.

## Proof
- `python -m pytest -q tests/test_review.py tests/test_review_document.py`
- `python -m pytest -q` (whole suite, ~9 s)
- `python -m compileall .`
- `ruff check dashboard/plugin_api.py review.py`
- named tests for any new route or rule
- explicit gap: "no real bank write; stub only"

## Open threads
- The "Check with Hermes" flow starts a normal chat session; its verdict read-back was never
  confirmed against a real Desktop run.
- HH-10/11 (2026-11-11, uncommitted): the list route now scans the whole bank (`MAX_SCAN`), ranks
  globally, filters with `review_only`, and hides facts a local ledger marks "still true"
  (`review-decisions.json` in the plugin data dir, `OK_TTL_DAYS = 90`, `MAX_DECISIONS = 5000`).
  `to_review` is the queue size after the ledger; `matched` is the post-filter count, not a `q`
  match count. `offset` is a best-effort cursor, not a snapshot.
- No real bank has ever been scanned: the 10-call window, its latency and the `truncated` boundary
  are proven against stubs only.
- The ledger lock is intra-process (`threading.Lock`); two Hermes processes writing the same file
  can lose one write.
