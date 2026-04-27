"""
workflow.py — AzureInfraWorkflow: coordinates agents, HITL, and DAG execution.

THIS FILE COORDINATES AGENTS BUT IS NOT ITSELF AN AGENT.
It is the domain workflow registered with WorkflowEngine for AZURE_INFRA.

Full execution sequence
-----------------------
0.  Create ONE branch: feature/{ticket_id} in the application's terraform repo
1.  Router Agent (Agent 1) — already ran in server.py; determined this is AZURE_INFRA
2.  Planner Agent (Agent 2) → breaks ticket into typed infra units + constraints
3.  HITL pause if Planner raised questions:
      - POSTs questions to SNOW ticket as a work note
      - returns; WorkflowEngine.resume() re-enters with human_answers
4.  Environment scan (deterministic) → what already exists in Azure
5.  Planner Agent (resume path) → finalizes plan with scan results + human answers
6.  GH Search Agent (Agent 3) → searches GitHub org, resolves repo per unit type
7.  DAG executor (topological wave order):
      - units within a wave run concurrently via asyncio.gather
      - each unit: TF Generator Agent (Agent 4) → evaluate → push files to branch
8.  Create ONE PR covering all units: {environment}/{ticket_id}/
9.  POST PR URL back to SNOW ticket as a work note
"""

from __future__ import annotations

import logging
import os
from typing import List

from agents.azure.cost_quota import run_cost_quota_check
from agents.azure.environment_scan import (
    _extract_resource_names_from_plan_units,
    scan_environment,
)
from agents.azure.planner_agent import run_planner_agent
from agents.azure.terraform_agent import TerraformOutput, run_terraform_agent
from agents.github_search_agent import run_github_search_agent
from evaluators.terraform_compliance import evaluate_compliance
from evaluators.terraform_correctness import evaluate_correctness
from evaluators.terraform_security import evaluate_security
from mcp.github import create_pull_request, create_ticket_branch, push_unit_terraform
from mcp.servicenow import update_ticket_with_pr, write_cost_approval_to_ticket, write_questions_to_ticket
from orchestrator.models import PlanUnit, SnowRequest, WorkflowRun, WorkflowStatus
from orchestrator.workflow_engine import execute_dag, store_run

logger = logging.getLogger(__name__)

_EVALUATORS = [evaluate_correctness, evaluate_security, evaluate_compliance]


def _github_org() -> str:
    return os.environ.get("GITHUB_ORG", "your-org")


def _modules_repo() -> str:
    return os.environ.get("GITHUB_MODULES_REPO", "terraform-modules")


# ---------------------------------------------------------------------------
# Domain workflow entry point
# ---------------------------------------------------------------------------


async def run(request: SnowRequest, run: WorkflowRun) -> None:
    """Full Azure infrastructure workflow.

    Entry point registered with WorkflowEngine for RequestType.AZURE_INFRA.
    Mutates `run` in place; calls store_run after each state transition.
    """
    org = _github_org()
    modules_repo = _modules_repo()

    # ── Step 0: Create ONE branch for this ticket ────────────────────────────
    # Branch is created once and reused across all units and any re-runs.
    if not run.branch_name:
        logger.info("run=%s Step 0: creating branch for ticket=%s repo=%s",
                    run.run_id, request.ticket_id, request.github_repo)
        run.branch_name = await create_ticket_branch(
            ticket_id=request.ticket_id,
            org=org,
            repo=request.github_repo,
        )
        store_run(run)
        logger.info("run=%s branch=%s", run.run_id, run.branch_name)

    # ── Step 1: Initial plan ─────────────────────────────────────────────────
    logger.info("run=%s Step 1: initial planner", run.run_id)
    plan = await run_planner_agent(
        request=request,
        human_answers=run.human_answers if run.human_answers else None,
    )
    run.plan = plan
    store_run(run)

    # ── Step 2: HITL pause if planner raised questions ───────────────────────
    # Only pause when there are unresolved questions AND no answers yet.
    # On resume, human_answers is populated and we skip straight to Step 3.
    if plan.questions and not run.human_answers:
        logger.info(
            "run=%s Step 2: pausing for HITL — %d question(s)",
            run.run_id, len(plan.questions),
        )
        run.pending_questions = plan.questions
        run.transition(WorkflowStatus.WAITING_FOR_HUMAN_INPUT)
        store_run(run)

        await write_questions_to_ticket(
            sys_id=request.sys_id,
            ticket_id=request.ticket_id,
            run_id=run.run_id,
            questions=plan.questions,
        )
        # Execution stops here.
        # WorkflowEngine.resume() will re-call this function with
        # run.human_answers populated and branch_name already set.
        return

    # ── Step 3: Environment scan ─────────────────────────────────────────────
    logger.info("run=%s Step 3: environment scan", run.run_id)
    resource_names = _extract_resource_names_from_plan_units(plan.units)
    scan_results = await scan_environment(
        resource_names=resource_names,
        subscription_id=os.environ.get("AZURE_SUBSCRIPTION_ID"),
    )
    plan.scan_results = scan_results
    store_run(run)

    # ── Step 4: Re-plan with scan context + human answers ───────────────────
    # Uses UserProxyAgent (HITL path) when human_answers are present so the
    # agent framework sees a proper human turn in the conversation.
    logger.info("run=%s Step 4: re-planning (HITL=%s)", run.run_id, bool(run.human_answers))
    final_plan = await run_planner_agent(
        request=request,
        scan_results=scan_results,
        human_answers=run.human_answers if run.human_answers else None,
    )
    final_plan.finalized = True
    run.plan = final_plan
    store_run(run)

    # ── Step 5: Cost + quota check ───────────────────────────────────────────
    # Only run when we haven't already paused for cost approval.
    # On resume from HITL 2, cost_quota_result is already populated.
    if run.cost_quota_result is None:
        logger.info("run=%s Step 5: cost + quota check", run.run_id)
        location = (
            next(
                (u.constraints.location for u in final_plan.units if u.constraints.location),
                "eastus2",
            )
        )
        run.cost_quota_result = await run_cost_quota_check(
            units=final_plan.units,
            subscription_id=os.environ.get("AZURE_SUBSCRIPTION_ID"),
            location=location,
        )
        store_run(run)

    # ── Step 5.5: HITL 2 — cost + quota approval gate ───────────────────────
    if run.cost_approved is None:
        logger.info(
            "run=%s Step 5.5: pausing for cost approval — $%.0f/mo quota_ok=%s",
            run.run_id,
            run.cost_quota_result.total_monthly_usd,
            run.cost_quota_result.quota_ok,
        )
        run.transition(WorkflowStatus.WAITING_FOR_COST_APPROVAL)
        store_run(run)

        await write_cost_approval_to_ticket(
            sys_id=request.sys_id,
            ticket_id=request.ticket_id,
            run_id=run.run_id,
            total_monthly_usd=run.cost_quota_result.total_monthly_usd,
            unit_breakdown=[
                {"unit_id": e.unit_id, "unit_type": e.unit_type, "monthly_usd": e.monthly_usd}
                for e in run.cost_quota_result.unit_estimates
            ],
            quota_detail=run.cost_quota_result.quota_detail,
            quota_ok=run.cost_quota_result.quota_ok,
        )
        return

    if run.cost_approved is False:
        logger.info("run=%s cost approval rejected", run.run_id)
        run.error = "Provisioning cancelled at cost/quota review step"
        run.transition(WorkflowStatus.FAILED)
        store_run(run)
        return

    # ── Step 6: GH Search Agent — resolve module repo per unit type ─────────
    unit_types = list(dict.fromkeys(u.type for u in final_plan.units))
    logger.info("run=%s Step 6: GH Search Agent resolving repos for %s", run.run_id, unit_types)
    repo_map = await run_github_search_agent(unit_types=unit_types, org=org)
    for unit in final_plan.units:
        if unit.type in repo_map:
            unit.resolved_repo = repo_map[unit.type]
            logger.info("run=%s unit=%s resolved_repo=%s", run.run_id, unit.id, unit.resolved_repo)
        else:
            logger.warning("run=%s unit=%s no repo found — will fall back to default", run.run_id, unit.id)
    store_run(run)

    # ── Step 7: DAG execution — parallel within dependency waves ─────────────
    logger.info(
        "run=%s Step 7: DAG execution (%d units)", run.run_id, len(final_plan.units)
    )

    success = await execute_dag(
        plan=final_plan,
        run=run,
        unit_executor=_make_unit_executor(run, org, modules_repo),
    )

    if not success:
        run.transition(WorkflowStatus.FAILED)
        store_run(run)
        logger.error("run=%s FAILED: %s", run.run_id, run.error)
        return

    # ── Step 8: Create ONE PR for all provisioned units ──────────────────────
    unit_ids: List[str] = [u.id for u in final_plan.units]
    logger.info("run=%s Step 8: creating PR for %d units", run.run_id, len(unit_ids))

    pr_url = await create_pull_request(
        ticket_id=request.ticket_id,
        environment=request.environment,
        org=org,
        repo=request.github_repo,
        branch=run.branch_name,
        unit_ids=unit_ids,
        description=request.short_description,
    )
    run.pr_url = pr_url
    store_run(run)

    # ── Step 9: POST PR URL back to SNOW ticket ───────────────────────────────
    summary = (
        f"Provisioned {len(unit_ids)} resource(s) for {request.application or request.ticket_id} "
        f"in environment '{request.environment}'.\n"
        f"Resources: {', '.join(unit_ids)}"
    )
    await update_ticket_with_pr(
        sys_id=request.sys_id,
        ticket_id=request.ticket_id,
        pr_url=pr_url,
        summary=summary,
    )

    run.transition(WorkflowStatus.COMPLETE)
    store_run(run)
    logger.info("run=%s COMPLETE pr=%s", run.run_id, pr_url)


# ---------------------------------------------------------------------------
# Unit executor — generates TF and pushes files for one PlanUnit
# ---------------------------------------------------------------------------


def _make_unit_executor(run: WorkflowRun, org: str, modules_repo: str):
    """Return an async callable for one DAG unit.

    Closure captures run, org, modules_repo so execute_dag stays domain-agnostic.
    Each unit:
      1. Runs TF Generator Agent → evaluates (correctness / security / compliance)
      2. Pushes main.tf + variables.tf into {environment}/{ticket_id}/{unit_id}/
    """
    async def _execute_unit(unit: PlanUnit, _run: WorkflowRun) -> None:
        output: TerraformOutput = await run_terraform_agent(
            unit=unit,
            run=_run,
            evaluators=_EVALUATORS,
            org=org,
            modules_repo=modules_repo,
        )

        if not output.passed:
            raise RuntimeError(
                f"unit={unit.id} failed evaluation after "
                f"{len(output.eval_results)} attempt(s)"
            )

        # Push files into the correct environment folder (Req 8)
        ticket_id = _run.request.ticket_id if _run.request else "UNKNOWN"
        environment = _run.request.environment if _run.request else "dev"
        github_repo = _run.request.github_repo if _run.request else ""

        await push_unit_terraform(
            branch=_run.branch_name,
            ticket_id=ticket_id,
            unit_id=unit.id,
            environment=environment,
            main_tf=output.main_tf,
            variables_tf=output.variables_tf,
            org=org,
            repo=github_repo,
        )
        logger.info(
            "run=%s unit=%s pushed to %s/%s/%s/",
            _run.run_id, unit.id, environment, ticket_id, unit.id,
        )

    return _execute_unit
