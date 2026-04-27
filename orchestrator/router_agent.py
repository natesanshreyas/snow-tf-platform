"""
router_agent.py — Agentic cloud router.

Analyzes a ServiceNow ticket and determines the target cloud platform.

Primary path:  Azure OpenAI chat completion (if AZURE_OPENAI_* env vars set)
Fallback path: Weighted keyword heuristic (works without any credentials)

Returns (cloud, reasoning_sentence) so the UI can show the agent's thinking.
"""

from __future__ import annotations

import logging
import os
from typing import Optional, Tuple

import httpx

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Heuristic signals per cloud (weighted: longer phrase = more specific = higher weight)
# ---------------------------------------------------------------------------

_SIGNALS: dict[str, list[str]] = {
    "azure": [
        "azure kubernetes service", "azure container instances", "azure data factory",
        "azure devops", "azure sql", "azure storage", "azure function",
        "cosmos db", "blob storage", "service bus", "key vault", "app service",
        "resource group", "azure postgres", "postgres flexible server",
        "azure", "aks", "aci", "vnet", "arm template", "bicep",
    ],
    "aws": [
        "amazon web services", "elastic kubernetes service", "elastic container service",
        "cloudformation", "cloudwatch", "elasticache", "dynamodb",
        "ec2", "rds", "lambda", "s3", "vpc", "ecs", "eks",
        "iam", "route53", "aurora", "kinesis", "glue", "redshift",
        "fargate", "beanstalk", "lightsail", "alb", "elb", "sqs", "sns",
        "aws",
    ],
    "snowflake": [
        "snowflake data cloud", "snowflake warehouse", "snowflake database",
        "snowflake schema", "snowflake stage", "snowflake pipe",
        "snowpark", "snowsight", "snowflake role", "snowflake account",
        "data cloud", "snowflake", "data warehouse",
    ],
}


def _heuristic_route(text: str) -> tuple[str, str]:
    text_lower = text.lower()
    scores: dict[str, int] = {c: 0 for c in _SIGNALS}

    matched: dict[str, list[str]] = {c: [] for c in _SIGNALS}
    for cloud, signals in _SIGNALS.items():
        for signal in signals:
            if signal in text_lower:
                scores[cloud] += len(signal.split())   # multi-word signals score higher
                matched[cloud].append(signal)

    best = max(scores, key=lambda c: scores[c])
    if scores[best] == 0:
        return "azure", "No cloud-specific terms detected; defaulting to Azure IaC workflow."

    hits = matched[best][:3]
    return best, (
        f"Detected {best.upper()} signals in ticket: {', '.join(repr(h) for h in hits)}. "
        f"Routing to {best.upper()} IaC workflow."
    )


# ---------------------------------------------------------------------------
# LLM-based routing via Azure OpenAI
# ---------------------------------------------------------------------------

_SYSTEM = (
    "You are a cloud infrastructure routing agent inside a ServiceNow automation platform. "
    "Your only job is to read a ticket and decide which cloud platform it targets."
)

_USER_TMPL = """\
Analyze the ServiceNow ticket below and determine the target cloud platform.

Output format (two lines, nothing else):
Line 1: exactly one of:  azure  |  aws  |  snowflake
Line 2: one sentence explaining the key signals that led to this decision.

Ticket:
{ticket_text}
"""


async def _llm_route(ticket_text: str) -> Optional[tuple[str, str]]:
    endpoint = os.environ.get("AZURE_OPENAI_ENDPOINT", "").rstrip("/")
    api_key  = os.environ.get("AZURE_OPENAI_API_KEY", "")
    deploy   = os.environ.get("AZURE_OPENAI_DEPLOYMENT_NAME", "")
    version  = os.environ.get("AZURE_OPENAI_API_VERSION", "2025-04-01-preview")

    if not (endpoint and deploy):
        logger.debug("Azure OpenAI not configured — skipping LLM routing")
        return None

    url = f"{endpoint}/openai/deployments/{deploy}/chat/completions?api-version={version}"
    headers: dict[str, str] = {"Content-Type": "application/json"}
    if api_key:
        headers["api-key"] = api_key

    body = {
        "messages": [
            {"role": "system", "content": _SYSTEM},
            {"role": "user",   "content": _USER_TMPL.format(ticket_text=ticket_text)},
        ],
        "max_tokens": 120,
        "temperature": 0,
    }

    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(url, json=body, headers=headers, timeout=15)

        if not resp.is_success:
            logger.warning("LLM router HTTP %s: %s", resp.status_code, resp.text[:200])
            return None

        raw = resp.json()["choices"][0]["message"]["content"].strip()
        lines = raw.split("\n", 1)
        cloud_raw = lines[0].strip().lower()
        reason = lines[1].strip() if len(lines) > 1 else "LLM decision."

        # Extract cloud token from first line (model might add punctuation)
        for c in ("azure", "aws", "snowflake"):
            if c in cloud_raw:
                logger.info("LLM router → %s: %s", c, reason)
                return c, reason

        logger.warning("LLM router returned unrecognised cloud: %r", cloud_raw)
        return None

    except Exception as exc:
        logger.warning("LLM router error: %s", exc)
        return None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def route_ticket(short_description: str, description: str) -> tuple[str, str]:
    """Determine cloud target. Returns (cloud, reasoning).

    cloud     — "azure" | "aws" | "snowflake"
    reasoning — human-readable sentence shown in the demo UI
    """
    ticket_text = f"Short description: {short_description}\n\nDescription:\n{description}"

    result = await _llm_route(ticket_text)
    if result:
        cloud, reason = result
        return cloud, f"AI Router: {reason}"

    cloud, reason = _heuristic_route(ticket_text)
    return cloud, f"Router Agent: {reason}"
