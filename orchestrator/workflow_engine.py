"""
workflow_engine.py — Deterministic state machine + DAG executor.

THIS FILE CONTAINS NO LLM CALLS.

Responsibilities:
- Accept a finalized Plan and execute its units in dependency order
- Retry failed units up to MAX_RETRIES times
- Pause/resume workflow when human input is required
- Delegate actual unit work to domain workflows (azure, aws, snowflake)
- Never interpret plan semantics — it only executes the graph

The engine is intentionally dumb. All intelligence lives in the agents.
"""

from __future__ import annotations

import asyncio
import logging
from collections import defaultdict, deque
from typing import Callable, Dict, List, Optional

from .models import (
    Plan,
    PlanUnit,
    RequestType,
    SnowRequest,
    UnitStatus,
    WorkflowRun,
    WorkflowStatus,
)

logger = logging.getLogger(__name__)

MAX_RETRIES = 3

# ---------------------------------------------------------------------------
# Type alias for domain workflow callables
# Each domain workflow must expose an async function matching this signature:
#   async def run(request: SnowRequest, run: WorkflowRun) -> WorkflowRun
# ---------------------------------------------------------------------------

DomainWorkflowFn = Callable[[SnowRequest, WorkflowRun], asyncio.Future]


# ---------------------------------------------------------------------------
# In-memory run store (replace with Cosmos DB / Redis in production)
# ---------------------------------------------------------------------------

_runs: Dict[str, WorkflowRun] = {}


def store_run(run: WorkflowRun) -> None:
    """Persist workflow run state. TODO: replace with durable storage."""
    _runs[run.run_id] = run


def load_run(run_id: str) -> Optional[WorkflowRun]:
    """Load workflow run by ID. TODO: replace with durable storage."""
    return _runs.get(run_id)


# ---------------------------------------------------------------------------
# DAG utilities (pure functions — no I/O, no LLM)
# ---------------------------------------------------------------------------


def topological_sort(units: List[PlanUnit]) -> List[List[PlanUnit]]:
    """Return units grouped into execution waves respecting depends_on order.

    Units in the same wave have no dependencies on each other and can run
    concurrently. Each wave must complete before the next starts.

    Raises ValueError if a cycle is detected.
    """
    unit_map: Dict[str, PlanUnit] = {u.id: u for u in units}
    in_degree: Dict[str, int] = defaultdict(int)
    dependents: Dict[str, List[str]] = defaultdict(list)

    for unit in units:
        if unit.id not in in_degree:
            in_degree[unit.id] = 0
        for dep in unit.depends_on:
            in_degree[unit.id] += 1
            dependents[dep].append(unit.id)

    waves: List[List[PlanUnit]] = []
    queue: deque[str] = deque(uid for uid, deg in in_degree.items() if deg == 0)

    while queue:
        wave = []
        next_queue: deque[str] = deque()
        while queue:
            uid = queue.popleft()
            wave.append(unit_map[uid])
            for dependent_id in dependents[uid]:
                in_degree[dependent_id] -= 1
                if in_degree[dependent_id] == 0:
                    next_queue.append(dependent_id)
        waves.append(wave)
        queue = next_queue

    visited = sum(len(w) for w in waves)
    if visited != len(units):
        raise ValueError("Cycle detected in plan dependency graph")

    return waves


# ---------------------------------------------------------------------------
# DAG executor
# ---------------------------------------------------------------------------


async def execute_dag(
    plan: Plan,
    run: WorkflowRun,
    unit_executor: Callable[[PlanUnit, WorkflowRun], asyncio.Future],
) -> bool:
    """Execute all plan units in dependency order with per-unit retry.

    Args:
        plan:          Finalized Plan with units and depends_on relationships.
        run:           Mutable WorkflowRun — unit statuses are updated in place.
        unit_executor: Async callable that executes one unit. Injected by the
                       domain workflow so this function stays domain-agnostic.

    Returns:
        True if all units completed successfully, False if any unit failed
        after exhausting retries.
    """
    waves = topological_sort(plan.units)
    logger.info(
        "run=%s DAG has %d waves, %d total units",
        run.run_id, len(waves), len(plan.units),
    )

    for wave_idx, wave in enumerate(waves):
        logger.info("run=%s executing wave %d (%d units)", run.run_id, wave_idx, len(wave))

        # Units within a wave run concurrently
        results = await asyncio.gather(
            *[_execute_with_retry(unit, run, unit_executor) for unit in wave],
            return_exceptions=True,
        )

        failed = [wave[i] for i, r in enumerate(results) if isinstance(r, Exception) or r is False]
        if failed:
            failed_ids = [u.id for u in failed]
            logger.error("run=%s wave %d failed units: %s", run.run_id, wave_idx, failed_ids)
            run.error = f"Units failed: {failed_ids}"
            return False

    return True


async def _execute_with_retry(
    unit: PlanUnit,
    run: WorkflowRun,
    unit_executor: Callable[[PlanUnit, WorkflowRun], asyncio.Future],
) -> bool:
    """Execute a single unit with up to MAX_RETRIES attempts.

    Returns True on success, False after exhausting retries.
    """
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            unit.status = UnitStatus.RUNNING
            unit.retry_count = attempt - 1
            store_run(run)

            await unit_executor(unit, run)

            unit.status = UnitStatus.COMPLETE
            store_run(run)
            logger.info("run=%s unit=%s completed on attempt %d", run.run_id, unit.id, attempt)
            return True

        except Exception as exc:
            unit.error = str(exc)
            logger.warning(
                "run=%s unit=%s attempt %d/%d failed: %s",
                run.run_id, unit.id, attempt, MAX_RETRIES, exc,
            )
            if attempt == MAX_RETRIES:
                unit.status = UnitStatus.FAILED
                store_run(run)
                return False

            await asyncio.sleep(2 ** attempt)  # exponential backoff

    return False


# ---------------------------------------------------------------------------
# Workflow engine
# ---------------------------------------------------------------------------


class WorkflowEngine:
    """Deterministic orchestrator. Routes requests, manages state, drives DAG.

    No LLM calls. No business logic. No cloud-specific knowledge.
    All intelligence is delegated to domain workflows and their agents.
    """

    def __init__(self, workflows: Dict[RequestType, DomainWorkflowFn]) -> None:
        """
        Args:
            workflows: Map of RequestType → async domain workflow function.
                       Injected at startup so the engine stays decoupled.
        """
        self._workflows = workflows

    async def start(self, request: SnowRequest) -> WorkflowRun:
        """Start a new workflow run for an approved ServiceNow request.

        Creates a WorkflowRun, stores it, and hands off to the domain workflow.
        Returns the run (likely still EXECUTING — callers should poll or await).
        """
        run = WorkflowRun(request=request, request_type=request.request_type)
        run.transition(WorkflowStatus.EXECUTING)
        store_run(run)

        logger.info(
            "Starting run=%s type=%s ticket=%s",
            run.run_id, request.request_type.value, request.ticket_id,
        )

        workflow_fn = self._workflows.get(request.request_type)
        if not workflow_fn:
            run.error = f"No workflow registered for {request.request_type}"
            run.transition(WorkflowStatus.FAILED)
            store_run(run)
            return run

        try:
            await workflow_fn(request, run)
        except Exception as exc:
            logger.exception("run=%s unhandled error: %s", run.run_id, exc)
            run.error = str(exc)
            run.transition(WorkflowStatus.FAILED)
            store_run(run)

        return run

    async def resume_cost_approval(self, run_id: str, approved: bool) -> WorkflowRun:
        """Resume a workflow paused at HITL 2 (cost + quota gate)."""
        run = load_run(run_id)
        if not run:
            raise ValueError(f"Unknown run_id: {run_id}")

        if run.status != WorkflowStatus.WAITING_FOR_COST_APPROVAL:
            raise ValueError(f"run={run_id} is not at cost approval step (status={run.status})")

        run.cost_approved = approved
        run.transition(WorkflowStatus.EXECUTING)
        store_run(run)

        logger.info("Cost approval for run=%s: %s", run_id, "APPROVED" if approved else "REJECTED")

        workflow_fn = self._workflows.get(run.request_type)
        try:
            await workflow_fn(run.request, run)
        except Exception as exc:
            logger.exception("run=%s cost-approval resume failed: %s", run_id, exc)
            run.error = str(exc)
            run.transition(WorkflowStatus.FAILED)
            store_run(run)

        return run

    async def resume(self, run_id: str, human_answers: Dict[str, str]) -> WorkflowRun:
        """Resume a paused workflow after human answers arrive from ServiceNow.

        The orchestrator re-attaches the answers to the run and re-invokes the
        domain workflow, which will re-plan with the new context.
        """
        run = load_run(run_id)
        if not run:
            raise ValueError(f"Unknown run_id: {run_id}")

        if run.status != WorkflowStatus.WAITING_FOR_HUMAN_INPUT:
            raise ValueError(f"run={run_id} is not paused (status={run.status})")

        run.human_answers.update(human_answers)
        run.pending_questions.clear()
        run.transition(WorkflowStatus.EXECUTING)
        store_run(run)

        logger.info("Resuming run=%s with %d answers", run_id, len(human_answers))

        workflow_fn = self._workflows.get(run.request_type)
        try:
            await workflow_fn(run.request, run)
        except Exception as exc:
            logger.exception("run=%s resume failed: %s", run_id, exc)
            run.error = str(exc)
            run.transition(WorkflowStatus.FAILED)
            store_run(run)

        return run
