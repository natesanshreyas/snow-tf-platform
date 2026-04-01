"""
server.py — FastAPI webhook entrypoint.

THIS FILE CONTAINS NO LLM CALLS.

Two inbound webhook routes:
  POST /webhook/snow/approval  — new approved RITM from ServiceNow
  POST /webhook/snow/update    — human answers to pending questions

The server parses the payload, determines request type, and hands off
to WorkflowEngine. All routing logic is deterministic.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from .models import RequestType, SnowRequest, WorkflowStatus
from .workflow_engine import WorkflowEngine, load_run

logger = logging.getLogger(__name__)

app = FastAPI(title="Snow → Terraform Orchestrator")


# ---------------------------------------------------------------------------
# Lazy engine initialisation (workflows injected at startup)
# ---------------------------------------------------------------------------

_engine: WorkflowEngine | None = None


def get_engine() -> WorkflowEngine:
    """Return the singleton WorkflowEngine. Raises if not initialised."""
    if _engine is None:
        raise RuntimeError("WorkflowEngine not initialised — call init_engine() at startup")
    return _engine


def init_engine(engine: WorkflowEngine) -> None:
    """Inject the configured WorkflowEngine at startup."""
    global _engine
    _engine = engine


# ---------------------------------------------------------------------------
# Request type router (deterministic — no LLM)
# ---------------------------------------------------------------------------

_KEYWORD_MAP: Dict[str, RequestType] = {
    "azure":     RequestType.AZURE_INFRA,
    "aws":       RequestType.AWS_INFRA,
    "snowflake": RequestType.SNOWFLAKE_INFRA,
}


def _infer_request_type(payload: Dict[str, Any]) -> RequestType:
    """Determine domain from the RITM description using keyword matching.

    TODO: Replace with ServiceNow catalog item field lookup for production.
    """
    text = (
        payload.get("short_description", "") + " " +
        payload.get("description", "")
    ).lower()

    for keyword, rtype in _KEYWORD_MAP.items():
        if keyword in text:
            return rtype

    # Default: check explicit 'cloud_provider' field if present
    cloud = payload.get("cloud_provider", "").lower()
    if cloud in _KEYWORD_MAP:
        return _KEYWORD_MAP[cloud]

    raise HTTPException(
        status_code=422,
        detail="Cannot determine request type. Add 'azure', 'aws', or 'snowflake' to the ticket.",
    )


# ---------------------------------------------------------------------------
# Webhook: new approved RITM
# ---------------------------------------------------------------------------


@app.post("/webhook/snow/approval")
async def receive_approval(request: Request) -> JSONResponse:
    """Receive a post-approval ServiceNow webhook.

    ServiceNow Business Rule should call this endpoint when:
      current.approval == 'approved' && previous.approval != 'approved'

    Returns immediately with run_id. Execution is async.
    """
    payload: Dict[str, Any] = await request.json()

    # Validate approval state
    if payload.get("approval") != "approved":
        raise HTTPException(status_code=400, detail="Ticket is not in approved state")

    ticket_id = payload.get("number") or payload.get("ticket_id")
    if not ticket_id:
        raise HTTPException(status_code=422, detail="Missing ticket number in payload")

    request_type = _infer_request_type(payload)

    snow_request = SnowRequest(
        ticket_id=ticket_id,
        short_description=payload.get("short_description", ""),
        description=payload.get("description", ""),
        requested_by=payload.get("requested_by", {}).get("value", "unknown"),
        approval_state=payload.get("approval", ""),
        request_type=request_type,
        raw=payload,
    )

    engine = get_engine()
    # Fire and forget — run executes in background
    import asyncio
    asyncio.create_task(engine.start(snow_request))

    logger.info("Accepted ticket=%s type=%s", ticket_id, request_type.value)
    return JSONResponse(
        status_code=202,
        content={"message": "Accepted", "ticket_id": ticket_id, "request_type": request_type.value},
    )


# ---------------------------------------------------------------------------
# Webhook: human answers to pending questions
# ---------------------------------------------------------------------------


@app.post("/webhook/snow/update")
async def receive_update(request: Request) -> JSONResponse:
    """Receive human answers from ServiceNow after a pause.

    ServiceNow should call this endpoint when a work note is added to a
    ticket that is currently in WAITING_FOR_HUMAN_INPUT state.

    Expected body:
      { "run_id": "...", "answers": { "question text": "answer text", ... } }
    """
    payload: Dict[str, Any] = await request.json()

    run_id = payload.get("run_id")
    answers = payload.get("answers", {})

    if not run_id:
        raise HTTPException(status_code=422, detail="Missing run_id")

    run = load_run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail=f"Unknown run_id: {run_id}")

    if run.status != WorkflowStatus.WAITING_FOR_HUMAN_INPUT:
        raise HTTPException(
            status_code=409,
            detail=f"run is not paused (current status: {run.status})",
        )

    engine = get_engine()
    import asyncio
    asyncio.create_task(engine.resume(run_id, answers))

    logger.info("Resuming run=%s with %d answers", run_id, len(answers))
    return JSONResponse(status_code=202, content={"message": "Resuming", "run_id": run_id})


# ---------------------------------------------------------------------------
# Status endpoint
# ---------------------------------------------------------------------------


@app.get("/runs/{run_id}")
async def get_run_status(run_id: str) -> JSONResponse:
    """Return current status of a workflow run."""
    run = load_run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail=f"Unknown run_id: {run_id}")

    return JSONResponse(content={
        "run_id": run.run_id,
        "status": run.status.value,
        "ticket_id": run.request.ticket_id if run.request else None,
        "pr_url": run.pr_url,
        "pending_questions": run.pending_questions,
        "error": run.error,
    })


@app.get("/health")
async def health() -> JSONResponse:
    return JSONResponse(content={"status": "ok"})
