"""
workflow.py — AzureInfraWorkflow: wires agents + orchestrator DAG.

THIS FILE COORDINATES AGENTS BUT IS NOT ITSELF AN AGENT.
It is the domain workflow registered with the WorkflowEngine.

Execution sequence:
  1. Run Planner Agent (Agent 1) → Plan
  2. If Plan.questions non-empty → pause, write to SNOW, wait for resume
  3. Run Environment Scan (non-agent) → scan_results
  4. Re-run Planner Agent with scan_results + answers → finalized Plan
  5. Hand Plan to DAG executor
  6. For each unit (in dependency order): run TF Generator Agent (Agent 3)
  7. Mark run COMPLETE

The orchestrator.workflow_engine.execute_dag handles steps 5-6 concurrency.
This workflow only needs to implement the unit_executor callable.
"""

from __future__ import annotations

import logging
from typing import Any, Dict

from agents.azure.environment_scan import (
    _extract_resource_names_from_plan_units,
    scan_environment,
)
from agents.azure.planner_agent import run_planner_agent
from agents.azure.terraform_agent import run_terraform_agent
from evaluators.terraform_correctness import evaluate_correctness
from evaluators.terraform_security import evaluate_security
from evaluators.terraform_compliance import evaluate_compliance
from mcp.servicenow import write_questions_to_ticket
from orchestrator.models import PlanUnit, SnowRequest, WorkflowRun, WorkflowStatus
from orchestrator.workflow_engine import execute_dag, store_run

logger = logging.getLogger(__name__)

# Evaluators injected into TF Generator Agent — order does not matter
_EVALUATORS = [evaluate_correctness, evaluate_security, evaluate_compliance]


async def run(request: SnowRequest, run: WorkflowRun) -> None:
    """Full Azure infrastructure workflow.

    Entry point registered with WorkflowEngine for RequestType.AZURE_INFRA.
    Mutates `run` in place and calls store_run after each state transition.
    """

    # ── Step 1: Initial plan ────────────────────────────────────────────────
    logger.info("run=%s Step 1: running planner agent", run.run_id)
    plan = await run_planner_agent(
        request=request,
        human_answers=run.human_answers if run.human_answers else None,
    )
    run.plan = plan
    store_run(run)

    # ── Step 2: Human-in-the-loop pause ─────────────────────────────────────
    if plan.questions and not run.human_answers:
        logger.info("run=%s Plan has %d questions — pausing", run.run_id, len(plan.questions))
        run.pending_questions = plan.questions
        run.transition(WorkflowStatus.WAITING_FOR_HUMAN_INPUT)
        store_run(run)

        # Write questions back to the ServiceNow ticket (MCP stub)
        await write_questions_to_ticket(
            ticket_id=request.ticket_id,
            run_id=run.run_id,
            questions=plan.questions,
        )
        # Execution stops here. WorkflowEngine.resume() will re-call this function
        # with run.human_answers populated.
        return

    # ── Step 3: Environment scan ─────────────────────────────────────────────
    logger.info("run=%s Step 3: environment scan", run.run_id)
    resource_names = _extract_resource_names_from_plan_units(plan.units)
    scan_results = await scan_environment(resource_names)
    plan.scan_results = scan_results
    store_run(run)

    # ── Step 4: Re-plan with scan context ────────────────────────────────────
    logger.info("run=%s Step 4: re-planning with scan results", run.run_id)
    final_plan = await run_planner_agent(
        request=request,
        scan_results=scan_results,
        human_answers=run.human_answers if run.human_answers else None,
    )
    final_plan.finalized = True
    run.plan = final_plan
    store_run(run)

    # ── Steps 5-6: DAG execution ─────────────────────────────────────────────
    logger.info("run=%s Step 5-6: DAG execution (%d units)", run.run_id, len(final_plan.units))

    success = await execute_dag(
        plan=final_plan,
        run=run,
        unit_executor=_make_unit_executor(run),
    )

    # ── Final state ──────────────────────────────────────────────────────────
    if success:
        run.transition(WorkflowStatus.COMPLETE)
        logger.info("run=%s COMPLETE pr=%s", run.run_id, run.pr_url)
    else:
        run.transition(WorkflowStatus.FAILED)
        logger.error("run=%s FAILED: %s", run.run_id, run.error)

    store_run(run)


def _make_unit_executor(run: WorkflowRun):
    """Return an async callable that runs TF Generator Agent for one unit.

    Closure captures `run` so the DAG executor stays domain-agnostic.
    """
    async def _execute_unit(unit: PlanUnit, _run: WorkflowRun) -> None:
        output = await run_terraform_agent(
            unit=unit,
            run=_run,
            evaluators=_EVALUATORS,
        )
        if not output.passed:
            raise RuntimeError(
                f"unit={unit.id} failed evaluation after {len(output.eval_results)} attempts"
            )
    return _execute_unit
