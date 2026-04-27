"""
servicenow.py — ServiceNow MCP client.

Single responsibility: write work notes back to the originating SNOW ticket.

The sys_id is included in the incoming Business Rule webhook payload
(current.sys_id) so no GET is needed. This module only ever POSTs back.

Production MCP equivalent for _patch_work_notes:
    snow__SN-Update-Record(table_name="sc_req_item", sys_id=..., data={work_notes: ...})
"""

from __future__ import annotations

import logging
import os
from typing import List

import httpx

logger = logging.getLogger(__name__)

_SNOW_INSTANCE = os.environ.get("SERVICENOW_INSTANCE_URL", "")
_SNOW_USER     = os.environ.get("SERVICENOW_USERNAME", "")
_SNOW_PASS     = os.environ.get("SERVICENOW_PASSWORD", "")


def _snow_configured() -> bool:
    return bool(_SNOW_INSTANCE and _SNOW_USER and _SNOW_PASS)


async def _patch_work_notes(sys_id: str, work_notes: str) -> None:
    if not _snow_configured():
        logger.info("SNOW not configured — skipping work_notes update (sys_id=%s)", sys_id)
        return
    if not sys_id:
        logger.warning("sys_id missing — cannot update SNOW work notes")
        return

    url = f"{_SNOW_INSTANCE}/api/now/table/sc_req_item/{sys_id}"
    async with httpx.AsyncClient() as client:
        resp = await client.patch(
            url,
            json={"work_notes": work_notes},
            auth=(_SNOW_USER, _SNOW_PASS),
            headers={"Content-Type": "application/json", "Accept": "application/json"},
            timeout=15,
        )
    if not resp.is_success:
        logger.warning("SNOW work_notes update failed (%s): %s", resp.status_code, resp.text[:200])
    else:
        logger.info("Updated SNOW work_notes for sys_id=%s", sys_id)


async def write_questions_to_ticket(
    sys_id: str,
    ticket_id: str,
    run_id: str,
    questions: List[str],
) -> None:
    """Write HITL questions as a work note on the SNOW ticket."""
    formatted = "\n".join(f"{i+1}. {q}" for i, q in enumerate(questions))
    work_notes = (
        f"[Automated provisioning paused — human input required]\n\n"
        f"Please answer the following questions and add a work note with your responses.\n\n"
        f"{formatted}\n\n"
        f"--- do not modify below this line ---\n"
        f"run_id: {run_id}"
    )
    logger.info("Writing %d HITL question(s) to ticket=%s", len(questions), ticket_id)
    await _patch_work_notes(sys_id, work_notes)


async def write_cost_approval_to_ticket(
    sys_id: str,
    ticket_id: str,
    run_id: str,
    total_monthly_usd: float,
    unit_breakdown: list,
    quota_detail: str,
    quota_ok: bool,
) -> None:
    """Write HITL 2 cost + quota summary to the SNOW ticket work note."""
    lines = [
        "[Automated provisioning paused — cost & quota review required]\n",
        "Please review the estimated cost and quota status below, then reply APPROVE or REJECT.\n",
        "─" * 50,
        "\n📦 Resources to be provisioned:\n",
    ]
    for item in unit_breakdown:
        cost_str = f"${item['monthly_usd']:.0f}/mo" if item["monthly_usd"] > 0 else "no charge"
        lines.append(f"  • {item['unit_id']} ({item['unit_type']}) — {cost_str}")

    lines += [
        f"\n💰 Estimated total: ${total_monthly_usd:.0f}/month",
        f"\n⚡ Quota: {quota_detail}",
    ]

    if not quota_ok:
        lines.append("\n⚠️  Insufficient quota — provisioning will fail unless quota is increased.")

    lines += [
        "\n─" * 50,
        "Reply with a work note containing APPROVE to proceed or REJECT to cancel.",
        f"\n--- do not modify below this line ---\nrun_id: {run_id}",
    ]

    work_notes = "\n".join(lines)
    logger.info("Writing cost/quota HITL to ticket=%s total=$%.0f quota_ok=%s",
                ticket_id, total_monthly_usd, quota_ok)
    await _patch_work_notes(sys_id, work_notes)


async def update_ticket_with_pr(
    sys_id: str,
    ticket_id: str,
    pr_url: str,
    summary: str,
) -> None:
    """Update the SNOW ticket with the GitHub PR URL on completion."""
    work_notes = (
        f"[Automated provisioning complete]\n\n"
        f"Terraform PR ready for review: {pr_url}\n\n"
        f"{summary}"
    )
    logger.info("Updating ticket=%s with PR=%s", ticket_id, pr_url)
    await _patch_work_notes(sys_id, work_notes)
