"""
server.py — FastAPI webhook entrypoint.

Two inbound webhook routes:
  POST /webhook/snow/approval  — new approved RITM from ServiceNow
  POST /webhook/snow/update    — human answers to pending questions

Demo routes (no cloud credentials needed):
  POST /demo/submit            — start a simulated provisioning run
  POST /demo/resume/{run_id}   — supply HITL answer and resume simulation

Cloud routing uses the AI Router Agent (LLM reads the ticket and decides
azure/aws/snowflake). Falls back to keyword heuristic if LLM is unavailable.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any, Dict

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from .models import RequestType, SnowRequest, WorkflowRun, WorkflowStatus
from .workflow_engine import WorkflowEngine, load_run, store_run

logger = logging.getLogger(__name__)

app = FastAPI(title="Snow → Terraform Orchestrator")

# ---------------------------------------------------------------------------
# CORS — allow the WorkbenchIQ Next.js dev server
# ---------------------------------------------------------------------------

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:3000",
        "http://localhost:3001",
        os.environ.get("FRONTEND_URL", ""),
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


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
# Request type router — AI-based
# ---------------------------------------------------------------------------

_REQUEST_TYPE_MAP: Dict[str, RequestType] = {
    "azure":     RequestType.AZURE_INFRA,
    "aws":       RequestType.AWS_INFRA,
    "snowflake": RequestType.SNOWFLAKE_INFRA,
}


async def _route_request(payload: Dict[str, Any]) -> RequestType:
    """Use the Router Agent (LLM) to determine which cloud this ticket targets."""
    from .router_agent import route_ticket

    short = payload.get("short_description", "")
    description = payload.get("description", "")
    cloud, reasoning = await route_ticket(short, description)

    logger.info("Router Agent → cloud=%s  reason=%s", cloud, reasoning)

    if cloud not in _REQUEST_TYPE_MAP:
        raise HTTPException(
            status_code=422,
            detail=f"Router Agent returned unrecognised cloud: {cloud!r}",
        )
    return _REQUEST_TYPE_MAP[cloud]


# ---------------------------------------------------------------------------
# Shared serialization helper
# ---------------------------------------------------------------------------


def _serialize_run(run: WorkflowRun) -> dict:
    """Convert WorkflowRun to a JSON-safe dict for API responses."""
    units = []
    if run.plan:
        for u in run.plan.units:
            units.append({
                "id": u.id,
                "type": u.type,
                "wave": u.wave,
                "status": u.status.value,
                "eval_scores": u.eval_scores,
                "module_info": u.module_info,
                "resolved_repo": u.resolved_repo,
                "error": u.error,
                "constraints": {
                    "required_rg": u.constraints.required_rg,
                    "location":    u.constraints.location,
                    "extra":       dict(u.constraints.extra),
                } if u.constraints else None,
            })

    steps = [
        {
            "id": s.id,
            "label": s.label,
            "status": s.status,
            "detail": s.detail,
            "started_at": s.started_at,
            "finished_at": s.finished_at,
        }
        for s in run.steps
    ]

    mcp_calls = [
        {
            "id":             c.id,
            "step_id":        c.step_id,
            "server":         c.server,
            "tool":           c.tool,
            "reasoning":      c.reasoning,
            "input_summary":  c.input_summary,
            "output_summary": c.output_summary,
            "status":         c.status,
            "timestamp":      c.timestamp,
            "duration_ms":    c.duration_ms,
        }
        for c in run.mcp_calls
    ]

    return {
        "run_id": run.run_id,
        "status": run.status.value,
        "cloud": run.cloud,
        "ticket_id": run.request.ticket_id if run.request else None,
        "application": run.request.application if run.request else None,
        "environment": run.request.environment if run.request else None,
        "pr_url": run.pr_url,
        "branch_name": run.branch_name,
        "pending_questions": run.pending_questions,
        "hitl_question": run.hitl_question,
        "cost_quota": (
            {
                "total_monthly_usd": run.cost_quota_result.total_monthly_usd,
                "vcpus_needed": run.cost_quota_result.vcpus_needed,
                "vcpus_available": run.cost_quota_result.vcpus_available,
                "quota_ok": run.cost_quota_result.quota_ok,
                "quota_detail": run.cost_quota_result.quota_detail,
                "unit_estimates": [
                    {"unit_id": e.unit_id, "unit_type": e.unit_type, "monthly_usd": e.monthly_usd}
                    for e in run.cost_quota_result.unit_estimates
                ],
            }
            if run.cost_quota_result else None
        ),
        "cost_approved": run.cost_approved,
        "error": run.error,
        "steps": steps,
        "units": units,
        "mcp_calls": mcp_calls,
    }


# ---------------------------------------------------------------------------
# Webhook: new approved RITM
# ---------------------------------------------------------------------------


@app.post("/webhook/snow/approval")
async def receive_approval(request: Request) -> JSONResponse:
    """Receive a post-approval ServiceNow webhook."""
    payload: Dict[str, Any] = await request.json()

    if payload.get("approval") != "approved":
        raise HTTPException(status_code=400, detail="Ticket is not in approved state")

    ticket_id = payload.get("number") or payload.get("ticket_id")
    if not ticket_id:
        raise HTTPException(status_code=422, detail="Missing ticket number in payload")

    request_type = await _route_request(payload)

    application = (
        payload.get("u_application") or payload.get("application") or ""
    ).lower().replace(" ", "-")

    environment = (
        payload.get("u_environment") or payload.get("environment")
        or os.getenv("DEFAULT_ENVIRONMENT", "dev")
    ).lower()

    github_repo = (
        payload.get("u_terraform_repo")
        or (f"terraform-{application}" if application else os.getenv("GITHUB_TERRAFORM_REPO", "terraform-modules"))
    )

    snow_request = SnowRequest(
        ticket_id=ticket_id,
        short_description=payload.get("short_description", ""),
        description=payload.get("description", ""),
        requested_by=payload.get("requested_by", {}).get("value", "unknown"),
        approval_state=payload.get("approval", ""),
        request_type=request_type,
        application=application,
        environment=environment,
        github_repo=github_repo,
        sys_id=payload.get("sys_id", ""),
        raw=payload,
    )

    engine = get_engine()
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
    """Receive human answers from ServiceNow after a pause."""
    payload: Dict[str, Any] = await request.json()

    run_id = payload.get("run_id")
    answers = payload.get("answers", {})

    if not run_id:
        raise HTTPException(status_code=422, detail="Missing run_id")

    run = load_run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail=f"Unknown run_id: {run_id}")

    engine = get_engine()

    if run.status == WorkflowStatus.WAITING_FOR_HUMAN_INPUT:
        asyncio.create_task(engine.resume(run_id, answers))
        logger.info("Resuming HITL-1 run=%s with %d answers", run_id, len(answers))
        return JSONResponse(status_code=202, content={"message": "Resuming", "run_id": run_id})

    if run.status == WorkflowStatus.WAITING_FOR_COST_APPROVAL:
        raw = str(list(answers.values())[0]).strip().upper() if answers else ""
        approved = raw == "APPROVE"
        asyncio.create_task(engine.resume_cost_approval(run_id, approved))
        logger.info("Cost approval run=%s: %s", run_id, "APPROVED" if approved else "REJECTED")
        return JSONResponse(status_code=202, content={"message": "Resuming", "run_id": run_id})

    raise HTTPException(
        status_code=409,
        detail=f"run is not paused (current status: {run.status})",
    )


# ---------------------------------------------------------------------------
# Demo routes — no cloud credentials required
# ---------------------------------------------------------------------------


@app.post("/demo/submit")
async def demo_submit(request: Request) -> JSONResponse:
    """Start a simulated provisioning run for demo purposes.

    The target cloud is determined agentically by the Router Agent reading
    the ticket description — callers do NOT specify cloud explicitly.

    Body:
      {
        "ticket_id":        "RITM0041293",
        "application":      "payments-api",
        "environment":      "prod",
        "short_description": "Provision Azure storage for payments API",
        "description":      "We need a PostgreSQL Flexible Server and Blob Storage..."
      }
    """
    from .demo_simulation import simulate_workflow
    from .router_agent import route_ticket

    payload: Dict[str, Any] = await request.json()

    ticket_id         = payload.get("ticket_id", "DEMO-001")
    application       = payload.get("application", "demo-app").lower().replace(" ", "-")
    environment       = payload.get("environment", "dev").lower()
    short_description = payload.get("short_description", "")
    description       = payload.get("description", "")

    # ── Agentic routing: LLM reads ticket and determines cloud ───────────────
    cloud, routing_reasoning = await route_ticket(short_description, description)
    logger.info("Router → cloud=%s  reason=%s", cloud, routing_reasoning)

    request_type_map = {
        "azure":     RequestType.AZURE_INFRA,
        "aws":       RequestType.AWS_INFRA,
        "snowflake": RequestType.SNOWFLAKE_INFRA,
    }

    snow_req = SnowRequest(
        ticket_id=ticket_id,
        short_description=short_description or f"[Demo] {cloud.upper()} infra for {application}",
        description=description,
        requested_by="demo-user",
        approval_state="approved",
        request_type=request_type_map[cloud],
        application=application,
        environment=environment,
        github_repo=f"terraform-{application}",
    )

    run = WorkflowRun(request=snow_req, request_type=request_type_map[cloud], cloud=cloud)
    store_run(run)

    asyncio.create_task(simulate_workflow(run, cloud))

    logger.info("Started demo run=%s cloud=%s ticket=%s", run.run_id, cloud, ticket_id)
    return JSONResponse(
        status_code=202,
        content={
            "run_id": run.run_id,
            "cloud": cloud,
            "ticket_id": ticket_id,
            "routing_reasoning": routing_reasoning,
        },
    )


@app.post("/demo/resume/{run_id}")
async def demo_resume(run_id: str, request: Request) -> JSONResponse:
    """Supply HITL answers and resume a paused demo simulation.

    Body: { "answers": { "question text": "A" } }
    """
    from .demo_simulation import resume_simulation

    payload: Dict[str, Any] = await request.json()
    answers: Dict[str, str] = payload.get("answers", {"q": "A"})

    run = load_run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail=f"Unknown run_id: {run_id}")

    if run.status not in (
        WorkflowStatus.WAITING_FOR_HUMAN_INPUT,
        WorkflowStatus.WAITING_FOR_COST_APPROVAL,
    ):
        raise HTTPException(
            status_code=409,
            detail=f"Run is not paused (status: {run.status})",
        )

    signaled = resume_simulation(run_id, answers)
    if not signaled:
        raise HTTPException(status_code=409, detail="No simulation waiting for this run_id")

    logger.info("Resumed demo run=%s", run_id)
    return JSONResponse(status_code=202, content={"message": "Resumed", "run_id": run_id})


# ---------------------------------------------------------------------------
# Status endpoint
# ---------------------------------------------------------------------------


@app.get("/runs/{run_id}")
async def get_run_status(run_id: str) -> JSONResponse:
    """Return current status of a workflow run (includes steps and units for UI)."""
    run = load_run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail=f"Unknown run_id: {run_id}")

    return JSONResponse(content=_serialize_run(run))


@app.get("/health")
async def health() -> JSONResponse:
    return JSONResponse(content={"status": "ok"})
