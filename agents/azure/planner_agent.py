"""
planner_agent.py — Agent 1: Planner Agent (Azure Agent Framework SDK)

Uses the Azure Agent Framework SDK (agent-framework RC5) for orchestration.

Responsibilities:
- Interpret the ServiceNow request and decompose it into infra units
- Identify dependency order (resource group must precede child resources)
- Apply invariant constraints (e.g. postgres never in app_rg)
- Raise a HITL question for each ambiguous resource group decision
- Re-plan after human answers are injected via UserProxyAgent

HITL paths
----------
Initial run (no human_answers):
    Uses SingleAgentRuntime — planner returns a Plan that may contain
    questions.  If questions are present, workflow.py pauses the run and
    writes them to the ServiceNow ticket.

Resume run (human_answers populated):
    Uses RoundRobinGroupChat[planner, human_proxy] so the agent framework
    sees a proper human turn.  UserProxyAgent replays stored answers;
    planner finalises the plan with no remaining questions.

Output contract: Plan (orchestrator/models.py)
The agent NEVER generates Terraform — that is Agent 3's job.
"""

from __future__ import annotations

import json
import logging
import os
import re
from typing import Any, Dict, List, Optional

# ---------------------------------------------------------------------------
# Azure Agent Framework SDK imports (agent-framework RC5)
# ---------------------------------------------------------------------------
from autogen_agentchat.agents import AssistantAgent, UserProxyAgent
from autogen_agentchat.teams import RoundRobinGroupChat
from autogen_agentchat.conditions import MaxMessageTermination
from autogen_ext.models.openai import AzureOpenAIChatCompletionClient

from agents.mock_client import get_model_client
from orchestrator.models import Plan, PlanUnit, SnowRequest, UnitConstraints

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# System prompt
# ---------------------------------------------------------------------------

_SYSTEM_PROMPT = """\
You are an Azure infrastructure planner. Given a ServiceNow provisioning request,
decompose it into discrete infrastructure units with dependency ordering, then
identify constraints and unresolved questions.

=== RULES ===

1. Output ONLY a JSON object matching the Plan schema — no prose, no Terraform.
2. Every unit must have: id (snake_case), type, depends_on (list of ids), constraints.
3. Resource groups MUST appear before any resource that depends on them.
4. Postgres units must NEVER be placed in an app resource group (forbidden_rg: "app_rg").
5. If environment scan shows a resource group already exists, raise a HITL question:
     "Resource group '{name}' already exists. Use existing (A) or create new (B)?"
6. Add to `questions` any parameter that is ambiguous or requires human confirmation.
7. If human answers are provided, incorporate them and output a finalized plan with
   an empty `questions` list. Append "PLAN_FINALIZED" on the last line.

=== OUTPUT SCHEMA ===
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
# Model client
# ---------------------------------------------------------------------------


def _make_model_client():
    return get_model_client()


# ---------------------------------------------------------------------------
# Plan parser
# ---------------------------------------------------------------------------


def _parse_plan(raw: str) -> Plan:
    """Parse agent JSON output into a typed Plan. Strips PLAN_FINALIZED marker."""
    raw = raw.replace("PLAN_FINALIZED", "").strip()
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", raw, re.DOTALL)
        if match:
            data = json.loads(match.group(0))
        else:
            raise ValueError(f"Planner returned non-JSON: {raw[:300]}")

    units: List[PlanUnit] = []
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
                extra={k: v for k, v in c.items()
                       if k not in ("required_rg", "forbidden_rg", "location")},
            ),
        ))

    return Plan(units=units, questions=data.get("questions", []))


# ---------------------------------------------------------------------------
# User message builder
# ---------------------------------------------------------------------------


def _build_user_message(
    request: SnowRequest,
    scan_results: Optional[Dict[str, Any]],
    human_answers: Optional[Dict[str, str]],
) -> str:
    parts = [
        f"ServiceNow Ticket: {request.ticket_id}",
        f"Application: {request.application or '(not specified)'}",
        f"Environment: {request.environment}",
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
            "\n--- Human Answers (incorporate these and finalize the plan) ---\n"
            + "\n".join(f"Q: {q}\nA: {a}" for q, a in human_answers.items())
            + "\n\nOutput the finalized plan with an empty questions list, "
              "then append PLAN_FINALIZED on a new line."
        )

    return "\n\n".join(parts)


# ---------------------------------------------------------------------------
# Single-turn planning (initial run — no human answers)
# ---------------------------------------------------------------------------


async def _run_planner_single_turn(
    model_client: AzureOpenAIChatCompletionClient,
    request: SnowRequest,
    scan_results: Optional[Dict[str, Any]],
) -> Plan:
    """Run one planning turn. Returns a Plan that may contain questions."""
    agent = AssistantAgent(
        name="azure_planner",
        system_message=_SYSTEM_PROMPT,
        model_client=model_client,
    )

    user_content = _build_user_message(request, scan_results, None)
    result = await agent.run(task=user_content)

    raw = result.messages[-1].content
    logger.info("Planner (initial) output (first 300 chars): %s", raw[:300])
    return _parse_plan(raw)


# ---------------------------------------------------------------------------
# HITL resume path — uses UserProxyAgent + RoundRobinGroupChat
# ---------------------------------------------------------------------------


async def _run_planner_with_hitl(
    model_client: AzureOpenAIChatCompletionClient,
    request: SnowRequest,
    scan_results: Optional[Dict[str, Any]],
    human_answers: Dict[str, str],
) -> Plan:
    """Resume planning with stored human answers via UserProxyAgent.

    The agent framework runtime sees a proper human turn (UserProxyAgent)
    rather than answers injected into the system prompt.  This keeps the
    conversation history semantically correct.

    Flow:
      1. Planner receives full context (ticket + scan + Q&A) — outputs plan with questions
      2. UserProxyAgent returns stored answers (one answer per exchange)
      3. Planner finalises plan (empty questions) and appends PLAN_FINALIZED
      4. MaxMessageTermination(6) stops the chat
    """
    answers_iter = iter(human_answers.values())

    async def _stored_input_fn(prompt: str, cancellation_token=None) -> str:
        """Return the next pre-stored human answer, or signal completion."""
        try:
            answer = next(answers_iter)
            logger.info("UserProxyAgent returning stored answer for prompt: %s…", prompt[:80])
            return answer
        except StopIteration:
            return "PLAN_FINALIZED"

    planner = AssistantAgent(
        name="azure_planner",
        system_message=_SYSTEM_PROMPT,
        model_client=model_client,
    )
    human_proxy = UserProxyAgent(
        name="human_approver",
        input_func=_stored_input_fn,
    )

    team = RoundRobinGroupChat(
        participants=[planner, human_proxy],
        termination_condition=MaxMessageTermination(max_messages=6),
    )

    user_content = _build_user_message(request, scan_results, human_answers)
    result = await team.run(task=user_content)

    planner_messages = [
        m for m in result.messages
        if getattr(m, "source", None) == "azure_planner"
    ]

    if not planner_messages:
        raise ValueError("Planner produced no messages in HITL group chat")

    raw = planner_messages[-1].content
    logger.info("Planner (HITL) final output (first 300 chars): %s", raw[:300])

    plan = _parse_plan(raw)
    plan.finalized = True
    return plan


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def run_planner_agent(
    request: SnowRequest,
    scan_results: Optional[Dict[str, Any]] = None,
    human_answers: Optional[Dict[str, str]] = None,
) -> Plan:
    """Run the Planner Agent and return a Plan.

    Args:
        request:       The approved ServiceNow request.
        scan_results:  Environment scan output (what already exists in Azure).
                       Injected so the planner raises HITL questions for
                       existing resource groups.
        human_answers: Stored answers from the ServiceNow work note.
                       When present, uses UserProxyAgent (HITL resume path).

    Returns:
        Plan with units, dependency order, and any unresolved questions.
    """
    model_client = _make_model_client()

    if human_answers:
        logger.info(
            "run_planner_agent: HITL resume path (%d answers)", len(human_answers)
        )
        return await _run_planner_with_hitl(model_client, request, scan_results, human_answers)

    logger.info("run_planner_agent: initial single-turn path")
    return await _run_planner_single_turn(model_client, request, scan_results)
