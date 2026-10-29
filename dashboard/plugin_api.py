"""Dashboard API for the Hindsight memory pane.

The Desktop half reads the plugin's local injection audit through the
read-only ``/health``, ``/history`` and ``/sessions`` routes (the opt-in memory
review routes below are the only ones that write, via Hindsight's soft
``PATCH /memories/{id}``). The audit is written by ``core.record_injection`` during
``pre_llm_call``; this module deliberately reuses that module's reader so both
halves agree on the retention and alias rules.

Mounted by Hermes at ``/api/plugins/hermes-hindsight-plus/``. No route accepts
a filesystem path: the only inputs are a session identifier and a bounded
limit.
"""

from __future__ import annotations

import importlib.util
import pathlib
import sys
import threading
from typing import Annotated

from fastapi import APIRouter, Body, HTTPException, Query

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
    limit: int = Query(100, ge=1, le=200),
    offset: int = Query(0, ge=0),
    state: str = Query("valid", max_length=16),
    sort: str = Query("staleness", max_length=16),
    q: str = Query("", max_length=200),
):
    """List a bank's facts with ``age_score`` and ``volatile_score``, sorted."""
    return _review_call(
        lambda: _review().list_review(
            _review_client(), bank, limit=limit, offset=offset, state=state, sort=sort, q=q
        )
    )


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


# ==== end Memory review =========================================================


@router.get("/health")
def health():
    """Return audit-store statistics without exposing its filesystem path."""
    try:
        stats = _core().injection_audit_stats()
        return {"ok": True, **stats}
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


@router.get("/sessions")
def sessions(limit: int = Query(100, ge=1, le=500)):
    """Return recent retained sessions, newest first."""
    try:
        return _core().list_injection_sessions(limit=limit)
    except Exception as exc:
        raise HTTPException(
            status_code=500, detail="could not list injection sessions"
        ) from exc
