"""
terraform_agent.py — Agent 3: Terraform Generator Agent (Microsoft Agent Framework)

THIS FILE USES MICROSOFT AGENT FRAMEWORK (agent-framework RC5).

Responsibilities:
- For each infra unit in the plan:
  - Select the correct Terraform module (stubbed MCP read)
  - Generate main.tf + variables.tf
  - Run evaluators (correctness, security, compliance)
  - Retry generation with evaluator feedback if any evaluator fails
- Return PR metadata (stub)

This agent runs AFTER the planner and environment scan are complete.
It operates on one unit at a time — the orchestrator DAG controls order.

Evaluators are injected as plain functions (not agents). They are called
from within this agent's execution loop, not by the MAF runtime.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from typing import Callable, List, Optional

from agent_framework import SingleAgentRuntime                          # type: ignore[import]
from agent_framework.agents import AssistantAgent                       # type: ignore[import]
from agent_framework.messages import TextMessage                        # type: ignore[import]
from agent_framework.models import AzureOpenAIChatCompletionClient      # type: ignore[import]

from mcp.github import push_terraform_files
from orchestrator.models import EvaluatorResult, PlanUnit, WorkflowRun

logger = logging.getLogger(__name__)

MAX_EVAL_RETRIES = 2

# ---------------------------------------------------------------------------
# Output contract
# ---------------------------------------------------------------------------


@dataclass
class TerraformOutput:
    """Generated Terraform for one infra unit."""

    unit_id: str
    main_tf: str
    variables_tf: str
    eval_results: List[EvaluatorResult]
    passed: bool


# ---------------------------------------------------------------------------
# System prompt
# ---------------------------------------------------------------------------

_SYSTEM_PROMPT = """\
You are a Terraform code generator for Azure infrastructure.

Given an infrastructure unit specification and a Terraform module example,
generate syntactically correct main.tf and variables.tf using module blocks.

Rules:
1. Use module blocks — never raw resource blocks.
2. Apply all constraints from the unit spec exactly.
3. Tag every resource with: cost_center (from ticket), ticket_id, environment.
4. Storage account names must be ≤24 chars, lowercase, alphanumeric only.
5. Default location: eastus2 unless overridden by constraints.
6. If evaluation feedback is provided, fix ONLY the reported issues.

Output ONLY JSON:
{
  "main_tf": "<full HCL content>",
  "variables_tf": "<full HCL content>"
}
"""

# ---------------------------------------------------------------------------
# Evaluator type alias
# Evaluators are plain functions — NOT agents. Injected for testability.
# ---------------------------------------------------------------------------

EvaluatorFn = Callable[[str, str, str], EvaluatorResult]


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def run_terraform_agent(
    unit: PlanUnit,
    run: WorkflowRun,
    evaluators: List[EvaluatorFn],
) -> TerraformOutput:
    """Generate and evaluate Terraform for a single infra unit.

    Args:
        unit:       The PlanUnit to generate Terraform for.
        run:        The active WorkflowRun (used for ticket context and pr push).
        evaluators: List of evaluator functions injected by the workflow.
                    Each must match signature: (main_tf, variables_tf, ticket_id) -> EvaluatorResult

    Returns:
        TerraformOutput with HCL and evaluation results.

    Raises:
        RuntimeError if all eval retries are exhausted.
    """
    model_client = _build_model_client()

    agent = AssistantAgent(
        name="azure_tf_generator",
        system_message=_SYSTEM_PROMPT,
        model_client=model_client,
    )

    module_example = await _fetch_module_example(unit.type)
    ticket_id = run.request.ticket_id if run.request else "UNKNOWN"

    feedback: Optional[str] = None
    last_output: Optional[TerraformOutput] = None

    for attempt in range(1, MAX_EVAL_RETRIES + 2):
        user_message = _build_user_message(unit, ticket_id, module_example, feedback)

        runtime = SingleAgentRuntime()
        runtime.register_agent(agent)
        await runtime.start()

        response = await runtime.send_message(
            TextMessage(content=user_message, source="orchestrator"),
            recipient="azure_tf_generator",
        )
        await runtime.stop()

        raw = response.content if hasattr(response, "content") else str(response)
        main_tf, variables_tf = _parse_terraform_output(raw)

        # Run all evaluators (plain function calls — not MAF)
        eval_results = [ev(main_tf, variables_tf, ticket_id) for ev in evaluators]
        passed = all(r.passed for r in eval_results)

        last_output = TerraformOutput(
            unit_id=unit.id,
            main_tf=main_tf,
            variables_tf=variables_tf,
            eval_results=eval_results,
            passed=passed,
        )

        if passed:
            logger.info("unit=%s terraform passed evaluation on attempt %d", unit.id, attempt)
            break

        failed = [r for r in eval_results if not r.passed]
        feedback = "\n".join(f"{r.evaluator} ({r.score}/5): {r.reason}" for r in failed)
        logger.warning("unit=%s eval failed attempt %d — retrying. Feedback: %s", unit.id, attempt, feedback)

        if attempt == MAX_EVAL_RETRIES + 1:
            logger.error("unit=%s exhausted eval retries", unit.id)
            break

    if not last_output:
        raise RuntimeError(f"unit={unit.id}: no output generated")

    # Push to GitHub via MCP (stub)
    if last_output.passed:
        pr_url = await push_terraform_files(
            ticket_id=ticket_id,
            unit_id=unit.id,
            main_tf=last_output.main_tf,
            variables_tf=last_output.variables_tf,
        )
        run.pr_url = pr_url
        logger.info("unit=%s PR created: %s", unit.id, pr_url)

    unit.terraform_output = last_output.main_tf
    return last_output


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _build_model_client() -> AzureOpenAIChatCompletionClient:
    """Build the Azure OpenAI client. TODO: use managed identity in production."""
    return AzureOpenAIChatCompletionClient(
        azure_deployment=os.environ["AZURE_OPENAI_DEPLOYMENT_NAME"],
        azure_endpoint=os.environ["AZURE_OPENAI_ENDPOINT"],
        api_version=os.environ.get("AZURE_OPENAI_API_VERSION", "2024-10-21"),
        api_key=os.environ.get("AZURE_OPENAI_API_KEY"),
    )


async def _fetch_module_example(unit_type: str) -> str:
    """Fetch a Terraform module example via MCP (stubbed).

    TODO: Replace with real GitHub MCP call to fetch module README/example.
    """
    # TODO: call mcp.github.read_file(repo="terraform-modules", path=f"examples/{unit_type}/main.tf")
    return f"# TODO: real module example for {unit_type}\n# Use module source = '../modules/{unit_type}'"


def _build_user_message(
    unit: PlanUnit,
    ticket_id: str,
    module_example: str,
    feedback: Optional[str],
) -> str:
    """Build the prompt for the TF generator agent."""
    parts = [
        f"Generate Terraform for the following infra unit:",
        f"\nUnit spec:\n{json.dumps({
            'id': unit.id,
            'type': unit.type,
            'constraints': {
                'required_rg': unit.constraints.required_rg,
                'forbidden_rg': unit.constraints.forbidden_rg,
                'location': unit.constraints.location,
            }
        }, indent=2)}",
        f"\nTicket ID (use as tag): {ticket_id}",
        f"\nModule example:\n{module_example}",
    ]

    if feedback:
        parts.append(f"\nEvaluation feedback from previous attempt — fix these issues:\n{feedback}")

    return "\n".join(parts)


def _parse_terraform_output(raw: str) -> tuple[str, str]:
    """Parse {main_tf, variables_tf} JSON from agent output."""
    import re
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", raw, re.DOTALL)
        if not match:
            raise ValueError(f"TF agent returned non-JSON: {raw[:300]}")
        data = json.loads(match.group(0))

    main_tf = data.get("main_tf", "")
    variables_tf = data.get("variables_tf", "")

    if not main_tf:
        raise ValueError("TF agent returned empty main_tf")

    return main_tf, variables_tf
