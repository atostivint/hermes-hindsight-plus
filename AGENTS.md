# AGENTS.md — operating brief

Hermes Agent plugin (`hermes-hindsight-plus`) that injects Hindsight mental models into
the first turn of every conversation, replacing hand-maintained `MEMORY.md` / `USER.md`.

## Hard constraints (do not violate)

- **Stdlib only.** `json`, `urllib.request`, `pathlib`, `threading`, `time`, `logging`, `os`.
  No third-party runtime dependency. Target Python 3.11+.
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

- `__init__.py` — entry point (`register`), config resolution, the hook, the per-session
  guard. Loaded as a package; keep `from . import core` (the directory name has a hyphen,
  so the module is not importable by its on-disk name).
- `core.py` — endpoint resolution, HTTP transport, cache read/write, truncation, header,
  and `build_injection`.
- `tests/` — pytest suite. `conftest.py` loads the package under a valid module name.

## Commands

```bash
python -m compileall .
python -m pytest -q
```

## Repo rules

- Work only inside this repo. No remote, never push, never create a GitHub repo.
- Commit to `main` with a clear message.
