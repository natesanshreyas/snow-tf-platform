"""
environment_scan.py — Agent 2: Environment Scanner (non-agent, deterministic)

NOT an MAF agent. This is a plain async function.

Responsibilities:
- Answer factual questions about the current Azure environment
- Does NOT apply policy or make decisions
- Does NOT generate Terraform
- Results are fed back to the Planner Agent for re-planning

This is intentionally non-agentic because the queries are deterministic:
list resource groups, check if a resource exists, read tags.
No reasoning is needed — just API calls.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, List

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Output contract
# ---------------------------------------------------------------------------


def empty_scan_result() -> Dict[str, Any]:
    """Return a blank scan result structure."""
    return {
        "existing_resources": {},
        "resource_groups": [],
        "subscription_id": "",
    }


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def scan_environment(
    resource_names: List[str],
    subscription_id: str | None = None,
) -> Dict[str, Any]:
    """Check which named resources already exist in the Azure subscription.

    Args:
        resource_names: List of resource names or RG names to check.
                        Extracted from the Plan units by the workflow.
        subscription_id: Azure subscription to scan. Defaults to env var.

    Returns:
        Dict with 'existing_resources' mapping name -> bool (exists or not).

    TODO: Replace stub with real Azure Resource Graph KQL queries.
          See: snow-terraform-agent/src/inventory_scanner.py for reference impl.
    """
    sub = subscription_id or os.environ.get("AZURE_SUBSCRIPTION_ID", "")

    logger.info(
        "Scanning subscription=%s for %d resource names",
        sub or "(not set)", len(resource_names),
    )

    # TODO: Call Azure Resource Graph REST API
    # POST https://management.azure.com/providers/Microsoft.ResourceGraph/resources
    # KQL: Resources | where name in~ ({names}) | project name, type, resourceGroup
    existing: Dict[str, bool] = {}
    for name in resource_names:
        # STUB: assume nothing exists — replace with real ARG query
        existing[name] = False

    # TODO: Also list resource groups for naming convention inference
    resource_groups: List[str] = []
    # STUB: resource_groups = _list_resource_groups(sub)

    result: Dict[str, Any] = {
        "existing_resources": existing,
        "resource_groups": resource_groups,
        "subscription_id": sub,
    }

    logger.info("Scan complete: %d resources checked", len(existing))
    return result


# ---------------------------------------------------------------------------
# Internal helpers (stubs)
# ---------------------------------------------------------------------------


def _extract_resource_names_from_plan_units(units: list) -> List[str]:
    """Pull resource names from plan unit constraints for scanning.

    Extracts required_rg values and unit ids as candidate names to check.
    Called by the workflow before running the scan.
    """
    names = []
    for unit in units:
        if unit.constraints.required_rg:
            names.append(unit.constraints.required_rg)
        # Unit id often maps to resource name — include as candidate
        names.append(unit.id)
    return list(set(names))
