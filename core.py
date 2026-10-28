"""Core logic for the Hermes Hindsight Plus plugin.

Fetches Hindsight mental models over HTTP, caches them on disk, and assembles
them into a context block injected into the current turn's user message.

Stdlib only, no third-party runtime dependency. Target Python 3.11+.
"""

from __future__ import annotations

import json
import logging
import os
import pathlib
import re
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


def truncate(text: str, max_chars: int, model_ids) -> str:
    """Truncate ``text`` at the last complete line that fits within ``max_chars``
    and append a marker line. The final string (including the marker) never
    exceeds ``max_chars``."""
    max_chars = max(1, int(max_chars))
    if len(text) <= max_chars:
        return text

    ids = ", ".join(str(i) for i in model_ids)
    label = "mental model" if len(model_ids) == 1 else "mental models"
    marker = f"[... truncated — full {label}: {ids}]"

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

    def __init__(self, api_url, bank_id, api_key, timeout_s):
        self.api_url = (api_url or "").rstrip("/")
        self.bank_id = bank_id
        self.api_key = api_key
        self.timeout_s = timeout_s
        self._deadline = time.monotonic() + max(0.0, float(timeout_s or 0.0))

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


def build_injection(config, logger=None, hermes_home=None):
    """Assemble the injection text for the given config, or return None to skip."""
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
        sections.append(f"## {model.get('name') or model_id}\n\n{content}")

    if not sections:
        logger.warning("hindsight-plus: no mental model content available; injecting nothing")
        return None

    max_chars = max(1, min(_as_int(config.get("max_chars"), 6000), HERMES_CALLBACK_CAP))
    note = str(config.get("context_note") or "").strip()
    parts = [build_header(models)]
    if note:
        parts.append(note)
    parts.extend(sections)
    text = "\n\n".join(parts)
    return truncate(text, max_chars, model_ids)
