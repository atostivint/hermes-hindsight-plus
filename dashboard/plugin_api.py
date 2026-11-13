"""Dashboard API for the Hindsight memory pane.

The Desktop half reads the plugin's local injection audit through the
read-only ``/health``, ``/history`` and ``/sessions`` routes and the configured
recall preamble through ``/recall-preamble`` (the opt-in memory review routes
below are the only ones that write, via Hindsight's soft ``PATCH /memories/{id}``).
The audit is written by ``core.record_injection`` during ``pre_llm_call``; this
module deliberately reuses that module's reader so both halves agree on the
retention and alias rules.

Mounted by Hermes at ``/api/plugins/hermes-hindsight-plus/``. No route accepts
a filesystem path: the only inputs are a session identifier and a bounded
limit.
"""

from __future__ import annotations

import importlib.util
import json
import os
import pathlib
import sys
import tempfile
import threading
from typing import Annotated

from fastapi import APIRouter, Body, HTTPException, Query
from typing import Any

PLUGIN_ID = "hermes-hindsight-plus"
_CORE_MODULE = "hermes_hindsight_plus_dashboard_core"
_core_lock = threading.Lock()
_core_cache = None


def _core():
    global _core_cache
    if _core_cache is not None:
        return _core_cache
    with _core_lock:
        if _core_cache is not None:
            return _core_cache
        path = pathlib.Path(__file__).resolve().parents[1] / "core.py"
        spec = importlib.util.spec_from_file_location(_CORE_MODULE, path)
        if spec is None or spec.loader is None:
            raise RuntimeError("could not load the plugin core module")
        module = importlib.util.module_from_spec(spec)
        sys.modules[_CORE_MODULE] = module
        try:
            spec.loader.exec_module(module)
        except Exception:
            sys.modules.pop(_CORE_MODULE, None)
            raise
        _core_cache = module
        return module


router = APIRouter()


# ==== Memory review (opt-in page) ===============================================
# Backend of the Desktop "Memory review" page. Logic lives in ``review.py``; these
# routes only resolve the endpoint server-side (the key never reaches the browser),
# call it, and map ``ReviewError`` to HTTP errors. No route calls DELETE.

_REVIEW_MODULE = "hermes_hindsight_plus_dashboard_review"
_review_cache = None


def _review():
    global _review_cache
    if _review_cache is not None:
        return _review_cache
    with _core_lock:
        if _review_cache is not None:
            return _review_cache
        path = pathlib.Path(__file__).resolve().parents[1] / "review.py"
        spec = importlib.util.spec_from_file_location(_REVIEW_MODULE, path)
        if spec is None or spec.loader is None:
            raise RuntimeError("could not load the memory review module")
        module = importlib.util.module_from_spec(spec)
        sys.modules[_REVIEW_MODULE] = module
        try:
            spec.loader.exec_module(module)
        except Exception:
            sys.modules.pop(_REVIEW_MODULE, None)
            raise
        _review_cache = module
        return module


def _review_client():
    core = _core()
    api_url, _bank, api_key = core.resolve_endpoint(core._hermes_home())
    return _review().ReviewClient(api_url, api_key)


# --- local review ledger --------------------------------------------------------
# ``review-decisions.json`` in the plugin data dir, one JSON object per bank:
# ``{"<bank>": {"<memory_id>": {"verdict", "at", "hash"}}}``. It is local
# bookkeeping only: no fact text, no key, no HTTP request, and never written to
# Hindsight. ``review.py`` owns the pure decision helpers; the file lives here.

DECISIONS_FILENAME = "review-decisions.json"
_decisions_lock = threading.Lock()


def _decisions_path():
    # core exposes no public alias: ``_plugin_data_dir`` is its resolver.
    return pathlib.Path(_core()._plugin_data_dir()) / DECISIONS_FILENAME


def _read_decisions():
    """Return the whole ledger; a missing, corrupt or non-object file reads empty.

    Fail open: a ledger the plugin cannot understand must never break the page or
    turn into a 500, the facts simply stay in the review queue.
    """
    try:
        with open(_decisions_path(), "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _bank_decisions(ledger, bank):
    """One bank's entry, ignoring anything that is not a decision object."""
    entry = ledger.get(bank) if isinstance(ledger, dict) else None
    if not isinstance(entry, dict):
        return {}
    return {
        key: value for key, value in entry.items()
        if isinstance(key, str) and isinstance(value, dict)
    }


def _write_decisions(ledger):
    """Write the ledger atomically: temp file in the same dir, then ``os.replace``."""
    path = _decisions_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        dir=str(path.parent), prefix=path.name + ".", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(ledger, handle, ensure_ascii=False, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def _review_call(fn, *args, **kwargs):
    review = _review()
    try:
        return fn(*args, **kwargs)
    except review.ReviewError as exc:
        raise HTTPException(status_code=exc.status, detail=exc.message) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=500, detail="memory review request failed"
        ) from exc


@router.get("/review/{bank}/memories")
def review_list(
    bank: str,
    limit: int = Query(100, ge=1),
    offset: int = Query(0, ge=0),
    state: str = Query("valid", max_length=16),
    sort: str = Query("staleness", max_length=16),
    q: str = Query("", max_length=200),
    review_only: bool = Query(False),
    curatable_only: bool = Query(False),
    volatile_only: bool = Query(False),
):
    """Rank a bank's facts across the whole bank (``age``/``volatile``/``staleness``/``risk``).

    ``items`` is the ``offset..offset + limit`` slice of the globally sorted set;
    ``scanned``, ``truncated``, ``matched`` and ``to_review`` describe the scan.
    ``matched`` is the post-filter count and ``to_review`` the queue size after the
    local ledger, not a ``q`` match count. ``curatable_only`` keeps only world and
    experience facts, which are the ones a soft revoke can act on.
    ``volatile_only`` keeps only the facts whose volatile score is above zero, i.e.
    the ones carrying a value likely to have gone stale. ``limit`` is
    capped at ``review.MAX_PAGE``, read at call time (see ``run``).
    """
    def run():
        review = _review()
        # The page ceiling is read here, not at import: a request cap and a backend
        # cap that drift apart would become a 400 the page cannot explain, and a
        # broken ``review.py`` must cost this route rather than the whole API.
        if not 1 <= limit <= review.MAX_PAGE:
            raise review.ReviewError(400, f"limit must be 1..{review.MAX_PAGE}")
        with _decisions_lock:
            decisions = _bank_decisions(_read_decisions(), bank)
        return review.list_review(
            _review_client(), bank, limit=limit, offset=offset, state=state, sort=sort,
            q=q, review_only=review_only, curatable_only=curatable_only,
            volatile_only=volatile_only, decisions=decisions,
        )

    return _review_call(run)


@router.get("/review/{bank}/decisions")
def review_decisions(bank: str):
    """Return one bank's local review ledger: verdict counts and the total.

    Read-only, local, and no Hindsight request: the ledger is the user's own
    record of what they already checked.
    """
    def run():
        review = _review()
        review.check_bank(bank)
        with _decisions_lock:
            entry = _bank_decisions(_read_decisions(), bank)
        return {"bank": bank, "counts": review.decision_counts(entry), "total": len(entry)}

    return _review_call(run)


@router.post("/review/{bank}/decisions")
def review_record_decision(bank: str, payload: Annotated[dict | None, Body()] = None):
    """Record one user decision for a fact: ``still true``, ``outdated`` or ``cannot tell``.

    The ledger is local bookkeeping only: nothing is sent to Hindsight and no request
    here checks that the fact exists upstream. The id is validated syntactically
    (``review.check_memory_id``) and the ``hash`` comes from the client, pinning the
    entry to the text the user actually saw -- a wrong hash is accepted and simply
    never hides the fact, so it stays in the queue. The ledger is bounded
    (``review.MAX_DECISIONS`` per bank) and the fact's text is never stored.
    """
    def run():
        review = _review()
        review.check_bank(bank)
        data = payload or {}
        with _decisions_lock:
            ledger = _read_decisions()
            entry = review.record_decision(
                _bank_decisions(ledger, bank),
                data.get("id"),
                data.get("verdict"),
                data.get("hash"),
            )
            entry = review.prune_decisions(entry)
            ledger = dict(ledger)
            ledger[bank] = entry
            _write_decisions(ledger)
        return {
            "decision": entry.get(str(data.get("id") or "")),
            "counts": review.decision_counts(entry),
        }

    return _review_call(run)


@router.post("/review/{bank}/memories/{memory_id}/revoke")
def review_revoke(bank: str, memory_id: str, payload: Annotated[dict, Body()]):
    """Soft-invalidate one fact (PATCH state=invalidated). Returns ``previous``."""
    return _review_call(
        lambda: _review().revoke(
            _review_client(), bank, memory_id, (payload or {}).get("reason")
        )
    )


@router.post("/review/{bank}/memories/{memory_id}/restore")
def review_restore(bank: str, memory_id: str):
    """Revert an invalidated fact to valid. Returns ``previous``."""
    return _review_call(
        lambda: _review().restore(_review_client(), bank, memory_id)
    )


@router.post("/review/{bank}/memories/{memory_id}/update")
def review_update(bank: str, memory_id: str, payload: Annotated[dict, Body()]):
    """Rewrite one fact's text. Returns ``previous`` so the UI can undo."""
    return _review_call(
        lambda: _review().update(
            _review_client(), bank, memory_id, (payload or {}).get("text")
        )
    )


@router.post("/review/{bank}/memories")
def review_create(bank: str, payload: Annotated[dict, Body()] = None):
    """Create one memory fact via POST /memories (async=False). Returns ``created``."""
    data = payload or {}
    return _review_call(
        lambda: _review().create_memory(
            _review_client(),
            bank,
            data.get("content"),
            data.get("context"),
            data.get("tags"),
        )
    )


@router.post("/review/check-prompt")
def review_check_prompt(payload: Annotated[dict, Body()]):
    """Build the verification prompt for 1..10 selected facts."""
    return _review_call(
        lambda: {"prompt": _review().build_check_prompt((payload or {}).get("facts"))}
    )


@router.post("/review/verdicts")
def review_verdicts(payload: Annotated[dict, Body()]):
    """Parse the agent's reply into per-fact verdicts (display only, never acted on)."""
    data = payload or {}
    ids = data.get("ids") if isinstance(data.get("ids"), list) else []
    return _review_call(
        lambda: {"verdicts": _review().parse_verdicts(data.get("reply"), ids)}
    )


@router.post("/review/{bank}/apply")
def review_apply(bank: str, payload: Annotated[Any, Body()] = None):
    """Apply a document-style batch of memory edits.

    Each entry is either ``{id, text}`` (rewrite) or
    ``{id, state: "invalidated", reason: "removed in document editor"}``
    (soft revoke). Nothing is created or physically deleted.
    """
    return _review_call(
        lambda: _review().apply(_review_client(), bank, payload)
    )


# ==== end Memory review =========================================================


# ==== recall preamble (Desktop memory pane) =====================================
# The pane shows the recalled text verbatim. Hindsight opens it with a configured
# recall preamble the user does not need to read, and the pane cannot know where it
# ends without the value: it lives in ``<HERMES_HOME>/hindsight/config.json``.
# Display sugar only -- no route here calls Hindsight or touches the API key.

RECALL_PREAMBLE_CAP = 4000


@router.get("/recall-preamble")
def recall_preamble():
    """Return the configured Hindsight recall preamble, or ``""``.

    Read-only and fail-open: a missing or unreadable file, bad JSON, a non-object
    config, a non-string value or an empty one all read as ``""``; the route never
    raises. The value is returned verbatim, capped at ``RECALL_PREAMBLE_CAP``
    characters because a config can hold anything.
    """
    try:
        path = pathlib.Path(_core()._hermes_home()) / "hindsight" / "config.json"
        config = json.loads(path.read_text("utf-8"))
    except Exception:  # noqa: BLE001 - display sugar must never turn into a 500
        return {"preamble": ""}
    if not isinstance(config, dict):
        return {"preamble": ""}
    preamble = config.get("recall_prompt_preamble")
    if not isinstance(preamble, str) or not preamble:
        return {"preamble": ""}
    return {"preamble": preamble[:RECALL_PREAMBLE_CAP]}


# ==== end recall preamble =======================================================


@router.get("/health")
def health():
    """Return audit-store statistics without exposing its filesystem path."""
    try:
        stats = _core().injection_audit_stats()
        _api_url, bank_id, _api_key = _core().resolve_endpoint(_core()._hermes_home())
        return {"ok": True, "bank": bank_id or "", **stats}
    except Exception as exc:
        raise HTTPException(
            status_code=500, detail="could not read the injection audit"
        ) from exc


@router.get("/history")
def history(
    session_id: str = Query("", max_length=256),
    limit: int = Query(200, ge=1, le=1000),
):
    """Return one session's retained injections, newest first.

    ``session_id`` may be either the live session id or the durable task id;
    the audit store matches both, plus aliases recorded before a session-id
    rotation.
    """
    try:
        return _core().read_injection_history(session_id, limit=limit)
    except Exception as exc:
        raise HTTPException(
            status_code=500, detail="could not read injection history"
        ) from exc


@router.get("/injections")
def injections(
    session: str = Query("", max_length=256),
    session_id: str = Query("", max_length=256),
    limit: int = Query(200, ge=1, le=1000),
):
    """Read injection history from Hermes' read-only ``state.db``.

    Returns ``{session, entries, source, reason?}``. Never raises a 500:
    an unreadable database yields ``source: "unavailable"``.
    """
    value = session or session_id
    try:
        return _core().read_state_db_injections(value, limit=limit)
    except Exception as exc:
        return {
            "session": value,
            "entries": [],
            "source": "unavailable",
            "reason": "state.db unreadable: %s" % exc,
        }


@router.get("/sessions")
def sessions(limit: int = Query(100, ge=1, le=500)):
    """Return recent retained sessions, newest first."""
    try:
        return _core().list_injection_sessions(limit=limit)
    except Exception as exc:
        raise HTTPException(
            status_code=500, detail="could not list injection sessions"
        ) from exc
