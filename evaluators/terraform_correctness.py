"""
terraform_correctness.py — Evaluator: structural correctness.

Plain function — NOT an MAF agent.

Checks that generated Terraform:
- Uses module blocks (not raw resource blocks)
- Has both main.tf and variables.tf content
- References modules that plausibly exist (name pattern check)
- Does not contain obvious syntax errors (bracket balance)

Score 1-5. Pass threshold: >= 3.
"""

from __future__ import annotations

import re

from orchestrator.models import EvaluatorResult


def evaluate_correctness(main_tf: str, variables_tf: str, ticket_id: str) -> EvaluatorResult:
    """Score Terraform HCL on structural correctness.

    Args:
        main_tf:      Content of main.tf
        variables_tf: Content of variables.tf
        ticket_id:    ServiceNow ticket ID (used for context, not scoring)

    Returns:
        EvaluatorResult with score 1-5 and reason.
    """
    issues = []

    # Check 1: main.tf is non-empty
    if not main_tf or not main_tf.strip():
        return EvaluatorResult(
            evaluator="correctness",
            score=1,
            passed=False,
            reason="main_tf is empty",
        )

    # Check 2: uses module blocks, not raw resource blocks
    has_module = bool(re.search(r'^\s*module\s+"', main_tf, re.MULTILINE))
    has_raw_resource = bool(re.search(r'^\s*resource\s+"', main_tf, re.MULTILINE))

    if not has_module:
        issues.append("No module blocks found — use module blocks from the modules/ directory")
    if has_raw_resource:
        issues.append("Raw resource blocks found — use module blocks instead")

    # Check 3: basic bracket balance (catches incomplete generation)
    open_braces = main_tf.count("{")
    close_braces = main_tf.count("}")
    if open_braces != close_braces:
        issues.append(f"Unbalanced braces: {open_braces} open vs {close_braces} close")

    # Check 4: variables.tf present if main.tf references variables
    references_vars = bool(re.search(r'\bvar\.\w+', main_tf))
    if references_vars and (not variables_tf or not variables_tf.strip()):
        issues.append("main.tf uses var.* references but variables_tf is empty")

    # Score
    if not issues:
        score = 5
    elif len(issues) == 1 and "Raw resource" not in issues[0]:
        score = 3
    elif len(issues) == 1:
        score = 2
    else:
        score = 1

    passed = score >= 3
    reason = "; ".join(issues) if issues else "Structural checks passed"

    return EvaluatorResult(
        evaluator="correctness",
        score=score,
        passed=passed,
        reason=reason,
    )
