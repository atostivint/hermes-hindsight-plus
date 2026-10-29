"""Memory review backend: list Hindsight facts with staleness scores and curate them.

Stdlib only and independent of ``core`` so ``dashboard/plugin_api.py`` can load it by
path. The Hindsight API key is passed in by the caller for each request; it is only ever
placed in the ``Authorization`` header and never returned, logged or written to disk.

The only writes are ``PATCH /memories/{id}`` (soft invalidate, restore, text update).
Nothing here issues ``DELETE`` and nothing retains new facts.
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

BANKS = ("hermes", "atlas", "astro")
CURATABLE_TYPES = ("world", "experience")
MAX_PAGE = 200
MAX_TEXT_CHARS = 4000
MAX_REASON_CHARS = 500
AGE_SATURATION_DAYS = 365
REASON_PREFIXES = ("superseded by ", "outdated: ")

_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
_SORTS = ("age", "volatile", "staleness")
_STATES = ("valid", "invalidated")


class ReviewError(Exception):
    """A request the review layer rejects; ``status`` maps to an HTTP status."""

    def __init__(self, status, message):
        super().__init__(message)
        self.status = status
        self.message = message


# --- volatile heuristic --------------------------------------------------------

_LAN_HOST = (
    r"(?:localhost|127\.\d+\.\d+\.\d+|10\.\d+\.\d+\.\d+|192\.168\.\d+\.\d+"
    r"|172\.(?:1[6-9]|2\d|3[01])\.\d+\.\d+|100\.(?:6[4-9]|[7-9]\d|1[01]\d|12[0-7])\.\d+\.\d+"
    r"|(?:[\w-]+\.)+(?:local|lan|internal|home|ts\.net))"
)

# (label, weight, regex). Weights add up and are capped at 1.0.
_VOLATILE_RULES = (
    ("lan-url", 0.35, re.compile(r"https?://" + _LAN_HOST + r"\b", re.IGNORECASE)),
    ("ip-address", 0.35, re.compile(r"(?<![\d.])(?:\d{1,3}\.){3}\d{1,3}(?![\d.])")),
    ("version", 0.25, re.compile(r"\bv\d+(?:\.\d+){1,3}\b|\b\d+\.\d+\.\d+(?:[-+][\w.]+)?\b")),
    ("commit-hash", 0.3, re.compile(r"\b(?=[0-9a-f]*\d)(?=[0-9a-f]*[a-f])[0-9a-f]{7,40}\b")),
    (
        "time-relative",
        0.2,
        re.compile(
            r"\b(?:pending|as of|currently|at the moment|for now|temporar(?:y|ily)|in progress"
            r"|right now|not yet|todo|en attente|actuellement|à ce jour|pour l'instant"
            r"|en cours|provisoire(?:ment)?)\b",
            re.IGNORECASE,
        ),
    ),
)


def volatile_score(text):
    """Return ``(score, hits)``: score in 0..1 and the labels of the rules that fired."""
    text = str(text or "")
    score = 0.0
    hits = []
    for label, weight, pattern in _VOLATILE_RULES:
        if pattern.search(text):
            hits.append(label)
            score += weight
    return round(min(1.0, score), 3), hits


# --- age / staleness -----------------------------------------------------------


def _parse_iso(value):
    if not value or not isinstance(value, str):
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def age_days(date_value, now=None):
    """Whole days since ``date_value`` (ISO 8601), or ``None`` when unparseable."""
    dt = _parse_iso(date_value)
    if dt is None:
        return None
    now = now or datetime.now(timezone.utc)
    return max(0, (now - dt).days)


def age_score(days):
    """Map an age in days to 0..1, saturating at ``AGE_SATURATION_DAYS``."""
    if days is None:
        return 0.0
    return round(min(1.0, days / AGE_SATURATION_DAYS), 3)


def score_item(item, now=None):
    """Return the review view of one Hindsight list item, with both scores."""
    item = item if isinstance(item, dict) else {}
    text = item.get("text") or ""
    date_value = item.get("date") or item.get("mentioned_at") or ""
    days = age_days(date_value, now)
    a_score = age_score(days)
    v_score, hits = volatile_score(text)
    fact_type = item.get("fact_type")
    return {
        "id": str(item.get("id") or ""),
        "text": text,
        "context": item.get("context") or "",
        "date": date_value,
        "fact_type": fact_type,
        "state": item.get("state") or "valid",
        "invalidation_reason": item.get("invalidation_reason"),
        "curatable": fact_type in CURATABLE_TYPES,
        "age_days": days,
        "age_score": a_score,
        "volatile_score": v_score,
        "volatile_hits": hits,
        "staleness": round((a_score + v_score) / 2, 3),
    }


def sort_items(items, sort):
    """Sort scored items, most stale first. Unknown ages sort as the freshest."""
    if sort not in _SORTS:
        raise ReviewError(400, "sort must be one of: " + ", ".join(_SORTS))
    key = {"age": "age_score", "volatile": "volatile_score", "staleness": "staleness"}[sort]
    tiebreak = "staleness" if key != "staleness" else "age_score"
    return sorted(items, key=lambda it: (it[key], it[tiebreak]), reverse=True)


# --- validation ----------------------------------------------------------------


def check_bank(bank):
    if bank not in BANKS:
        raise ReviewError(404, "unknown bank; expected one of: " + ", ".join(BANKS))
    return bank


def check_memory_id(memory_id):
    if not isinstance(memory_id, str) or not _ID_RE.match(memory_id):
        raise ReviewError(400, "invalid memory id")
    return memory_id


def check_reason(reason):
    reason = str(reason or "").strip()
    if len(reason) > MAX_REASON_CHARS:
        raise ReviewError(400, "reason is too long")
    for prefix in REASON_PREFIXES:
        if reason.lower().startswith(prefix) and reason[len(prefix):].strip():
            return reason
    raise ReviewError(400, "reason must be 'superseded by <...>' or 'outdated: <...>'")


def check_text(text):
    if not isinstance(text, str) or not text.strip():
        raise ReviewError(400, "text must be a non-empty string")
    text = text.strip()
    if len(text) > MAX_TEXT_CHARS:
        raise ReviewError(400, "text is too long")
    return text


# --- HTTP client ---------------------------------------------------------------


class ReviewClient:
    """Tiny Hindsight client for the memory endpoints (GET list/one, PATCH)."""

    def __init__(self, api_url, api_key, timeout_s=15.0):
        self.api_url = (api_url or "").rstrip("/")
        self.api_key = api_key or ""
        self.timeout_s = timeout_s

    def _request(self, method, path, payload=None):
        if not self.api_url:
            raise ReviewError(503, "Hindsight API URL is not configured")
        if method not in ("GET", "PATCH"):
            raise ReviewError(500, "unsupported method")
        data = None
        if payload is not None:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(self.api_url + path, data=data, method=method)
        req.add_header("Accept", "application/json")
        if data is not None:
            req.add_header("Content-Type", "application/json")
        if self.api_key:
            req.add_header("Authorization", "Bearer " + self.api_key)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout_s) as resp:
                body = resp.read()
        except urllib.error.HTTPError as exc:
            # Never echo the upstream body or URL: neither is needed and both could
            # carry request details.
            status = exc.code if exc.code in (400, 404, 409, 422) else 502
            raise ReviewError(status, f"Hindsight returned HTTP {exc.code}") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise ReviewError(502, "Hindsight is unreachable") from exc
        if not body:
            return {}
        try:
            return json.loads(body.decode("utf-8"))
        except ValueError as exc:
            raise ReviewError(502, "Hindsight returned invalid JSON") from exc

    @staticmethod
    def _base(bank):
        return "/v1/default/banks/" + urllib.parse.quote(check_bank(bank), safe="")

    def list_memories(self, bank, limit, offset, state=None, q=None):
        params = {"limit": limit, "offset": offset}
        if state:
            params["state"] = state
        if q:
            params["q"] = q
        query = urllib.parse.urlencode(params)
        return self._request("GET", f"{self._base(bank)}/memories/list?{query}")

    def get_memory(self, bank, memory_id):
        mid = urllib.parse.quote(check_memory_id(memory_id), safe="")
        return self._request("GET", f"{self._base(bank)}/memories/{mid}")

    def patch_memory(self, bank, memory_id, body):
        mid = urllib.parse.quote(check_memory_id(memory_id), safe="")
        return self._request("PATCH", f"{self._base(bank)}/memories/{mid}", body)


# --- operations ------------------------------------------------------------------


def list_review(client, bank, limit=100, offset=0, state="valid", sort="staleness", q="", now=None):
    """List one page of a bank's facts, scored and sorted client-side."""
    if not isinstance(limit, int) or not 1 <= limit <= MAX_PAGE:
        raise ReviewError(400, f"limit must be 1..{MAX_PAGE}")
    if not isinstance(offset, int) or offset < 0:
        raise ReviewError(400, "offset must be >= 0")
    if state not in _STATES:
        raise ReviewError(400, "state must be valid or invalidated")
    raw = client.list_memories(bank, limit, offset, state=state, q=(q or "").strip()[:200])
    raw_items = raw.get("items") if isinstance(raw, dict) else None
    items = [score_item(it, now) for it in (raw_items or []) if isinstance(it, dict)]
    return {
        "bank": bank,
        "total": raw.get("total") if isinstance(raw, dict) else None,
        "offset": offset,
        "limit": limit,
        "sort": sort,
        "items": sort_items(items, sort),
    }


def _snapshot(memory):
    """The fields needed to undo a change: a restorable view of one fact."""
    memory = memory if isinstance(memory, dict) else {}
    return {
        "id": str(memory.get("id") or ""),
        "text": memory.get("text") or "",
        "state": memory.get("state") or "valid",
        "invalidation_reason": memory.get("invalidation_reason"),
        "fact_type": memory.get("fact_type"),
    }


def _curate(client, bank, memory_id, body, require_state=None):
    before = client.get_memory(bank, memory_id)
    previous = _snapshot(before)
    if previous["fact_type"] not in CURATABLE_TYPES:
        raise ReviewError(400, "only world/experience facts can be curated")
    if require_state and previous["state"] != require_state:
        raise ReviewError(409, f"fact is not {require_state}")
    client.patch_memory(bank, memory_id, body)
    current = _snapshot(client.get_memory(bank, memory_id))
    return {"bank": bank, "previous": previous, "current": current}


def revoke(client, bank, memory_id, reason):
    """Soft-invalidate a fact. Reversible with :func:`restore`; never deletes."""
    reason = check_reason(reason)
    return _curate(
        client, bank, memory_id, {"state": "invalidated", "reason": reason}, require_state="valid"
    )


def restore(client, bank, memory_id):
    """Revert an invalidated fact to ``valid``."""
    return _curate(client, bank, memory_id, {"state": "valid"}, require_state="invalidated")


def update(client, bank, memory_id, text):
    """Rewrite a fact's text only; its curation state is left untouched."""
    text = check_text(text)
    return _curate(client, bank, memory_id, {"text": text})


# --- prompt for the "Check with Hermes" chat -----------------------------------

MAX_CHECK_FACTS = 10
VERDICTS = ("still true", "outdated", "cannot tell")


def build_check_prompt(facts):
    """Build the verification prompt for 1..10 facts. The agent only reports; it must not
    edit memory."""
    if not isinstance(facts, list) or not 1 <= len(facts) <= MAX_CHECK_FACTS:
        raise ReviewError(400, f"select 1..{MAX_CHECK_FACTS} facts")
    lines = []
    for fact in facts:
        if not isinstance(fact, dict):
            raise ReviewError(400, "each fact needs id and text")
        fid = check_memory_id(str(fact.get("id") or ""))
        text = check_text(fact.get("text"))
        lines.append(f"- [{fid}] {text}")
    return (
        "Verify each of the following remembered facts against live sources (check the "
        "real systems, files, services or web pages they refer to; do not rely on memory). "
        "Do not modify, delete or invalidate any memory: only report.\n\n"
        + "\n".join(lines)
        + "\n\nAnswer per fact with one of: still true / outdated / cannot tell, and give "
        "the evidence you found. End your reply with one fenced json block, a list of "
        '{"id": "<fact id>", "verdict": "still true|outdated|cannot tell", '
        '"evidence": "<short>"} objects, one per fact.'
    )


def parse_verdicts(reply, expected_ids):
    """Extract per-fact verdicts from an agent reply; unknown ids/verdicts are dropped."""
    expected = set(expected_ids)
    blocks = re.findall(r"```json\s*(.*?)```", str(reply or ""), re.DOTALL)
    out = {}
    for block in reversed(blocks):
        try:
            data = json.loads(block)
        except ValueError:
            continue
        if not isinstance(data, list):
            continue
        for entry in data:
            if not isinstance(entry, dict):
                continue
            fid, verdict = entry.get("id"), str(entry.get("verdict") or "").strip().lower()
            if fid in expected and verdict in VERDICTS and fid not in out:
                out[fid] = {"verdict": verdict, "evidence": str(entry.get("evidence") or "")[:1000]}
        if out:
            break
    return out
