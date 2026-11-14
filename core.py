"""Core logic for the Hermes Hindsight Plus plugin.

Fetches Hindsight mental models over HTTP, caches them on disk, and assembles
them into a context block injected into the current turn's user message.

Stdlib only, no third-party runtime dependency. Target Python 3.11+.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import pathlib
import re
import sqlite3
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

_LOG = logging.getLogger("hermes_hindsight_plus")

PLUGIN_ID = "hermes-hindsight-plus"

# Hard cap imposed by Hermes on a single pre_llm_call context. We stay well
# under it by default; this is a defensive ceiling in case a user configures a
# very large max_chars.
HERMES_CALLBACK_CAP = 10_000

# Defaults used only when a configured mental model is absent and the user has
# enabled create-if-missing. Existing models are never modified.
DEFAULT_CREATE_NAME = "Stable operating context"
DEFAULT_CREATE_SOURCE_QUERY = (
    "Build a stable operating context for this assistant from the bank's durable "
    "memories. Cover the user's identity and preferences, environment and "
    "infrastructure facts, active projects and commitments, standing "
    "instructions, recurring workflows, and the assistant's deployment topology "
    "(instance, profiles, and memory-bank mapping) when known. Exclude transient "
    "details, secrets, and anything likely to change soon. Return concise "
    "markdown with short sections."
)
DEFAULT_CREATE_REFRESH_CRON = "0 3 * * *"  # daily at 03:00 UTC
DEFAULT_CREATE_MAX_TOKENS = 2048
DEFAULT_CREATE_MODE = "full"

# Local injection audit used by the Desktop memory pane. The file is plain
# JSON under the plugin data directory; it never contains the API key. Keep
# the bounds conservative so an every-turn deployment cannot grow without
# limit.
AUDIT_FILENAME = "injection-audit.json"
AUDIT_SCHEMA = 1
AUDIT_CONTENT_CAP = HERMES_CALLBACK_CAP
AUDIT_PREVIEW_CHARS = 180
DEFAULT_AUDIT_MAX_TOTAL = 200
DEFAULT_AUDIT_MAX_PER_SESSION = 20
DEFAULT_AUDIT_MAX_SESSIONS = 50
_AUDIT_LOCK = threading.RLock()

# Hindsight IDs are lowercase alphanumeric with hyphens.
_MODEL_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")


def _hermes_home() -> pathlib.Path:
    # Hermes' canonical resolver follows the active profile; fall back to the
    # environment and finally to this plugin's install path when imported
    # outside Hermes (tests, ad-hoc use).
    try:
        from hermes_constants import get_hermes_home

        return pathlib.Path(get_hermes_home())
    except Exception:
        pass
    env = os.environ.get("HERMES_HOME")
    if env:
        return pathlib.Path(env)
    # Plugin lives at $HERMES_HOME/plugins/<id>/core.py -> parents[2] is the home.
    return pathlib.Path(__file__).resolve().parents[2]


def _plugin_data_dir(hermes_home=None) -> pathlib.Path:
    """Return the plugin-owned data directory (``<home>/plugin-data/<id>/``).

    Uses Hermes' ``plugin_data_dir`` when available so the active profile and
    its collision-resistant namespace are honoured; otherwise falls back to the
    documented path.
    """
    if hermes_home is not None:
        return pathlib.Path(hermes_home) / "plugin-data" / PLUGIN_ID
    try:
        from plugins.plugin_storage import plugin_data_dir as _pdd
    except Exception:
        _pdd = None
    if _pdd is not None:
        try:
            return pathlib.Path(_pdd(PLUGIN_ID))
        except Exception:
            pass
    return _hermes_home() / "plugin-data" / PLUGIN_ID


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_iso(value):
    if not value:
        return None
    if not isinstance(value, str):
        value = str(value)
    try:
        text = value.replace("Z", "+00:00")
        dt = datetime.fromisoformat(text)
    except (ValueError, TypeError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _format_date(value) -> str:
    dt = _parse_iso(value)
    if dt is None:
        return "unknown"
    return dt.strftime("%Y-%m-%d")


def _stale_days(value):
    dt = _parse_iso(value)
    if dt is None:
        return None
    return max(0, (datetime.now(timezone.utc) - dt).days)


def _cache_age_s(record) -> int:
    dt = _parse_iso(record.get("fetched_at"))
    if dt is None:
        return 0
    return int((datetime.now(timezone.utc) - dt).total_seconds())


def _human_age(seconds) -> str:
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds} s"
    if seconds < 3600:
        return f"{seconds // 60} min"
    if seconds < 86400:
        return f"{seconds // 3600} h"
    return f"{seconds // 86400} d"


def _safe_name(value: str) -> str:
    value = str(value)
    cleaned = "".join(ch if (ch.isalnum() or ch in "._-") else "_" for ch in value)
    return cleaned or "_"


def _as_list(value):
    """Coerce a config value to a list of non-empty strings."""
    if value is None:
        return []
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return []
        if text.startswith("["):
            try:
                parsed = json.loads(text)
            except ValueError:
                parsed = None
            if isinstance(parsed, list):
                return [str(item).strip() for item in parsed if str(item).strip()]
        return [part.strip() for part in text.split(",") if part.strip()]
    if isinstance(value, (list, tuple, set)):
        return [str(item).strip() for item in value if str(item).strip()]
    text = str(value).strip()
    return [text] if text else []


def _as_int(value, default):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _as_float(value, default):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _as_bool(value, default):
    """Coerce a config value to bool, tolerating YAML 1.1 spellings."""
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "on", "enabled"}:
        return True
    if text in {"0", "false", "no", "off", "disabled"}:
        return False
    return default


def _json_scalar(value):
    """Return a JSON-friendly scalar for audit metadata."""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def _clean_id(value) -> str:
    text = str(value or "").strip()
    return text[:256]


def _http_get_json(url, timeout_s, api_key):
    req = urllib.request.Request(url, method="GET")
    req.add_header("Accept", "application/json")
    if api_key:
        req.add_header("Authorization", "Bearer " + api_key)
    with urllib.request.urlopen(req, timeout=timeout_s) as resp:
        body = resp.read()
    return json.loads(body.decode("utf-8"))


def _http_post_json(url, timeout_s, api_key, payload):
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Accept", "application/json")
    req.add_header("Content-Type", "application/json")
    if api_key:
        req.add_header("Authorization", "Bearer " + api_key)
    with urllib.request.urlopen(req, timeout=timeout_s) as resp:
        body = resp.read()
    if not body:
        return {}
    try:
        return json.loads(body.decode("utf-8"))
    except ValueError:
        return {}


def resolve_endpoint(hermes_home):
    api_url = os.environ.get("HINDSIGHT_API_URL")
    bank_id = os.environ.get("HINDSIGHT_BANK_ID")
    api_key = os.environ.get("HINDSIGHT_API_KEY")

    cfg_path = pathlib.Path(hermes_home) / "hindsight" / "config.json"
    cfg = {}
    if cfg_path.is_file():
        try:
            loaded = json.loads(cfg_path.read_text("utf-8"))
            if isinstance(loaded, dict):
                cfg = loaded
        except (OSError, ValueError) as exc:
            _LOG.warning("hindsight-plus: could not read %s: %s", cfg_path, exc)

    if not api_url:
        api_url = cfg.get("api_url") or ""
    if not bank_id:
        bank_id = cfg.get("bank_id") or ""
    if not api_key:
        api_key = cfg.get("api_key") or ""

    return (api_url or ""), (bank_id or ""), (api_key or "")


# Upper bound on the models pulled from a project bank when the user did not
# name any. Discovery cannot be bounded by the config the way a configured id
# list is, and the audit keeps at most 16 model metadata entries anyway;
# ``max_chars`` still truncates the assembled text.
PROJECT_MODEL_LIMIT = 16

# A bank id travels in a URL path and names storage on the server, so accept
# only a conservative shape: 2-128 chars, no ``..`` traversal.
_BANK_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{1,127}$")
# A misconfigured template must not warn on every turn; log each distinct bad
# bank id once (bounded, so an exotic caller cannot grow this without limit).
_LOGGED_BAD_BANKS = set()
_LOGGED_BAD_BANKS_MAX = 64

# ``sessions.cwd`` lookups are cached per session id in memory: one read-only
# query per session, bounded, never raising.
_SESSION_CWD_CACHE = {}
_SESSION_CWD_LOCK = threading.Lock()
SESSION_CWD_CACHE_MAX = 200


def _valid_bank_id(value):
    """True when ``value`` is safe to put in a Hindsight bank URL."""
    if not isinstance(value, str) or ".." in value:
        return False
    return bool(_BANK_ID_RE.match(value))


def _log_bad_bank(logger, bank):
    key = str(bank)[:128]
    if key not in _LOGGED_BAD_BANKS and len(_LOGGED_BAD_BANKS) < _LOGGED_BAD_BANKS_MAX:
        _LOGGED_BAD_BANKS.add(key)
        logger.warning(
            "hindsight-plus: refusing project bank id %r: use 2-128 chars of "
            "[A-Za-z0-9._:-] starting alphanumeric, without '..'",
            key,
        )


def _bank_cache_hash(bank):
    """Short digest that keeps two project banks from sharing one cache file.

    ``_safe_name`` maps ``:`` and spaces to ``_``, so ``coding-agent::a`` and
    ``coding-agent a`` would otherwise collide on disk. The configured bank
    keeps its unhashed filename: it has no sibling, and changing it would throw
    away every warm cache for no benefit.
    """
    return hashlib.sha1(str(bank).encode("utf-8")).hexdigest()[:8]


def read_session_cwd(session_id, hermes_home=None):
    """Return the ``cwd`` Hermes stored for ``session_id``, or ``""``.

    Hermes records it in the ``sessions`` table of its read-only ``state.db``;
    desktop chats usually leave it empty and only some platforms fill it. One
    query per session id (cached in memory, bounded), a 2 s timeout, and it
    never raises. Never writes to the database.
    """
    session_id = _clean_id(session_id)
    if not session_id:
        return ""
    with _SESSION_CWD_LOCK:
        cached = _SESSION_CWD_CACHE.get(session_id)
    if cached is not None:
        return cached

    home = pathlib.Path(hermes_home) if hermes_home else _hermes_home()
    db_path = home / "state.db"
    value = ""
    conn = None
    try:
        if db_path.is_file():
            conn = sqlite3.connect(
                db_path.resolve().as_uri() + "?mode=ro", uri=True, timeout=2.0
            )
            try:
                conn.execute("PRAGMA query_only = ON")
            except sqlite3.Error:
                pass
            for column in ("session_id", "id"):
                try:
                    row = conn.execute(
                        f"SELECT cwd FROM sessions WHERE {column} = ? LIMIT 1",
                        (session_id,),
                    ).fetchone()
                except sqlite3.OperationalError:
                    continue
                if row and isinstance(row[0], str):
                    value = row[0].strip()
                break
    except (sqlite3.Error, OSError, ValueError):
        value = ""
    finally:
        if conn is not None:
            try:
                conn.close()
            except sqlite3.Error:
                pass

    with _SESSION_CWD_LOCK:
        if len(_SESSION_CWD_CACHE) >= SESSION_CWD_CACHE_MAX:
            _SESSION_CWD_CACHE.clear()
        _SESSION_CWD_CACHE[session_id] = value
    return value


def _worktree_repo_name(marker):
    """Return the repository name from a worktree's ``.git`` file, or ``""``.

    A linked checkout stores ``gitdir: <repo>/.git/worktrees/<name>``; the
    component before ``/.git/`` is the repository, so every worktree of a
    repository maps to the same bank.
    """
    try:
        text = marker.read_text("utf-8", errors="replace")
    except (OSError, ValueError):
        return ""
    for line in text.splitlines():
        head, sep, tail = line.strip().partition(":")
        if not sep or head.strip().lower() != "gitdir":
            continue
        target = tail.strip()
        if not target:
            return ""
        if not os.path.isabs(target):
            target = os.path.join(str(marker.parent), target)
        normalized = os.path.normpath(target).replace("\\", "/")
        cut = normalized.rfind("/.git/")
        if cut != -1:
            return normalized[:cut].rstrip("/").rsplit("/", 1)[-1]
        # A relative ``gitdir`` without a ``worktrees`` segment, or a bare
        # repository (``<name>.git``): the last component names the repo.
        tail_name = normalized.rstrip("/").rsplit("/", 1)[-1]
        if tail_name.endswith(".git"):
            tail_name = tail_name[: -len(".git")]
        return tail_name
    return ""


def repo_name(path):
    """Return the git repository name containing ``path``, or ``""``.

    Walks up looking for a ``.git`` entry, without spawning ``git``: a directory
    is the repository root, a file is a worktree (see ``_worktree_repo_name``).
    The path is resolved first so a junction names the real repository, not the
    link. Read-only, stdlib only, and it never raises.
    """
    try:
        current = pathlib.Path(path).resolve(strict=False)
        if current.is_file():
            current = current.parent
        for candidate in (current, *current.parents):
            marker = candidate / ".git"
            if marker.is_dir():
                return candidate.name or ""
            if marker.is_file():
                name = _worktree_repo_name(marker)
                if name:
                    return name
                # Unparseable ``.git`` file: keep walking up, another ancestor
                # may still be the repository root.
                continue
    except (OSError, RuntimeError, TypeError, ValueError):
        return ""
    return ""


def project_bank_id(template, project):
    """Return the bank id for ``project``, or ``""`` when either side is empty.

    Only ``{project}`` is substituted. Any other placeholder (``{profile}``) is
    left literal: this plugin has no notion of it, and inventing one would
    silently target a bank nobody configured.
    """
    if not isinstance(template, str) or not isinstance(project, str):
        return ""
    template = template.strip()
    project = project.strip()
    if not template or not project:
        return ""
    return template.replace("{project}", project)


def _model_ids_from_list(payload, limit=PROJECT_MODEL_LIMIT):
    """Return valid model ids from a mental-models collection response.

    The API wraps collections differently across versions, so accept a bare list
    and the known envelopes; anything else yields no ids. Ids are filtered
    through the same shape the plugin requires of a created model.
    """
    items = payload
    if isinstance(items, dict):
        for key in ("items", "mental_models", "models", "results"):
            if isinstance(items.get(key), list):
                items = items[key]
                break
    if isinstance(items, dict):
        items = [items]
    if not isinstance(items, list):
        return []
    ids = []
    for item in items:
        if isinstance(item, dict):
            value = item.get("id") or item.get("mental_model_id")
        elif isinstance(item, str):
            value = item
        else:
            continue
        if not isinstance(value, str):
            continue
        value = value.strip()
        if not value or value in ids or not _is_valid_model_id(value):
            continue
        ids.append(value)
        if limit is not None and len(ids) >= limit:
            break
    return ids


def _read_id_list(path):
    """Return ``{"fetched_at", "ids"}`` from the id-list cache, or ``None``."""
    try:
        data = json.loads(path.read_text("utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    ids = data.get("ids")
    if not isinstance(ids, list):
        return None
    return {
        "fetched_at": data.get("fetched_at"),
        "ids": [str(item) for item in ids if str(item).strip()],
    }


def _write_id_list(path, ids):
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({"fetched_at": _now_iso(), "ids": list(ids)}, ensure_ascii=False),
            "utf-8",
        )
    except OSError as exc:
        _LOG.warning("hindsight-plus: could not write cache %s: %s", path, exc)


def _discover_project_model_ids(client, logger, cache_path, ttl):
    """Return the project bank's model ids, on the same cache contract as models.

    Fresh cache first; on a failed or unusable refresh, the stale list is used
    so a remembered project section survives an offline turn. Never raises.
    """
    cached = _read_id_list(cache_path)
    if cached is not None and cached["ids"] and _is_fresh(cached, ttl):
        return cached["ids"]

    try:
        payload = client.list_models()
    except (OSError, ValueError) as exc:
        logger.warning("hindsight-plus: could not list project bank models: %s", exc)
        return cached["ids"] if cached is not None else []

    ids = _model_ids_from_list(payload)
    if ids:
        _write_id_list(cache_path, ids)
        return ids
    # An empty or unrecognized listing is treated like a failed refresh: the
    # cached ids are better than silently losing a section that exists.
    return cached["ids"] if cached is not None else []


def _load_project_block(config, client, cache_dir, cache_ttl_s, logger):
    """Return ``(model_ids, sections, metadata)`` read from the project bank.

    Read-only: every failure is logged and skipped, and ``create`` stays unset
    so a 404 here can never become a POST. Section headings use ``###`` to sit
    under the group heading, and each section is a whole unit for truncation.
    """
    bank = str(client.bank_id)
    requested = _as_list(config.get("project_mental_model_ids"))
    if requested:
        candidates = requested
    else:
        list_path = cache_dir / (
            f"id-list--{_safe_name(bank)}-{_bank_cache_hash(bank)}.json"
        )
        candidates = _discover_project_model_ids(client, logger, list_path, cache_ttl_s)

    ids = []
    sections = []
    meta = []
    for model_id in candidates:
        cache_path = cache_dir / (
            f"{_safe_name(bank)}-{_bank_cache_hash(bank)}--{_safe_name(model_id)}.json"
        )
        model = load_model(
            model_id, client, cache_path, cache_ttl_s, logger, create=None
        )
        if model is None:
            continue
        content = (model.get("content") or "").strip()
        if not content:
            continue
        ids.append(model_id)
        stale_age = model.get("_stale_cache_age")
        meta.append(
            {
                "id": model_id,
                "name": str(model.get("name") or model_id),
                "last_refreshed_at": _json_scalar(model.get("last_refreshed_at")),
                "is_stale": bool(model.get("is_stale")),
                "cache_age_s": (
                    _as_int(stale_age, 0) if stale_age is not None else None
                ),
                "content_chars": len(content),
            }
        )
        sections.append(f"### {model.get('name') or model_id}\n\n{content}")
    return ids, sections, meta


def _fit_project_block(prefix, group, sections, max_chars):
    """Append whole project sections to ``prefix`` while they fit.

    Returns ``(text, kept)``. A section is never cut mid-way, and the group
    heading is never left alone: when nothing fits, ``text == prefix``.
    """
    kept = len(sections)
    while kept > 0:
        candidate = "\n\n".join([prefix, group, *sections[:kept]])
        if len(candidate) <= max_chars:
            return candidate, kept
        kept -= 1
    return prefix, 0


def create_spec(config):
    """Return the create-if-missing spec, or ``None`` when creation is disabled.

    Only used after a 404: an existing model is never patched, so its name,
    source query, schedule, and content stay whatever the user configured.
    """
    if not _as_bool(config.get("create_missing"), True):
        return None

    name = str(config.get("create_name") or DEFAULT_CREATE_NAME).strip()
    source_query = str(
        config.get("create_source_query") or DEFAULT_CREATE_SOURCE_QUERY
    ).strip()
    raw_cron = config.get("create_refresh_cron")
    # Absent -> the documented daily default. Explicit "" -> no schedule.
    cron = (
        DEFAULT_CREATE_REFRESH_CRON
        if raw_cron is None
        else str(raw_cron).strip()
    )
    max_tokens = _as_int(config.get("create_max_tokens"), DEFAULT_CREATE_MAX_TOKENS)
    max_tokens = max(256, min(8192, max_tokens))
    mode = str(config.get("create_mode") or DEFAULT_CREATE_MODE).strip().lower()
    if mode not in {"full", "delta"}:
        mode = DEFAULT_CREATE_MODE

    return {
        "name": name or DEFAULT_CREATE_NAME,
        "source_query": source_query or DEFAULT_CREATE_SOURCE_QUERY,
        "refresh_cron": cron,
        "max_tokens": max_tokens,
        "mode": mode,
    }


def _is_valid_model_id(model_id):
    return bool(_MODEL_ID_RE.match(str(model_id or "")))


def _read_cache(path: pathlib.Path):
    try:
        data = json.loads(path.read_text("utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    if not data.get("content"):
        return None
    return data


def _write_cache(path: pathlib.Path, record: dict):
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(record, ensure_ascii=False), "utf-8")
    except OSError as exc:
        _LOG.warning("hindsight-plus: could not write cache %s: %s", path, exc)


def _is_fresh(record: dict, ttl) -> bool:
    dt = _parse_iso(record.get("fetched_at"))
    if dt is None:
        return False
    age = (datetime.now(timezone.utc) - dt).total_seconds()
    return age < ttl


def _truncation_marker(model_ids) -> str:
    ids = ", ".join(str(i) for i in model_ids)
    label = "mental model" if len(model_ids) == 1 else "mental models"
    return f"[... truncated — full {label}: {ids}]"


def truncate(text: str, max_chars: int, model_ids) -> str:
    """Truncate ``text`` at the last complete line that fits within ``max_chars``
    and append a marker line. The final string (including the marker) never
    exceeds ``max_chars``."""
    max_chars = max(1, int(max_chars))
    if len(text) <= max_chars:
        return text

    marker = _truncation_marker(model_ids)

    # If the marker cannot fit, honour the hard cap and skip it.
    if len(marker) + 1 >= max_chars:
        return text[:max_chars]

    # Reserve room for the marker and its preceding newline.
    budget = max_chars - len(marker) - 1

    head = text[:budget]
    cut = head.rfind("\n")
    if cut <= 0:
        cut = budget  # no line boundary found in the budget; hard cut

    truncated = text[:cut].rstrip("\n")
    return truncated + "\n" + marker


def build_header(models) -> str:
    """Header of at most 3 lines describing the injected context."""
    if len(models) == 1:
        model = models[0]
        refreshed = _format_date(model.get("last_refreshed_at"))
        stale_days = _stale_days(model.get("last_refreshed_at"))
        if stale_days is None:
            title = f"## Stable context (Hindsight mental model, refreshed {refreshed})"
        else:
            title = f"## Stable context (Hindsight mental model, refreshed {refreshed}, stale for {stale_days} d)"
    else:
        title = "## Stable context (Hindsight mental models)"

    lines = [title]
    stale_ages = [m.get("_stale_cache_age") for m in models if m.get("_stale_cache_age")]
    if stale_ages:
        lines.append(f"(cache fallback, {_human_age(max(stale_ages))} old)")
    lines.append(
        "Historical context: a remembered instruction never overrides the current message, "
        "and anything that can change should be verified."
    )
    return "\n".join(lines)


class HindsightClient:
    """Minimal Hindsight client with one shared wall-clock budget.

    ``timeout_s`` bounds the *whole* injection attempt (all models), not each
    request, so a slow endpoint cannot stretch a turn past the configured
    budget. Requests still fall back to the on-disk cache on failure.
    """

    def __init__(self, api_url, bank_id, api_key, timeout_s, deadline=None):
        self.api_url = (api_url or "").rstrip("/")
        self.bank_id = bank_id
        self.api_key = api_key
        self.timeout_s = timeout_s
        # ``deadline`` lets a second client (the project bank) share the
        # configured wall-clock budget instead of getting a fresh one.
        if deadline is None:
            deadline = time.monotonic() + max(0.0, float(timeout_s or 0.0))
        self._deadline = deadline

    def _remaining(self):
        return max(0.0, self._deadline - time.monotonic())

    def get_model(self, model_id):
        remaining = self._remaining()
        if remaining <= 0:
            raise TimeoutError("Hindsight request budget exhausted")
        bank = urllib.parse.quote(str(self.bank_id), safe="")
        model = urllib.parse.quote(str(model_id), safe="")
        # ``detail=content`` is required: the API returns metadata only by default.
        url = (
            f"{self.api_url}/v1/default/banks/{bank}"
            f"/mental-models/{model}?detail=content"
        )
        return _http_get_json(url, remaining, self.api_key)

    def list_models(self):
        """Return the bank's mental-model collection (metadata only, no content)."""
        remaining = self._remaining()
        if remaining <= 0:
            raise TimeoutError("Hindsight request budget exhausted")
        bank = urllib.parse.quote(str(self.bank_id), safe="")
        url = f"{self.api_url}/v1/default/banks/{bank}/mental-models"
        return _http_get_json(url, remaining, self.api_key)

    def create_model(self, model_id, spec):
        remaining = self._remaining()
        if remaining <= 0:
            raise TimeoutError("Hindsight request budget exhausted")
        bank = urllib.parse.quote(str(self.bank_id), safe="")
        url = f"{self.api_url}/v1/default/banks/{bank}/mental-models"
        trigger = {
            "mode": spec.get("mode") or DEFAULT_CREATE_MODE,
            # ``refresh_cron`` and ``refresh_after_consolidation`` are mutually
            # exclusive; the daily cron is the default.
            "refresh_after_consolidation": False,
        }
        cron = spec.get("refresh_cron") or ""
        if cron:
            trigger["refresh_cron"] = cron
        payload = {
            "id": model_id,
            "name": spec.get("name") or DEFAULT_CREATE_NAME,
            "source_query": spec.get("source_query") or DEFAULT_CREATE_SOURCE_QUERY,
            "max_tokens": spec.get("max_tokens") or DEFAULT_CREATE_MAX_TOKENS,
            "trigger": trigger,
        }
        return _http_post_json(url, remaining, self.api_key, payload)


def _create_missing_model(model_id, client, spec, logger):
    """Create one absent mental model. Never called for an existing model."""
    if not _is_valid_model_id(model_id):
        logger.warning(
            "hindsight-plus: cannot create mental model %r: id must be lowercase "
            "alphanumeric with hyphens",
            model_id,
        )
        return False
    try:
        client.create_model(model_id, spec)
        logger.info(
            "hindsight-plus: created missing mental model %s (refresh_cron=%s)",
            model_id,
            spec.get("refresh_cron") or "none",
        )
        return True
    except urllib.error.HTTPError as exc:
        if exc.code == 409:
            logger.info(
                "hindsight-plus: mental model %s already exists (concurrent create)",
                model_id,
            )
            return True
        logger.warning(
            "hindsight-plus: failed to create mental model %s: HTTP %s",
            model_id,
            exc.code,
        )
        return False
    except Exception as exc:
        logger.warning(
            "hindsight-plus: failed to create mental model %s: %s", model_id, exc
        )
        return False


def load_model(model_id, client, cache_path, ttl, logger, create=None):
    cached = _read_cache(cache_path)
    if cached is not None and _is_fresh(cached, ttl):
        return cached

    try:
        obj = client.get_model(model_id)
        if not isinstance(obj, dict):
            raise ValueError("unexpected response shape for mental model")

        content = obj.get("content") or ""
        if not isinstance(content, str):
            content = ""

        record = {
            "content": content,
            "name": str(obj.get("name") or model_id),
            "fetched_at": _now_iso(),
            "last_refreshed_at": obj.get("last_refreshed_at"),
            "is_stale": obj.get("is_stale"),
        }
        _write_cache(cache_path, record)
        return record
    except Exception as exc:
        missing = isinstance(exc, urllib.error.HTTPError) and exc.code == 404
        if missing and create is not None:
            # Create only when the model is absent. Never PATCH an existing
            # model: its name, query, schedule, and content belong to the user.
            created = _create_missing_model(model_id, client, create, logger)
            if not created:
                logger.warning(
                    "hindsight-plus: failed to fetch mental model %s: %s",
                    model_id,
                    exc,
                )
        else:
            logger.warning(
                "hindsight-plus: failed to fetch mental model %s: %s", model_id, exc
            )

        if cached is not None:
            fallback = dict(cached)
            fallback["_stale_cache_age"] = _cache_age_s(cached)
            return fallback
        return None


def build_injection_details(config, logger=None, hermes_home=None, project=None):
    """Assemble the injection and return its text plus audit metadata.

    Returns ``None`` when there is nothing to inject. The text is exactly what
    ``pre_llm_call`` returns; the extra fields let the audit record describe
    what was injected without re-fetching or re-assembling anything.

    ``project`` optionally names a second, read-only bank for the turn's
    repository (``{"name": <repo>, "bank": <bank id>}``). Its models are
    appended after the configured bank's, share the same ``max_chars`` budget,
    and are never created: creation stays reserved to the configured bank.
    """
    logger = logger or _LOG
    home = pathlib.Path(hermes_home) if hermes_home else _hermes_home()

    api_url, bank_id, api_key = resolve_endpoint(home)
    if not api_url or not bank_id:
        logger.warning(
            "hindsight-plus: Hindsight endpoint not configured; skipping injection "
            "(api_url set: %s, bank_id set: %s)",
            bool(api_url),
            bool(bank_id),
        )
        return None

    cache_dir = _plugin_data_dir(hermes_home)
    timeout_s = _as_float(config.get("timeout_s"), 3.0)
    cache_ttl_s = _as_int(config.get("cache_ttl_s"), 21600)
    model_ids = _as_list(config.get("mental_model_ids"))
    create = create_spec(config)
    client = HindsightClient(api_url, bank_id, api_key, timeout_s)

    models = []
    model_meta = []
    sections = []
    for model_id in model_ids:
        cache_path = cache_dir / f"{_safe_name(bank_id)}--{_safe_name(model_id)}.json"
        model = load_model(model_id, client, cache_path, cache_ttl_s, logger, create)
        if model is None:
            continue
        content = (model.get("content") or "").strip()
        if not content:
            continue
        models.append(model)
        stale_age = model.get("_stale_cache_age")
        model_meta.append(
            {
                "id": model_id,
                "name": str(model.get("name") or model_id),
                "last_refreshed_at": _json_scalar(model.get("last_refreshed_at")),
                "is_stale": bool(model.get("is_stale")),
                "cache_age_s": _as_int(stale_age, 0) if stale_age is not None else None,
                "content_chars": len(content),
            }
        )
        sections.append(f"## {model.get('name') or model_id}\n\n{content}")

    if not sections:
        logger.warning("hindsight-plus: no mental model content available; injecting nothing")
        return None

    # Optional second bank, read-only. Every failure below is swallowed so the
    # configured-bank injection is unaffected.
    project = project if isinstance(project, dict) else None
    project_name = ""
    project_bank = ""
    project_ids = []
    project_sections = []
    project_meta = []
    project_kept = 0
    if project and project.get("bank"):
        project_bank = str(project["bank"])
        project_name = str(project.get("name") or project_bank)
        if not _valid_bank_id(project_bank):
            # Never put an unsafe id in a URL; the section is simply disabled.
            _log_bad_bank(logger, project_bank)
            project_bank = ""
        else:
            project_client = HindsightClient(
                api_url, project_bank, api_key, timeout_s, deadline=client._deadline
            )
            project_ids, project_sections, project_meta = _load_project_block(
                config, project_client, cache_dir, cache_ttl_s, logger
            )

    max_chars = max(1, min(_as_int(config.get("max_chars"), 6000), HERMES_CALLBACK_CAP))
    note = str(config.get("context_note") or "").strip()
    parts = [build_header(models)]
    if note:
        parts.append(note)
    parts.extend(sections)
    text = "\n\n".join(parts)
    injected = truncate(text, max_chars, model_ids + project_ids)
    truncated = len(injected) < len(text)

    # The project block is assembled whole: a section is never cut mid-way and
    # the group heading never stands alone. When the main text already fills the
    # budget (or no section fits), the injection is byte-identical to today's.
    if project_sections and not truncated:
        group = (
            f"## Project memory ({project_name})\n\n"
            f"These facts apply only when working in {project_name}."
        )
        fitted, project_kept = _fit_project_block(
            injected, group, project_sections, max_chars
        )
        if project_kept:
            injected = fitted
            if project_kept < len(project_sections):
                truncated = True
                marker = _truncation_marker(model_ids + project_ids)
                if len(injected) + 1 + len(marker) <= max_chars:
                    injected = injected + "\n" + marker

    details = {
        "text": injected,
        "assembled_chars": len(text),
        "max_chars": max_chars,
        "truncated": truncated,
        "context_note_chars": len(note),
        "models": model_meta,
    }
    if project_kept:
        details["project"] = {
            "bank": project_bank,
            "repo": project_name,
            "models": project_meta[:project_kept],
        }
    return details


def build_injection(config, logger=None, hermes_home=None, project=None):
    """Assemble the injection text for the given config, or return None to skip."""
    details = build_injection_details(
        config, logger=logger, hermes_home=hermes_home, project=project
    )
    if details is None:
        return None
    return details.get("text") or None


# ---------------------------------------------------------------------------
# Injection audit
#
# The Desktop memory pane reads this file through the plugin's dashboard API.
# It is deliberately bounded, plain JSON, and content-optional. Never put the
# API key (or any request header) in here.
# ---------------------------------------------------------------------------

def _audit_path(hermes_home=None) -> pathlib.Path:
    return _plugin_data_dir(hermes_home) / AUDIT_FILENAME


def _empty_audit() -> dict:
    return {"schema": AUDIT_SCHEMA, "updated_at": _now_iso(), "sessions": {}}


def _sanitize_audit(data) -> dict:
    """Return a bounded, typed view of an on-disk audit document."""
    if not isinstance(data, dict):
        data = {}
    raw_sessions = data.get("sessions")
    if not isinstance(raw_sessions, dict):
        raw_sessions = {}

    sessions = {}
    for key, bucket in raw_sessions.items():
        if not isinstance(key, str) or not key or not isinstance(bucket, dict):
            continue
        entries = []
        raw_entries = bucket.get("entries")
        if isinstance(raw_entries, list):
            for entry in raw_entries:
                if isinstance(entry, dict) and isinstance(entry.get("recorded_at"), str):
                    entries.append(entry)
        aliases = []
        for alias in bucket.get("aliases") or []:
            alias = _clean_id(alias)
            if alias and alias not in aliases:
                aliases.append(alias)
        if key not in aliases:
            aliases.insert(0, key)
        sessions[key] = {
            "session_id": _clean_id(bucket.get("session_id")),
            "task_id": _clean_id(bucket.get("task_id")),
            "aliases": aliases[:8],
            "entries": entries,
        }

    return {
        "schema": AUDIT_SCHEMA,
        "updated_at": _json_scalar(data.get("updated_at")) or _now_iso(),
        "sessions": sessions,
    }


def _read_audit(path: pathlib.Path) -> dict:
    try:
        raw = path.read_text("utf-8")
    except FileNotFoundError:
        return _empty_audit()
    except OSError as exc:
        _LOG.warning("hindsight-plus: could not read injection audit %s: %s", path, exc)
        return _empty_audit()

    try:
        data = json.loads(raw)
    except ValueError as exc:
        _LOG.warning("hindsight-plus: ignoring malformed injection audit %s: %s", path, exc)
        return _empty_audit()
    return _sanitize_audit(data)


def _write_audit(path: pathlib.Path, data: dict) -> bool:
    tmp = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(
            f"{path.name}.tmp-{os.getpid()}-{threading.get_ident()}"
        )
        tmp.write_text(
            json.dumps(data, ensure_ascii=False, separators=(",", ":")),
            "utf-8",
        )
        os.replace(tmp, path)
        return True
    except (OSError, ValueError) as exc:
        _LOG.warning("hindsight-plus: could not write injection audit %s: %s", path, exc)
        if tmp is not None:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
        return False


def _audit_limits(config) -> tuple[int, int, int]:
    config = config if isinstance(config, dict) else {}
    max_total = max(
        1,
        min(2000, _as_int(config.get("audit_max_entries"), DEFAULT_AUDIT_MAX_TOTAL)),
    )
    max_per_session = max(
        1,
        min(
            max_total,
            _as_int(
                config.get("audit_max_per_session"), DEFAULT_AUDIT_MAX_PER_SESSION
            ),
        ),
    )
    max_sessions = max(
        1,
        min(500, _as_int(config.get("audit_max_sessions"), DEFAULT_AUDIT_MAX_SESSIONS)),
    )
    return max_total, max_per_session, max_sessions


def _find_session_key(sessions: dict, session_id="", task_id="") -> str:
    wanted = [_clean_id(task_id), _clean_id(session_id)]
    for value in wanted:
        if not value:
            continue
        for key, bucket in sessions.items():
            candidates = {
                key,
                _clean_id(bucket.get("session_id")),
                _clean_id(bucket.get("task_id")),
                *[_clean_id(alias) for alias in bucket.get("aliases") or []],
            }
            if value in candidates:
                return key
    return ""


def _audit_model_metadata(models) -> list[dict]:
    result = []
    if not isinstance(models, list):
        return result
    for item in models[:16]:
        if not isinstance(item, dict):
            continue
        cache_age = item.get("cache_age_s")
        result.append(
            {
                "id": _clean_id(item.get("id")),
                "name": str(item.get("name") or item.get("id") or "")[:300],
                "last_refreshed_at": _json_scalar(item.get("last_refreshed_at")),
                "is_stale": bool(item.get("is_stale")),
                "cache_age_s": None if cache_age is None else max(0, _as_int(cache_age, 0)),
                "content_chars": max(0, _as_int(item.get("content_chars"), 0)),
            }
        )
    return result


def _audit_project_metadata(project):
    """Bounded, content-free view of the project bank that was injected."""
    if not isinstance(project, dict):
        return None
    bank = str(project.get("bank") or "").strip()
    repo = str(project.get("repo") or "").strip()
    models = _audit_model_metadata(project.get("models"))
    if not bank and not repo and not models:
        return None
    return {
        "bank": bank[:128],
        "repo": repo[:128],
        "models": models,
    }


def _user_preview(value) -> str:
    if isinstance(value, str):
        text = value
    elif isinstance(value, list):
        parts = []
        for item in value:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict) and isinstance(item.get("text"), str):
                parts.append(item["text"])
        text = " ".join(parts)
    else:
        text = "" if value is None else str(value)
    return re.sub(r"\s+", " ", text).strip()[:AUDIT_PREVIEW_CHARS]


def _session_summary(key: str, bucket: dict) -> dict:
    entries = bucket.get("entries") or []
    latest = entries[-1] if entries else {}
    models = latest.get("models") if isinstance(latest.get("models"), list) else []
    return {
        "key": key,
        "session_id": _clean_id(bucket.get("session_id")),
        "task_id": _clean_id(bucket.get("task_id")),
        "aliases": list(bucket.get("aliases") or [])[:8],
        "first_recorded_at": entries[0].get("recorded_at") if entries else "",
        "last_recorded_at": (
            latest.get("recorded_at") or bucket.get("last_recorded_at") or ""
        ),
        "entry_count": len(entries),
        "last_user_preview": str(latest.get("user_preview") or "")[:AUDIT_PREVIEW_CHARS],
        "last_injected_chars": max(0, _as_int(latest.get("injected_chars"), 0)),
        "last_truncated": bool(latest.get("truncated")),
        "last_models": [
            str(item.get("name") or item.get("id") or "")
            for item in models[:8]
            if isinstance(item, dict)
        ],
    }


def _trim_audit(
    sessions: dict, max_total: int, max_per_session: int, max_sessions: int
) -> None:
    for bucket in sessions.values():
        entries = bucket.get("entries") or []
        if len(entries) > max_per_session:
            bucket["entries"] = entries[-max_per_session:]

    if len(sessions) > max_sessions:
        oldest = sorted(
            sessions.items(),
            key=lambda item: str(item[1].get("last_recorded_at") or ""),
        )
        for key, _bucket in oldest[: len(sessions) - max_sessions]:
            sessions.pop(key, None)

    total = sum(len(bucket.get("entries") or []) for bucket in sessions.values())
    while total > max_total:
        candidates = [
            (str(bucket.get("last_recorded_at") or ""), key)
            for key, bucket in sessions.items()
            if bucket.get("entries")
        ]
        if not candidates:
            break
        _when, key = min(candidates)
        bucket = sessions[key]
        bucket["entries"].pop(0)
        total -= 1
        if not bucket["entries"]:
            sessions.pop(key, None)

    for key, bucket in list(sessions.items()):
        entries = bucket.get("entries") or []
        if not entries:
            sessions.pop(key, None)
            continue
        bucket["entry_count"] = len(entries)
        bucket["first_recorded_at"] = entries[0].get("recorded_at") or ""


def record_injection(
    details,
    config=None,
    session_id=None,
    task_id=None,
    turn_id=None,
    user_message=None,
    is_first_turn=False,
    platform=None,
    model=None,
    hermes_home=None,
):
    """Persist one successful injection. Returns a content-free summary.

    Fails open: any read/write error is logged and ``None`` is returned, so it
    can never break the ``pre_llm_call`` hook that already has context to
    return.
    """
    try:
        if not isinstance(details, dict):
            return None
        text = details.get("text")
        if not isinstance(text, str) or not text:
            return None
        session_id = _clean_id(session_id)
        task_id = _clean_id(task_id)
        if not session_id and not task_id:
            return None

        home = pathlib.Path(hermes_home) if hermes_home else _hermes_home()
        try:
            _api_url, bank_id, _api_key = resolve_endpoint(home)
        except Exception:
            bank_id = ""
        bank_id = str(bank_id or "")[:128]

        config = config if isinstance(config, dict) else {}
        record_content = _as_bool(config.get("record_content"), True)
        max_total, max_per_session, max_sessions = _audit_limits(config)
        path = _audit_path(hermes_home)

        with _AUDIT_LOCK:
            data = _read_audit(path)
            sessions = data["sessions"]
            now = _now_iso()
            key = _find_session_key(sessions, session_id, task_id)
            if not key:
                key = task_id or session_id
            bucket = sessions.get(key)
            if bucket is None:
                bucket = {
                    "session_id": session_id,
                    "task_id": task_id,
                    "aliases": [],
                    "entries": [],
                }
                sessions[key] = bucket

            aliases = list(bucket.get("aliases") or [])
            for value in (key, session_id, task_id):
                if value and value not in aliases:
                    aliases.append(value)
            bucket["aliases"] = aliases[:8]
            if session_id:
                bucket["session_id"] = session_id
            if task_id:
                bucket["task_id"] = task_id

            entry_id = f"{time.time_ns():x}-{os.urandom(4).hex()}"
            entry = {
                "id": entry_id,
                "recorded_at": now,
                "bank": bank_id,
                "session_id": session_id,
                "task_id": task_id,
                "turn_id": _clean_id(turn_id),
                "is_first_turn": bool(is_first_turn),
                "platform": str(platform or "")[:80],
                "model": str(model or "")[:160],
                "user_preview": _user_preview(user_message) if record_content else "",
                "content": text[:AUDIT_CONTENT_CAP] if record_content else None,
                "content_recorded": record_content,
                "injected_chars": len(text),
                "assembled_chars": max(0, _as_int(details.get("assembled_chars"), len(text))),
                "max_chars": max(0, _as_int(details.get("max_chars"), 0)),
                "truncated": bool(details.get("truncated")),
                "context_note_chars": max(
                    0, _as_int(details.get("context_note_chars"), 0)
                ),
                "models": _audit_model_metadata(details.get("models")),
            }
            project_meta = _audit_project_metadata(details.get("project"))
            if project_meta:
                # Present only when a project section was actually injected, so
                # an entry without one keeps today's shape exactly.
                entry["project"] = project_meta
            entries = bucket.setdefault("entries", [])
            entries.append(entry)
            bucket["last_recorded_at"] = now
            _trim_audit(sessions, max_total, max_per_session, max_sessions)
            data["updated_at"] = now
            if not _write_audit(path, data):
                return None

            bucket = sessions.get(key)
            if bucket is None:
                return None
            return {
                "entry_id": entry_id,
                "session_key": key,
                "session_id": bucket.get("session_id") or session_id,
                "task_id": bucket.get("task_id") or task_id,
                "recorded_at": now,
                "entry_count": len(bucket.get("entries") or []),
            }
    except Exception as exc:  # audit must never break the injection path
        _LOG.warning("hindsight-plus: could not record injection audit: %s", exc)
        return None


def read_injection_history(session_id, hermes_home=None, limit=200) -> dict:
    """Return one session's retained injections, newest first."""
    session_id = _clean_id(session_id)
    limit = max(1, min(1000, _as_int(limit, 200)))
    result = {"schema": AUDIT_SCHEMA, "session": None, "entries": [], "bank": ""}
    if not session_id:
        try:
            _api_url, bank_id, _api_key = resolve_endpoint(
                pathlib.Path(hermes_home) if hermes_home else _hermes_home()
            )
            result["bank"] = str(bank_id or "")
        except Exception:
            pass
        return result

    path = _audit_path(hermes_home)
    with _AUDIT_LOCK:
        data = _read_audit(path)
    key = _find_session_key(data["sessions"], session_id=session_id)
    if not key:
        return result
    bucket = data["sessions"][key]
    result["session"] = _session_summary(key, bucket)
    result["entries"] = list(reversed(bucket.get("entries") or []))[:limit]
    if not result["bank"]:
        try:
            _api_url, bank_id, _api_key = resolve_endpoint(
                pathlib.Path(hermes_home) if hermes_home else _hermes_home()
            )
            result["bank"] = str(bank_id or "")
        except Exception:
            pass
    if not result["bank"]:
        for entry in result["entries"]:
            if isinstance(entry, dict) and entry.get("bank"):
                result["bank"] = str(entry.get("bank"))
                break
    return result


def _state_db_timestamp_to_iso(value):
    if value is None:
        return ""
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return ""
        try:
            dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except (ValueError, TypeError):
            return text
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.isoformat()
    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value)
    if number > 1e12:
        number = number / 1000.0
    try:
        return datetime.fromtimestamp(number, tz=timezone.utc).isoformat()
    except (OSError, ValueError, OverflowError):
        return str(value)


_MEMORY_CONTEXT_OPEN = "<memory-context>"
_MEMORY_CONTEXT_CLOSE = "</memory-context>"
_PLUGIN_BLOCK_MARKER = "## Stable context (Hindsight"
# A recall preamble is matched on its head only: the tail can differ from the
# config (the pane itself drops a leading ``# ``), and 200 characters are
# specific enough that ordinary prose does not match by accident.
_RECALL_PREAMBLE_PREFIX_CHARS = 200


def _recall_preamble(hermes_home):
    """Return the configured Hindsight recall preamble, or ``""``.

    Read-only and fail-open, exactly like ``GET /recall-preamble`` in the
    dashboard: a missing or unreadable file, bad JSON, a non-object config, a
    non-string value and an empty (or whitespace-only) one all read as ``""``.
    This is display metadata, so nothing here may raise or touch the network.
    """
    try:
        path = pathlib.Path(hermes_home) / "hindsight" / "config.json"
        config = json.loads(path.read_text("utf-8"))
    except (OSError, ValueError):
        return ""
    if not isinstance(config, dict):
        return ""
    preamble = config.get("recall_prompt_preamble")
    if not isinstance(preamble, str) or not preamble.strip():
        return ""
    return preamble[:_RECALL_PREAMBLE_PREFIX_CHARS]


def _recall_preamble_index(text, preamble):
    """Return where ``preamble`` starts in ``text``, or ``-1``.

    A substring search, so a preamble shown with a leading ``# `` still matches
    and the entry starts at the preamble itself, not at the ``#``.
    """
    if not preamble:
        return -1
    return text.find(preamble)


def read_state_db_injections(session_id, hermes_home=None, limit=200) -> dict:
    """Read injection history from Hermes' read-only ``state.db``.

    Returns ``{"session", "entries", "source", "reason"?}``. ``source`` is
    ``"state.db"`` when the database could be read, ``"unavailable"`` otherwise.
    Never writes to the database.

    One entry per injected block found in a message, whatever its marker:
    ``kind`` is ``"memory-context"`` for the provider block between the
    ``<memory-context>`` tags, ``"mental-models"`` for this plugin's
    ``## Stable context (Hindsight ...)`` header, and ``"provider-recall"`` for
    a bare configured recall preamble. ``source`` keeps its historical value
    (the Desktop pane branches on it) and ``kind`` says which marker matched.
    """
    session_id = _clean_id(session_id)
    limit = max(1, min(1000, _as_int(limit, 200)))
    result = {"session": session_id, "entries": [], "source": "unavailable"}

    home = pathlib.Path(hermes_home) if hermes_home else _hermes_home()
    db_path = home / "state.db"

    # Resolve the configured bank once; failures leave it empty rather than
    # aborting the whole read.
    bank_id = ""
    try:
        _api_url, resolved_bank, _api_key = resolve_endpoint(home)
        bank_id = str(resolved_bank or "")
    except Exception:
        bank_id = ""

    if not db_path.is_file():
        result["reason"] = "state.db not found"
        return result

    conn = None
    try:
        conn = sqlite3.connect(
            db_path.resolve().as_uri() + "?mode=ro",
            uri=True,
            timeout=2.0,
        )
        try:
            conn.execute("PRAGMA query_only = ON")
        except sqlite3.Error:
            pass
        cursor = conn.execute(
            "SELECT id, api_content, timestamp FROM messages "
            "WHERE session_id = ? AND api_content IS NOT NULL "
            "AND api_content <> '' ORDER BY id DESC LIMIT ?",
            (session_id, limit),
        )
        rows = cursor.fetchall()
        cursor.close()
    except (sqlite3.Error, OSError, ValueError) as exc:
        result["reason"] = "state.db unreadable: %s" % exc
        return result
    finally:
        if conn is not None:
            try:
                conn.close()
            except sqlite3.Error:
                pass

    preamble = _recall_preamble(home)

    entries = []
    for row in rows:
        try:
            row_id = row[0]
            api_content = row[1]
            timestamp = row[2]
        except (IndexError, TypeError):
            continue
        if not isinstance(api_content, str) or not api_content.strip():
            continue
        recorded_at = _state_db_timestamp_to_iso(timestamp)

        # Provider recall block: between <memory-context> and </memory-context>.
        # This spans the tags and is the only marker whose end is known; the
        # other two run to the end of the message, so a marker that falls
        # inside this span is never reported a second time.
        tag_span = None
        open_idx = api_content.find(_MEMORY_CONTEXT_OPEN)
        if open_idx != -1:
            close_idx = api_content.find(
                _MEMORY_CONTEXT_CLOSE, open_idx + len(_MEMORY_CONTEXT_OPEN)
            )
            if close_idx != -1:
                tag_span = (
                    open_idx,
                    close_idx + len(_MEMORY_CONTEXT_CLOSE),
                )
                inner = api_content[
                    open_idx + len(_MEMORY_CONTEXT_OPEN) : close_idx
                ]
                lines = inner.splitlines()
                start = 0
                while start < len(lines) and not lines[start].strip():
                    start += 1
                split_at = next(
                    (i for i in range(start, len(lines)) if not lines[i].strip()),
                    None,
                )
                if split_at is None:
                    memory_lines = [
                        line for line in lines[start:]
                        if not line.lstrip().startswith("[System note")
                    ]
                else:
                    memory_lines = lines[split_at + 1:]
                text = "\n".join(memory_lines).strip()
                bullet_lines = [
                    line for line in memory_lines
                    if line.lstrip().startswith(("-", "*"))
                ]
                count = len(bullet_lines) if bullet_lines else None
                status = "empty" if not text else "injected"
                entries.append(
                    {
                        "id": row_id,
                        "recorded_at": recorded_at,
                        "session_id": session_id,
                        "kind": "memory-context",
                        "source": "provider recall",
                        "bank": bank_id,
                        "count": count,
                        "injected_chars": len(text),
                        "status": status,
                        "text": text[:4000],
                        "turn_id": "",
                    }
                )

        # Plugin mental-models block: starts with the stable-context header and
        # is appended last, so it runs to the end of the message.
        plugin_idx = api_content.find(_PLUGIN_BLOCK_MARKER)
        if tag_span is not None and tag_span[0] <= plugin_idx < tag_span[1]:
            plugin_idx = -1
        if plugin_idx != -1:
            block = api_content[plugin_idx:].strip()
            count = sum(
                1
                for line in block.splitlines()
                if line.lstrip().startswith("## ")
            )
            text = block.strip()
            if not text:
                status = "empty"
            elif "[... truncated" in text:
                status = "truncated"
            else:
                status = "injected"
            entries.append(
                {
                    "id": row_id,
                    "recorded_at": recorded_at,
                    "session_id": session_id,
                    "kind": "mental-models",
                    "source": "plugin mental-models",
                    "bank": bank_id,
                    "count": count,
                    "injected_chars": len(text),
                    "status": status,
                    "text": text[:4000],
                    "turn_id": "",
                }
            )

        # A bare recall preamble (the provider not wrapping its recall in the
        # tags above) also runs to the end of the message.
        preamble_idx = _recall_preamble_index(api_content, preamble)
        if tag_span is not None:
            covered = tag_span[0] <= preamble_idx < tag_span[1]
            if covered:
                preamble_idx = -1
        if preamble_idx != -1:
            text = api_content[preamble_idx:].strip()
            if text:
                entries.append(
                    {
                        "id": row_id,
                        "recorded_at": recorded_at,
                        "session_id": session_id,
                        "kind": "provider-recall",
                        "source": "provider recall",
                        "bank": bank_id,
                        "count": None,
                        "injected_chars": len(text),
                        "status": "injected",
                        "text": text[:4000],
                        "turn_id": "",
                    }
                )

    result["entries"] = entries
    result["source"] = "state.db"
    return result


def list_injection_sessions(hermes_home=None, limit=100) -> dict:
    """Return retained session summaries, newest first."""
    limit = max(1, min(500, _as_int(limit, 100)))
    path = _audit_path(hermes_home)
    with _AUDIT_LOCK:
        data = _read_audit(path)
    sessions = [
        _session_summary(key, bucket)
        for key, bucket in data["sessions"].items()
    ]
    sessions.sort(key=lambda item: item.get("last_recorded_at") or "", reverse=True)
    return {"schema": AUDIT_SCHEMA, "sessions": sessions[:limit]}


def injection_audit_stats(hermes_home=None) -> dict:
    path = _audit_path(hermes_home)
    with _AUDIT_LOCK:
        data = _read_audit(path)
    sessions = data["sessions"]
    return {
        "schema": AUDIT_SCHEMA,
        "session_count": len(sessions),
        "entry_count": sum(len(bucket.get("entries") or []) for bucket in sessions.values()),
        "file_present": path.is_file(),
    }


