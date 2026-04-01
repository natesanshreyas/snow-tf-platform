"""
planner_agent.py — Agent 1: Planner Agent (Microsoft Agent Framework)

THIS FILE USES MICROSOFT AGENT FRAMEWORK (agent-framework RC5).
It is the ONLY place in the Azure workflow that calls an LLM for planning.

Responsibilities:
- Interpret the ServiceNow request in natural language
- Identify infra units to be created (resource_group, postgres_flex, storage, etc.)
- Apply invariant constraints (e.g. "postgres never goes in app_rg")
- Identify unresolved questions that require human input
- Optionally re-plan after environment scan results are injected

Output contract: Plan object (defined in orchestrator/models.py)
The agent NEVER generates Terraform — that is Agent 3's job.

NOTE: Microsoft Agent Framework is currently RC5 (pre-GA as of March 2026).
The import paths and API below reflect the RC5 package structure.
Exact method signatures may shift before GA — check the migration guide:
https://learn.microsoft.com/en-us/agent-framework/migration-guide/
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any, Dict, Optional

# ---------------------------------------------------------------------------
# Microsoft Agent Framework imports
# RC5 package: pip install agent-framework --pre
# ---------------------------------------------------------------------------
from agent_framework import SingleAgentRuntime                          # type: ignore[import]
from agent_framework.agents import AssistantAgent                       # type: ignore[import]
from agent_framework.messages import TextMessage                        # type: ignore[import]
from agent_framework.models import AzureOpenAIChatCompletionClient      # type: ignore[import]

from orchestrator.models import Plan, PlanUnit, SnowRequest, UnitConstraints

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# System prompt
# ---------------------------------------------------------------------------

_SYSTEM_PROMPT = """\
You are an Azure infrastructure planner. Given a ServiceNow provisioning request,
you decompose it into discrete infrastructure units and identify any constraints or
unresolved questions.

Rules:
1. Output ONLY a JSON object matching the Plan schema — no prose, no Terraform.
2. Each unit must have: id (snake_case), type, depends_on (list of ids), constraints (object).
3. Postgres units must NEVER be placed in an app resource group (forbidden_rg: app_rg).
4. If you are uncertain about any parameter, add it to the `questions` list.
5. Resource groups must always be listed before resources that depend on them.
6. If environment scan results are provided, use them to avoid creating resources
   that already exist — instead note them as references.

Output schema:
{
  "units": [
    {
      "id": "string",
      "type": "string",
      "depends_on": ["string"],
      "constraints": {
        "required_rg": "string or null",
        "forbidden_rg": "string or null",
        "location": "string or null"
      }
    }
  ],
  "questions": ["string"]
}
"""

# ---------------------------------------------------------------------------
# Model client factory
# ---------------------------------------------------------------------------


def _make_model_client() -> AzureOpenAIChatCompletionClient:
    """Build the Azure OpenAI client for the planner agent.

    TODO: Replace env var reads with Key Vault / managed identity for production.
    """
    return AzureOpenAIChatCompletionClient(
        azure_deployment=os.environ["AZURE_OPENAI_DEPLOYMENT_NAME"],
        azure_endpoint=os.environ["AZURE_OPENAI_ENDPOINT"],
        api_version=os.environ.get("AZURE_OPENAI_API_VERSION", "2024-10-21"),
        # TODO: Switch to DefaultAzureCredential for production
        api_key=os.environ.get("AZURE_OPENAI_API_KEY"),
    )


# ---------------------------------------------------------------------------
# Plan parser
# ---------------------------------------------------------------------------


def _parse_plan(raw: str) -> Plan:
    """Parse the agent's JSON output into a typed Plan object.

    Raises ValueError if the output does not match the expected schema.
    """
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        # Try to extract JSON from prose if the model wrapped it
        import re
        match = re.search(r"\{.*\}", raw, re.DOTALL)
        if match:
            data = json.loads(match.group(0))
        else:
            raise ValueError(f"Planner returned non-JSON output: {raw[:300]}") from exc

    units = []
    for u in data.get("units", []):
        c = u.get("constraints", {})
        units.append(PlanUnit(
            id=u["id"],
            type=u["type"],
            depends_on=u.get("depends_on", []),
            constraints=UnitConstraints(
                required_rg=c.get("required_rg"),
                forbidden_rg=c.get("forbidden_rg"),
                location=c.get("location"),
                extra={k: v for k, v in c.items() if k not in ("required_rg", "forbidden_rg", "location")},
            ),
        ))

    return Plan(
        units=units,
        questions=data.get("questions", []),
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def run_planner_agent(
    request: SnowRequest,
    scan_results: Optional[Dict[str, Any]] = None,
    human_answers: Optional[Dict[str, str]] = None,
) -> Plan:
    """Run the Planner Agent and return a Plan.

    This is an MAF-orchestrated agent call. The runtime manages the message
    loop; we send one user message and expect one structured JSON response.

    Args:
        request:       The approved ServiceNow request.
        scan_results:  Optional environment scan output (Agent 2). If provided,
                       injected as context so the planner can avoid duplicates.
        human_answers: Optional answers to previously raised questions.
                       Injected so the planner can finalize an incomplete plan.

    Returns:
        Plan with units and any remaining questions.
    """
    model_client = _make_model_client()

    # MAF: create an assistant agent with a system prompt and no tools.
    # The planner reasons entirely from context — it calls no external APIs.
    agent = AssistantAgent(
        name="azure_planner",
        system_message=_SYSTEM_PROMPT,
        model_client=model_client,
    )

    # Build the user message — assemble all context into a single prompt
    user_content = _build_user_message(request, scan_results, human_answers)

    # MAF: SingleAgentRuntime runs the agent for one request/response cycle
    runtime = SingleAgentRuntime()
    runtime.register_agent(agent)
    await runtime.start()

    response = await runtime.send_message(
        TextMessage(content=user_content, source="orchestrator"),
        recipient="azure_planner",
    )
    await runtime.stop()

    raw_output = response.content if hasattr(response, "content") else str(response)
    logger.info("Planner agent raw output (first 300 chars): %s", raw_output[:300])

    plan = _parse_plan(raw_output)
    logger.info(
        "Plan: %d units, %d questions",
        len(plan.units), len(plan.questions),
    )
    return plan


def _build_user_message(
    request: SnowRequest,
    scan_results: Optional[Dict[str, Any]],
    human_answers: Optional[Dict[str, str]],
) -> str:
    """Assemble the full context message for the planner agent."""
    parts = [
        f"ServiceNow Ticket: {request.ticket_id}",
        f"Requested by: {request.requested_by}",
        f"Short description: {request.short_description}",
        f"Description:\n{request.description}",
    ]

    if scan_results:
        parts.append(
            "\n--- Environment Scan Results ---\n"
            + json.dumps(scan_results, indent=2)
        )

    if human_answers:
        parts.append(
            "\n--- Human Answers to Previous Questions ---\n"
            + "\n".join(f"Q: {q}\nA: {a}" for q, a in human_answers.items())
        )

    return "\n\n".join(parts)
