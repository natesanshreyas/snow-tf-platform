"""
cost_quota.py — deterministic cost estimation and quota check (no LLM).

Runs after plan finalization, before HITL 2, so the human sees:
  - Estimated monthly cost per resource unit
  - vCPU quota status (needed vs available)

Non-agentic by design: pricing lookup and quota checks are deterministic facts.
"""

from __future__ import annotations

import logging
import os
from typing import List, Optional

import httpx

from orchestrator.models import CostQuotaResult, PlanUnit, UnitCostEstimate

logger = logging.getLogger(__name__)

# Approximate monthly USD — East US 2. Swap for live Azure Retail Prices API in prod.
_PRICE_TABLE = {
    "resource_group":  0.0,
    "postgres_flex":   185.0,   # GP_Standard_D2s_v3, 4 vCores
    "storage":          20.0,   # Standard LRS, 500 GB estimate
    "app_service":      55.0,   # Standard S1
    "key_vault":         5.0,
    "virtual_network":   0.0,
    "subnet":            0.0,
    "openai":          100.0,   # S0 tier estimate
    "container_app":    30.0,
    "service_bus":      10.0,
    "cosmos_db":        25.0,
    "vpc":               0.0,
    "rds_postgres":    180.0,   # db.t3.medium Multi-AZ
    "s3":                5.0,
    "database":          0.0,
    "schema":            0.0,
    "warehouse":        35.0,   # XSMALL, 300s auto-suspend
}

_VCPU_TABLE = {
    "postgres_flex":  4,
    "rds_postgres":   2,
    "container_app":  2,
    "app_service":    2,
}


async def _check_vcpu_quota(
    subscription_id: str,
    location: str,
    vcpus_needed: int,
) -> tuple[Optional[int], Optional[int], bool, str]:
    """Query ARM Compute usage API. Returns (current, limit, ok, detail)."""
    if not subscription_id:
        return None, None, True, "quota check skipped (no subscription configured)"

    try:
        from azure.identity import DefaultAzureCredential
        token = DefaultAzureCredential().get_token("https://management.azure.com/.default")
        url = (
            f"https://management.azure.com/subscriptions/{subscription_id}"
            f"/providers/Microsoft.Compute/locations/{location}"
            f"/usages?api-version=2024-03-01"
        )
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(
                url,
                headers={"Authorization": f"Bearer {token.token}"},
            )

        if not resp.is_success:
            logger.warning("Quota API %s: %s", resp.status_code, resp.text[:200])
            return None, None, True, "quota check unavailable"

        for item in resp.json().get("value", []):
            if item.get("name", {}).get("value") == "cores":
                current = item["currentValue"]
                limit = item["limit"]
                remaining = limit - current
                ok = remaining >= vcpus_needed
                detail = (
                    f"{vcpus_needed} vCPUs needed · "
                    f"{current}/{limit} used · "
                    f"{remaining} remaining · "
                    f"{'✅ OK' if ok else '❌ INSUFFICIENT'}"
                )
                return current, limit, ok, detail

        return None, None, True, "cores quota entry not found"

    except Exception as exc:
        logger.warning("Quota check failed (non-fatal): %s", exc)
        return None, None, True, f"quota check skipped ({exc})"


async def run_cost_quota_check(
    units: List[PlanUnit],
    subscription_id: Optional[str] = None,
    location: str = "eastus2",
) -> CostQuotaResult:
    """Estimate monthly cost and check vCPU quota for a finalized plan."""
    sub_id = subscription_id or os.environ.get("AZURE_SUBSCRIPTION_ID", "")

    estimates: list[UnitCostEstimate] = []
    total = 0.0
    vcpus_needed = 0

    for unit in units:
        monthly = _PRICE_TABLE.get(unit.type, 0.0)
        estimates.append(UnitCostEstimate(
            unit_id=unit.id,
            unit_type=unit.type,
            monthly_usd=monthly,
        ))
        total += monthly
        vcpus_needed += _VCPU_TABLE.get(unit.type, 0)

    current, limit, quota_ok, quota_detail = await _check_vcpu_quota(
        sub_id, location, vcpus_needed,
    )

    return CostQuotaResult(
        unit_estimates=estimates,
        total_monthly_usd=round(total, 2),
        vcpus_needed=vcpus_needed,
        vcpus_available=(limit - current) if (limit is not None and current is not None) else None,
        vcpus_current_usage=current,
        quota_ok=quota_ok,
        quota_detail=quota_detail,
    )
