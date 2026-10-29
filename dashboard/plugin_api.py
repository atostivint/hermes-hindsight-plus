"""Dashboard API for the Hindsight memory pane.

The Desktop half reads the plugin's local injection audit through these
read-only routes. The audit is written by ``core.record_injection`` during
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

from fastapi import APIRouter, HTTPException, Query

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
