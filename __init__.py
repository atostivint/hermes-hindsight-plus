"""Hermes Hindsight Plus plugin entry point.

Injects one or more Hindsight mental models into the first turn of every
conversation so the agent starts with stable, cron-refreshed context instead of
hand-maintained MEMORY.md / USER.md files.

See README.md for configuration and behaviour.
"""

from __future__ import annotations

import functools
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


def _get_config(ctx):
    merged = dict(DEFAULTS)
    try:
        raw = getattr(ctx, "config", None)
    except Exception:
        raw = None

    if isinstance(raw, dict):
        for key in merged:
            if key in raw and raw[key] is not None:
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
    cfg = state.config

    # Cheap off switches: no I/O at all.
    if cfg.get("mode") == "off":
        return None
    if not cfg.get("mental_model_ids"):
        return None
    if cfg.get("mode") == "first_turn" and not is_first_turn:
        return None
    if platform in (cfg.get("skip_platforms") or []):
        return None

    # Per-session guard: a retried first turn must not double-inject.
    if is_first_turn and session_id in state.guard:
        return None

    text = core.build_injection(cfg, _logger)
    if not text:
        return None

    if is_first_turn:
        state.guard.add(session_id)
    return {"context": text}


def register(ctx):
    state = _State(_get_config(ctx))
    ctx.register_hook("pre_llm_call", functools.partial(_hook, state))
