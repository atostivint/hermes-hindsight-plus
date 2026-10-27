"""Hermes Hindsight Plus plugin entry point.

Injects one or more Hindsight mental models into the first turn of every
conversation so the agent starts with stable, cron-refreshed context instead of
hand-maintained MEMORY.md / USER.md files.

See README.md for configuration and behaviour.
"""

from __future__ import annotations

import logging
import threading

try:
    from . import core
except ImportError:  # pragma: no cover - flat-module loading (dir name has a hyphen)
    import core as core

DEFAULTS = {
    "mode": "first_turn",
    "mental_model_ids": ["operator-context"],
    "max_chars": 6000,
    "cache_ttl_s": 21600,
    "timeout_s": 3.0,
    "skip_platforms": ["cron"],
    # Optional static context for this deployment (empty by default). Users can
    # put instance/profile/architecture facts here without forking the plugin.
    "context_note": "",
    # Create-if-missing: only a 404 triggers a POST. Existing models are never
    # patched, so users keep full control of their names, queries, and schedules.
    "create_missing": True,
    "create_name": core.DEFAULT_CREATE_NAME,
    "create_source_query": core.DEFAULT_CREATE_SOURCE_QUERY,
    "create_refresh_cron": core.DEFAULT_CREATE_REFRESH_CRON,
    "create_max_tokens": core.DEFAULT_CREATE_MAX_TOKENS,
    "create_mode": core.DEFAULT_CREATE_MODE,
}

_logger = logging.getLogger("hermes_hindsight_plus")


class _SessionGuard:
    """Bounded, thread-safe set of session ids with FIFO eviction.

    Prevents a retried first turn from double-injecting context.
    """

    def __init__(self, max_size=200):
        self._max_size = max_size
        self._lock = threading.Lock()
        self._seen = {}

    def __contains__(self, session_id):
        if session_id is None:
            return False
        with self._lock:
            return session_id in self._seen

    def add(self, session_id):
        if session_id is None:
            return
        with self._lock:
            self._seen.pop(session_id, None)  # refresh insertion order on re-add
            self._seen[session_id] = True
            while len(self._seen) > self._max_size:
                oldest = next(iter(self._seen))
                del self._seen[oldest]


class _State:
    def __init__(self, config):
        self.config = config
        self.guard = _SessionGuard()


def _normalize_mode(value):
    """Return ``first_turn``, ``every_turn`` or ``off``.

    Hermes parses YAML 1.1, so an unquoted ``mode: off`` in ``config.yaml``
    arrives as the boolean ``False`` (and ``mode: on`` as ``True``). Normalize
    those, plus the obvious aliases, before the hot-path checks.
    """
    if isinstance(value, bool):
        return "off" if value is False else "first_turn"
    text = str(value).strip().lower() if value is not None else ""
    if text in {"off", "false", "0", "no", "none", "disabled"}:
        return "off"
    if text in {"every_turn", "every-turn", "always", "all"}:
        return "every_turn"
    return "first_turn"


def _get_config(ctx):
    """Read this plugin's settings, preferring Hermes' ``ctx.get_config`` API.

    ``ctx.get_config(key, default)`` reads ``plugins.entries.<id>.settings.<key>``
    in the active profile's ``config.yaml``. Older/foreign contexts that only
    expose a ``config`` mapping still work.
    """
    merged = dict(DEFAULTS)

    getter = getattr(ctx, "get_config", None)
    if callable(getter):
        for key, default in DEFAULTS.items():
            try:
                value = getter(key, default=default)
            except TypeError:  # very old context without the keyword form
                try:
                    value = getter(key, default)
                except Exception:
                    continue
            except Exception:
                continue
            if value is not None:
                merged[key] = value
        return merged

    try:
        raw = getattr(ctx, "config", None)
    except Exception:
        raw = None

    if isinstance(raw, dict):
        for key in merged:
            if raw.get(key) is not None:
                merged[key] = raw[key]
    elif raw is not None and hasattr(raw, "get"):
        for key in merged:
            try:
                value = raw.get(key)
            except Exception:
                continue
            if value is not None:
                merged[key] = value
    return merged


def _hook(state, session_id=None, user_message=None, conversation_history=None,
          is_first_turn=False, model=None, platform=None, **kwargs):
    try:
        cfg = state.config
        mode = _normalize_mode(cfg.get("mode"))

        # Cheap off switches: no I/O at all.
        if mode == "off":
            return None
        if not core._as_list(cfg.get("mental_model_ids")):
            return None
        if mode == "first_turn" and not is_first_turn:
            return None
        if platform in core._as_list(cfg.get("skip_platforms")):
            return None

        # Per-session guard: a retried first turn must not double-inject.
        if is_first_turn and session_id in state.guard:
            return None

        text = core.build_injection(cfg, _logger)
        if not text:
            return None

        # Metadata only: never log the injected content or the API key.
        _logger.info(
            "hindsight-plus: injected %d chars (first_turn=%s, platform=%s, session=%s)",
            len(text),
            is_first_turn,
            platform or "",
            session_id or "",
        )

        if is_first_turn:
            state.guard.add(session_id)
        return {"context": text}
    except Exception as exc:  # never raise into the conversation
        _logger.warning("hindsight-plus: pre_llm_call failed: %s", exc)
        return None


def register(ctx):
    state = _State(_get_config(ctx))

    def hook(**kwargs):
        return _hook(state, **kwargs)

    # A ``**kwargs`` callback opts into additive hook payload fields and keeps
    # ``hermes plugins doctor`` happy.
    ctx.register_hook("pre_llm_call", hook)
