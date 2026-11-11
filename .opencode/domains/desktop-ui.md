# Domain: desktop-ui

## Owns
- `desktop/plugin.js` - the whole Desktop surface in one plain-ESM file: the right-edge **Memory**
  pane (injection history) and the opt-in **Memory review** page, plus the SDK registrations.
- `dashboard/manifest.json` - the surface declaration (`label`, `icon`, `tab`, `api`).
- `assets/icon.png` - plugin artwork. **Untracked on 2026-11-11: it belongs to the user. Do not
  delete, move or commit it without asking.**

## Does not own
- `dashboard/plugin_api.py`, `review.py` -> dashboard-backend (the routes this page calls).
- `__init__.py`, `core.py` -> plugin-core (the audit and the gateway event it consumes).
- `plugin.yaml` version/description -> packaging-docs (a `defaultEnabled` change is still declared in
  the SDK registration here).

## Invariants
- The plugin loader imports `desktop/plugin.js` as a **blob**: relative imports cannot resolve and
  the page cannot live in its own file. Keep one file with clearly delimited sections
  (`// ==== Memory review (opt-in page) ====` ... `// ==== end Memory review`).
- Imports are limited to `@hermes/plugin-sdk`, `react` and `react/jsx-runtime`. A new import needs
  the user's approval and `hermes plugins validate .` to pass the "desktop surface" check.
- `jsx()` calls only, **no JSX syntax** - the file is hand-run through `node --check`, there is no
  build step.
- Only theme variables for colours, **never a hardcoded colour**.
- Every registration call carries `id`, `render` and `geometry` inside `data`. The page registers
  via `ROUTES_AREA` plus a `SIDEBAR_NAV_AREA` row; the pane via `PANES_AREA`.
- `defaultEnabled: false` keeps the surface opt-in. Known trade-off: one toggle per plugin, so it
  also switches off the injection pane.
- The live refresh comes from the metadata-only gateway event (`plugin.<id>.injection.recorded`):
  content is fetched through the authenticated dashboard route, never pushed in the event.
- Message text shown in the page comes from the local audit; the page never talks to Hindsight
  directly and never sees the API key.
- Starting a chat from the review page: `session.create` -> `prompt.submit` -> `host.openSession`,
  and on failure fall back to `host.newChat()` + `host.composer.setDraft('new', prompt)`.
- French UI copy is acceptable here; the injected agent text stays English (plugin-core).

## Known traps
- There is no JS test harness: `node --check` proves syntax only. Any rendering, layout, selection or
  dialog behaviour is a **gap** until the user looks at it in Hermes Desktop.
- The review header counts are PAGE-scoped: the list is fetched with `limit=100&offset=…`, so the
  numerator is the page size and the "à revoir" count is computed on the displayed set only. The
  server's real total (`query.data.total`, `review.py:266`) is only in the footer. A label that
  compares the two without saying so reads as nonsense ("100 souvenirs sur 21 a revoir").
  Fixed once on 2026-11-11 (`100 / 245 souvenirs · 21 à revoir sur cette page`); beyond page 1 the
  numerator still does not reflect the offset.
- The review page's tags are a closed list (`ALLOWED_TAGS`), and the theme list is `THEME_KEYS`;
  a new tag needs the backend's `_TAG_RE` and the page to agree.
- `reviewCall` / `reviewPost` build paths under `/api/plugins/<id>/review/...`; a route rename breaks
  the page silently.
- Never re-enable a page by editing the SDK registration without saying it changes what the user
  sees on their next launch.

## Proof
- `node --check desktop/plugin.js`
- `hermes plugins validate .` (desktop surface + registration probe)
- `python -m pytest -q` when the page's expected route payload changed
- a written list of what was not verified in a real Desktop window (always non-empty unless the user
  confirms)

## Open threads
- No live Desktop run of the Memory review page has ever been recorded; it is syntax-checked only.
- HH-6 fixed the header label (2026-11-11); the "à revoir" metric is still page-scoped by design, and
  the numerator on page 2+ still shows the page size rather than the offset window.
- HH-7 open: the user says information is missing from the review page but has not named which.
- HH-8 open: the injection pane is `defaultCollapsed: true`; the user could not find it.
