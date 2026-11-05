"""Backend routes for the Desktop pane. Mounted by Hermes at /api/plugins/backtrack/."""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter

from .runtime import Runtime

router = APIRouter()
_rt = Runtime()


@router.get("/timeline")
async def timeline(root: Optional[str] = None, limit: int = 50):
    return _rt.api_timeline(root, limit)


@router.get("/diff")
async def diff(id: int, root: Optional[str] = None):
    return _rt.api_diff(id, root)


@router.post("/restore")
async def restore(body: dict):
    try:
        rid = int(body.get("id"))
    except (TypeError, ValueError):
        return {"ok": False, "error": "id must be an integer"}
    return _rt.api_restore(rid, body.get("root"))


@router.post("/redo")
async def redo(body: dict = None):
    return _rt.api_redo((body or {}).get("root"))
