"""
terraform_compliance.py — Evaluator: tag compliance.

Plain function — NOT an MAF agent.

Checks that generated Terraform includes required org tags:
- cost_center
- ticket_id
- environment

These are deterministic string checks — no LLM needed.
If you need semantic tag validation (e.g. cost_center format), use an LLM judge.

Score 1-5. Pass threshold: >= 3.
"""

from __future__ import annotations

import re
from typing import List

from orchestrator.models import EvaluatorResult

REQUIRED_TAGS = ["cost_center", "ticket_id", "environment"]


def evaluate_compliance(main_tf: str, variables_tf: str, ticket_id: str) -> EvaluatorResult:
    """Score Terraform HCL on tag compliance.

    Args:
        main_tf:      Content of main.tf
        variables_tf: Content of variables.tf
        ticket_id:    ServiceNow ticket ID (expected in ticket_id tag)

    Returns:
        EvaluatorResult with score 1-5 and reason.
    """
    missing: List[str] = []

    for tag in REQUIRED_TAGS:
        # Accept either: tag = "value" or tag = var.something
        pattern = rf'(?i){re.escape(tag)}\s*='
        if not re.search(pattern, main_tf):
            missing.append(tag)

    # Bonus check: ticket_id tag should reference the actual ticket
    if "ticket_id" not in missing:
        if ticket_id and ticket_id not in main_tf:
            missing.append(f"ticket_id value (expected '{ticket_id}' or var reference)")

    if not missing:
        score = 5
        reason = "All required tags present"
    elif len(missing) == 1:
        score = 3
        reason = f"Missing tag: {missing[0]}"
    elif len(missing) == 2:
        score = 2
        reason = f"Missing tags: {', '.join(missing)}"
    else:
        score = 1
        reason = f"Missing all required tags: {', '.join(missing)}"

    return EvaluatorResult(
        evaluator="compliance",
        score=score,
        passed=score >= 3,
        reason=reason,
    )
