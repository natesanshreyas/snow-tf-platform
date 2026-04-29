"""
github_search_agent.py — Agent 2: GitHub Search Agent

Resolves which GitHub repo contains the Terraform module for each infra unit
type in the plan. Eliminates hardcoded repo names — the agent searches the
org at runtime and reasons about which repo to use.

Responsibilities:
- For each unit type from the Planner's output, call the GitHub code search API
- Receive the candidate repos per type as tool context
- Reason about the best match (consider environment, repo name, recency)
- Return a {unit_type: repo_name} mapping used by the TF Generator Agent

Output contract: Dict[str, str] — {unit_type: repo_name}
Missing entries mean no repo was found; the TF agent will skip those units.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from typing import Dict, List, Optional

from autogen_agentchat.agents import AssistantAgent
from autogen_ext.models.openai import AzureOpenAIChatCompletionClient

from agents.mock_client import get_model_client

from mcp.github import search_module_repos

logger = logging.getLogger(__name__)

_SYSTEM_PROMPT = """\
You are a GitHub repository resolver for a Terraform automation platform.

Given a list of infrastructure module types and the GitHub search results
showing which repos contain each module, select the best repo for each type.

=== SELECTION RULES ===
1. If only one repo matches a type, use it.
2. If multiple repos match, prefer the one whose name most closely relates to
   the infrastructure domain (e.g. "terraform-azure-modules" for azure types).
3. If a repo name contains "prod" or "stable", prefer it over "dev" or "test".
4. If still ambiguous, pick the repo that appears most frequently across types
   (consistency — keep all modules in the same repo if possible).
5. If no repo matches a type, omit that type from the output.

=== OUTPUT FORMAT ===
Output ONLY a JSON object — no prose, no markdown:
{
  "unit_type": "repo_name",
  ...
}
"""


def _make_model_client():
    return get_model_client()


def _parse_mapping(raw: str) -> Dict[str, str]:
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", raw, re.DOTALL)
        if match:
            return json.loads(match.group(0))
        raise ValueError(f"GH Search Agent returned non-JSON: {raw[:300]}")


async def run_github_search_agent(
    unit_types: List[str],
    org: str,
) -> Dict[str, str]:
    """Search GitHub for repos containing each module type and return a mapping.

    Args:
        unit_types: List of module type strings from the Planner's Plan units
                    (e.g. ["resource_group", "postgres_flex", "storage_account"])
        org:        GitHub org to search within

    Returns:
        {unit_type: repo_name} — only includes types where a repo was found.
    """
    # Search all types concurrently
    search_results: Dict[str, List[str]] = {}
    results = await asyncio.gather(
        *[search_module_repos(t, org) for t in unit_types],
        return_exceptions=True,
    )
    for unit_type, result in zip(unit_types, results):
        if isinstance(result, Exception):
            logger.warning("search failed for %s: %s", unit_type, result)
            search_results[unit_type] = []
        else:
            search_results[unit_type] = result

    # Short-circuit: if every type resolved unambiguously, skip the LLM call
    mapping: Dict[str, str] = {}
    ambiguous: Dict[str, List[str]] = {}
    for t, repos in search_results.items():
        if len(repos) == 1:
            mapping[t] = repos[0]
        elif len(repos) > 1:
            ambiguous[t] = repos
        # len == 0: omit

    if not ambiguous:
        logger.info("GH Search Agent: all types resolved without LLM — %s", mapping)
        return mapping

    # Build prompt for the LLM to resolve ambiguous cases
    model_client = _make_model_client()
    search_summary = json.dumps(search_results, indent=2)
    user_content = (
        f"GitHub org: {org}\n\n"
        f"Search results (unit_type → candidate repos):\n{search_summary}\n\n"
        f"Select the best repo for each type and return the JSON mapping."
    )

    agent = AssistantAgent(
        name="github_search_agent",
        system_message=_SYSTEM_PROMPT,
        model_client=model_client,
    )

    result = await agent.run(task=user_content)
    raw = result.messages[-1].content
    logger.info("GH Search Agent output: %s", raw[:300])

    llm_mapping = _parse_mapping(raw)

    # Merge: unambiguous results + LLM-resolved ambiguous ones
    mapping.update(llm_mapping)
    return mapping
