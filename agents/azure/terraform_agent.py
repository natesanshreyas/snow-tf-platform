"""
terraform_agent.py — Agent 3: Terraform Generator Agent (Azure Agent Framework SDK)

Uses the Azure Agent Framework SDK (agent-framework RC5).

Responsibilities (per PlanUnit):
  1. Fetch module README at runtime → know required vs optional variables (Req 7)
  2. Fetch latest module commit hash → pin source to exact version (Req 5)
  3. Generate main.tf + variables.tf using module blocks only (Req 6)
  4. Run 3 evaluators (correctness, security, compliance)
  5. Retry generation with evaluator feedback if any evaluator fails (up to 2 retries)
  6. Return TerraformOutput — file push and PR creation are handled by workflow.py

This agent does NOT push files.  workflow.py calls push_unit_terraform after
run_terraform_agent succeeds, keeping push logic separate from codegen logic.
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass
from typing import Callable, List, Optional

from agent_framework import SingleAgentRuntime                           # type: ignore[import]
from agent_framework.agents import AssistantAgent                        # type: ignore[import]
from agent_framework.messages import TextMessage                         # type: ignore[import]
from agent_framework.models import AzureOpenAIChatCompletionClient       # type: ignore[import]

from mcp.github import get_latest_module_version, read_module_readme
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
    module_version: str = "main"   # commit SHA used in module source


# ---------------------------------------------------------------------------
# Evaluator type alias
# Plain functions injected for testability — not MAF agents.
# ---------------------------------------------------------------------------

EvaluatorFn = Callable[[str, str, str], EvaluatorResult]

# ---------------------------------------------------------------------------
# System prompt
# ---------------------------------------------------------------------------

_SYSTEM_PROMPT = """\
You are a Terraform code generator for Azure infrastructure.

Given an infrastructure unit spec, a module README, and the module's latest
commit SHA, generate syntactically correct main.tf and variables.tf.

=== RULES ===

1. Use module blocks exclusively — never raw resource blocks. (ENFORCED by evaluator)
2. Pin module source to the provided commit SHA:
     source = "git::https://github.com/{org}/{modules_repo}.git//modules/{type}?ref={sha}"
3. Consult the README to identify ALL required variables and their types.
   Do not omit required variables. Do not invent variable names.
4. Apply all constraints from the unit spec exactly.
5. Tag every resource with: cost_center (from ticket), ticket_id, environment.
6. Storage account names: ≤24 chars, lowercase, alphanumeric only.
7. Default location: eastus2 unless overridden by constraints.location.
8. If evaluation feedback is provided, fix ONLY the reported issues — do not
   regenerate the entire file from scratch.

=== OUTPUT FORMAT ===
Output ONLY this JSON — no prose, no markdown fences:
{
  "main_tf": "<full HCL content as a single JSON string>",
  "variables_tf": "<full HCL content as a single JSON string>"
}
"""


# ---------------------------------------------------------------------------
# Model client
# ---------------------------------------------------------------------------


def _build_model_client() -> AzureOpenAIChatCompletionClient:
    return AzureOpenAIChatCompletionClient(
        azure_deployment=os.environ["AZURE_OPENAI_DEPLOYMENT_NAME"],
        azure_endpoint=os.environ["AZURE_OPENAI_ENDPOINT"],
        api_version=os.environ.get("AZURE_OPENAI_API_VERSION", "2025-04-01-preview"),
        api_key=os.environ.get("AZURE_OPENAI_API_KEY"),
    )


# ---------------------------------------------------------------------------
# Module metadata helpers (Req 5 + 7)
# ---------------------------------------------------------------------------


async def _fetch_module_context(
    unit_type: str,
    org: str,
    modules_repo: str,
) -> tuple[str, str]:
    """Return (readme_text, commit_sha) for a module type.

    Both are fetched from GitHub at runtime so the agent always uses the
    latest version and knows the exact variable contract.
    """
    readme, sha = await _gather(
        read_module_readme(unit_type, org, modules_repo),
        get_latest_module_version(unit_type, org, modules_repo),
    )
    return readme, sha


async def _gather(coro1, coro2):
    """Run two coroutines concurrently."""
    import asyncio
    return await asyncio.gather(coro1, coro2)


# ---------------------------------------------------------------------------
# Prompt builder
# ---------------------------------------------------------------------------


def _build_user_message(
    unit: PlanUnit,
    ticket_id: str,
    environment: str,
    org: str,
    modules_repo: str,
    module_readme: str,
    module_sha: str,
    feedback: Optional[str],
) -> str:
    unit_spec = {
        "id": unit.id,
        "type": unit.type,
        "constraints": {
            "required_rg": unit.constraints.required_rg,
            "forbidden_rg": unit.constraints.forbidden_rg,
            "location": unit.constraints.location,
            **unit.constraints.extra,
        },
    }

    parts = [
        f"Generate Terraform for the following infrastructure unit.",
        f"\n=== Unit spec ===\n{json.dumps(unit_spec, indent=2)}",
        f"\n=== Context ===",
        f"ticket_id: {ticket_id}",
        f"environment: {environment}",
        f"module source org: {org}",
        f"modules repo: {modules_repo}",
        f"module commit SHA (use as ?ref= value): {module_sha}",
        f"\n=== Module README (defines required and optional variables) ===\n{module_readme}",
    ]

    if feedback:
        parts.append(
            f"\n=== Evaluator feedback from previous attempt — fix these issues ===\n{feedback}"
        )

    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Output parser
# ---------------------------------------------------------------------------


def _parse_terraform_output(raw: str) -> tuple[str, str]:
    """Parse {main_tf, variables_tf} JSON from agent output."""
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


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def run_terraform_agent(
    unit: PlanUnit,
    run: WorkflowRun,
    evaluators: List[EvaluatorFn],
    org: str,
    modules_repo: str,
) -> TerraformOutput:
    """Generate and evaluate Terraform for a single infra unit.

    Args:
        unit:         The PlanUnit to generate Terraform for. unit.resolved_repo
                      (set by GH Search Agent) takes precedence over modules_repo.
        run:          The active WorkflowRun (ticket context + environment).
        evaluators:   List of evaluator functions (correctness, security, compliance).
                      Each: (main_tf, variables_tf, ticket_id) -> EvaluatorResult
        org:          GitHub org owning the modules repo.
        modules_repo: Fallback repo if unit.resolved_repo is not set.

    Returns:
        TerraformOutput with HCL content, evaluation results, and module SHA used.
        Does NOT push files — workflow.py handles that after all units pass.
    """
    ticket_id = run.request.ticket_id if run.request else "UNKNOWN"
    environment = run.request.environment if run.request else "dev"

    # GH Search Agent resolves the repo per unit; fall back to env var default
    repo = unit.resolved_repo or modules_repo

    # Fetch module README and latest commit SHA in parallel
    module_readme, module_sha = await _fetch_module_context(unit.type, org, repo)
    logger.info(
        "unit=%s module=%s sha=%s readme=%d chars",
        unit.id, unit.type, module_sha[:7] if module_sha != "main" else "main", len(module_readme),
    )

    model_client = _build_model_client()
    agent = AssistantAgent(
        name="azure_tf_generator",
        system_message=_SYSTEM_PROMPT,
        model_client=model_client,
    )

    feedback: Optional[str] = None
    last_output: Optional[TerraformOutput] = None

    for attempt in range(1, MAX_EVAL_RETRIES + 2):
        user_message = _build_user_message(
            unit=unit,
            ticket_id=ticket_id,
            environment=environment,
            org=org,
            modules_repo=modules_repo,
            module_readme=module_readme,
            module_sha=module_sha,
            feedback=feedback,
        )

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

        # Run all evaluators (plain function calls — not MAF agents)
        eval_results = [ev(main_tf, variables_tf, ticket_id) for ev in evaluators]
        passed = all(r.passed for r in eval_results)

        last_output = TerraformOutput(
            unit_id=unit.id,
            main_tf=main_tf,
            variables_tf=variables_tf,
            eval_results=eval_results,
            passed=passed,
            module_version=module_sha,
        )

        if passed:
            logger.info(
                "unit=%s terraform passed all evaluators on attempt %d (sha=%s)",
                unit.id, attempt, module_sha[:7] if module_sha != "main" else "main",
            )
            break

        failed = [r for r in eval_results if not r.passed]
        feedback = "\n".join(f"{r.evaluator} ({r.score}/5): {r.reason}" for r in failed)
        logger.warning(
            "unit=%s eval failed attempt %d/%d — feedback: %s",
            unit.id, attempt, MAX_EVAL_RETRIES + 1, feedback,
        )

        if attempt == MAX_EVAL_RETRIES + 1:
            logger.error("unit=%s exhausted all eval retries", unit.id)
            break

    if not last_output:
        raise RuntimeError(f"unit={unit.id}: no output generated")

    unit.terraform_output = last_output.main_tf
    return last_output
