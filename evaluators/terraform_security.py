"""
terraform_security.py — Evaluator: security checks.

Plain function — NOT an MAF agent.

Checks for hardcoded secrets, insecure patterns, and exposed credentials.
Uses pattern matching — intentionally deterministic, no LLM.

TODO: For richer detection (context-aware secret identification),
      replace with an LLM-based judge following the pattern in
      snow-terraform-agent/src/terraform_evaluator.py:SecurityEvaluator.

Score 1-5. Pass threshold: >= 3.
"""

from __future__ import annotations

import re
from typing import List

from orchestrator.models import EvaluatorResult

# Patterns that strongly suggest hardcoded secrets
_SECRET_PATTERNS = [
    (r'(?i)(password|passwd|pwd)\s*=\s*"[^"${}][^"]{3,}"', "hardcoded password"),
    (r'(?i)(secret|api_key|access_key|client_secret)\s*=\s*"[^"${}][^"]{8,}"', "hardcoded secret/key"),
    (r'(?i)connection_string\s*=\s*"[^"${}]{20,}"', "hardcoded connection string"),
    (r'(?i)sas_token\s*=\s*"[^"${}]{20,}"', "hardcoded SAS token"),
]

# Patterns that are acceptable (variable references, empty strings, placeholders)
_SAFE_PATTERNS = [
    r'\$\{',          # Terraform interpolation
    r'var\.',         # Variable reference
    r'""',            # Empty string
    r'"<.*>"',        # Placeholder
]


def evaluate_security(main_tf: str, variables_tf: str, ticket_id: str) -> EvaluatorResult:
    """Score Terraform HCL on security (no hardcoded secrets).

    Args:
        main_tf:      Content of main.tf
        variables_tf: Content of variables.tf
        ticket_id:    ServiceNow ticket ID

    Returns:
        EvaluatorResult with score 1-5 and reason.
    """
    combined = main_tf + "\n" + variables_tf
    findings: List[str] = []

    for pattern, label in _SECRET_PATTERNS:
        matches = re.findall(pattern, combined)
        if matches:
            # Exclude matches that are actually safe variable references
            real_matches = [
                m for m in (re.findall(pattern + r'[^\n]*', combined) or [])
                if not any(re.search(safe, m) for safe in _SAFE_PATTERNS)
            ]
            if real_matches:
                findings.append(label)

    if not findings:
        score = 5
        reason = "No hardcoded secrets detected"
    elif len(findings) == 1:
        score = 2
        reason = f"Potential hardcoded secret: {findings[0]} — use variable or Key Vault reference"
    else:
        score = 1
        reason = f"Multiple hardcoded secrets: {', '.join(findings)} — use variables or Key Vault references"

    return EvaluatorResult(
        evaluator="security",
        score=score,
        passed=score >= 3,
        reason=reason,
    )
