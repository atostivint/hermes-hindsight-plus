"""Hermes Hindsight Plus plugin entry point.

Injects one or more Hindsight mental models into the first turn of every
conversation so the agent starts with stable, cron-refreshed context instead of
hand-maintained MEMORY.md / USER.md files.

See README.md for configuration and behaviour.
"""

from __future__ import annotations

import logging
import os
import threading
import time

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
    # Local injection audit for the Desktop memory pane. Content is stored in
    # plain JSON under the plugin data directory; disabling it keeps metadata
    # only (timestamps, counts, model names) and stores no prompt/injection text.
    "record_content": True,
    "audit_max_entries": core.DEFAULT_AUDIT_MAX_TOTAL,
    "audit_max_per_session": core.DEFAULT_AUDIT_MAX_PER_SESSION,
    "audit_max_sessions": core.DEFAULT_AUDIT_MAX_SESSIONS,
    # Create-if-missing: only a 404 triggers a POST. Existing models are never
    # patched, so users keep full control of their names, queries, and schedules.
    "create_missing": True,
    "create_name": core.DEFAULT_CREATE_NAME,
    "create_source_query": core.DEFAULT_CREATE_SOURCE_QUERY,
    "create_refresh_cron": core.DEFAULT_CREATE_REFRESH_CRON,
    "create_max_tokens": core.DEFAULT_CREATE_MAX_TOKENS,
    "create_mode": core.DEFAULT_CREATE_MODE,
    # Optional second, read-only bank for the turn's repository. Empty template
    # disables the feature; `{project}` is the git repository name.
    "project_bank_template": "",
    "project_mental_model_ids": [],
    # Static project bank, used instead of the template. The only route desktop
    # sessions can use: Hermes' pre_llm_call payload carries no cwd.
    "project_bank_id": "",
    # Tolerant alternative to the template: match the known project name
    # against the instance's bank list instead of formatting an id.
    "project_bank_lookup": False,
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


def _normal_path(value):
    """Return a case-normalized, resolved path string, or ``""``."""
    try:
        text = os.path.realpath(os.fspath(value))
    except (OSError, TypeError, ValueError):
        return ""
    return os.path.normcase(text) if text else ""


def _blocked_roots():
    """Directories that are the plugin host's, never a project to route."""
    roots = []
    for value in (core._hermes_home(), core._plugin_data_dir()):
        roots.append(value)
    roots.append(os.path.dirname(os.path.abspath(__file__)))
    return roots


def _is_blocked_path(value):
    """True when ``value`` is the Hermes home, the plugin dir or its data dir.

    The gateway's own directory is the known trap: on desktop a chat carries no
    repository, and the daemon's cwd points at the host installation.
    """
    candidate = _normal_path(value)
    if not candidate:
        return True
    return any(candidate == _normal_path(root) for root in _blocked_roots())


def _project_name(value):
    """Return a repository name from a path or a bare repository name.

    A path is resolved through its ``.git`` entry so a worktree maps to the same
    bank as its main checkout. A value that does not resolve is rejected when it
    looks like a path, rather than being silently basenamed.
    """
    text = value.strip() if isinstance(value, str) else ""
    if not text or text.startswith("\\\\") or text.startswith("//"):
        return ""
    if any(sep in text for sep in ("/", "\\")):
        if _is_blocked_path(text):
            return ""
        return core.repo_name(text)
    if text in {".", ".."}:
        return ""
    return text


def _payload_name(payload):
    """First payload alias that resolves to a project, or ``""``."""
    for key in ("cwd", "workspace", "workspace_path", "working_directory"):
        value = payload.get(key)
        if not isinstance(value, str) or not value.strip():
            continue
        name = _project_name(value)
        if name:
            return name
    return ""


def _lookup_bank(cfg, name, hermes_home=None, deadline=None):
    """Resolve ``name`` against the instance's bank list, or return ``""``.

    Read-only and best-effort: the list is cached on the same terms as the
    project model ids, and any failure disables the project section instead of
    affecting the turn.
    """
    try:
        api_url, _bank, api_key = core.resolve_endpoint(
            hermes_home or core._hermes_home()
        )
        if not api_url or not name:
            return ""
        client = core.HindsightClient(
            api_url,
            "",
            api_key,
            core._as_float(cfg.get("timeout_s"), 3.0),
            deadline=deadline,
        )
        banks = core.list_banks(
            client,
            cache_path=core.bank_list_cache_path(api_url, hermes_home),
            ttl=core._as_int(cfg.get("cache_ttl_s"), 21600),
            logger=_logger,
        )
        return core.match_project_bank(banks, name)
    except Exception:  # noqa: BLE001 - the lookup is optional, never fatal
        return ""


def _project_from_payload(
    cfg, payload, session_id=None, hermes_home=None, deadline=None
):
    """Return ``{"name", "bank"}`` for the turn's repository, or ``None``.

    Hermes' ``pre_llm_call`` payload carries no working directory, so the path
    comes from, in order: the tolerated payload aliases (other transports may
    fill them), ``HINDSIGHT_PROJECT`` (a path or a repo name), and finally the
    ``cwd`` Hermes stored for this session in its read-only ``state.db``. The
    daemon's own directory is never used. A static ``project_bank_id`` needs no
    path at all, which is what makes it work from a desktop chat where Hermes
    sends neither a project nor a working directory. With
    ``project_bank_lookup`` on, the known name is matched against the instance's
    bank list, and the template is only the fallback.
    """
    try:
        static = cfg.get("project_bank_id")
        template = cfg.get("project_bank_template")
        lookup = core._as_bool(cfg.get("project_bank_lookup"), False)
        static = static.strip() if isinstance(static, str) else ""
        template = template.strip() if isinstance(template, str) else ""
        if not static and not template and not lookup:
            return None

        name = _payload_name(payload)
        if not name:
            name = _project_name(os.environ.get("HINDSIGHT_PROJECT", ""))
        if not name:
            name = _project_name(
                core.read_session_cwd(
                    session_id or payload.get("session_id"), hermes_home
                )
            )

        if static:
            return {"name": name or static, "bank": static}
        if lookup and name:
            bank = _lookup_bank(cfg, name, hermes_home, deadline)
            if bank:
                return {"name": name, "bank": bank}
        bank = core.project_bank_id(template, name)
        return {"name": name, "bank": bank} if bank else None
    except Exception:  # noqa: BLE001 - project routing is optional, never fatal
        return None


def _broadcast_injection_recorded(summary):
    """Tell an attached Desktop to refresh its memory pane.

    The event carries audit metadata only: message content stays on disk and is
    fetched through the authenticated dashboard REST route. Broadcasting is
    best-effort and must never affect the turn.
    """
    try:
        from hermes_cli.plugin_events import broadcast_plugin_event

        broadcast_plugin_event(
            core.PLUGIN_ID,
            "injection.recorded",
            {
                "entry_id": summary.get("entry_id", ""),
                "session_id": summary.get("session_id", ""),
                "task_id": summary.get("task_id", ""),
                "session_key": summary.get("session_key", ""),
                "recorded_at": summary.get("recorded_at", ""),
                "entry_count": summary.get("entry_count", 0),
            },
        )
    except Exception as exc:
        _logger.debug("hindsight-plus: could not broadcast injection event: %s", exc)


def _hook(state, session_id=None, task_id=None, turn_id=None, user_message=None,
          conversation_history=None, is_first_turn=False, model=None,
          platform=None, **kwargs):
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

        # One wall-clock budget for the whole attempt, including a bank lookup.
        deadline = time.monotonic() + max(
            0.0, core._as_float(cfg.get("timeout_s"), 3.0)
        )

        details = core.build_injection_details(
            cfg,
            _logger,
            project=_project_from_payload(
                cfg, kwargs, session_id=session_id, deadline=deadline
            ),
            deadline=deadline,
        )
        text = (details or {}).get("text") or ""
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

        try:
            summary = core.record_injection(
                details,
                cfg,
                session_id=session_id,
                task_id=task_id,
                turn_id=turn_id,
                user_message=user_message,
                is_first_turn=is_first_turn,
                platform=platform,
                model=model,
            )
            if summary:
                _broadcast_injection_recorded(summary)
        except Exception as exc:  # auditing is fail-open, never lose the context
            _logger.warning("hindsight-plus: injection audit failed: %s", exc)

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
