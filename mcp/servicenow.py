"""
servicenow.py — ServiceNow MCP stubs.

In production these go through the servicenow-mcp-server MCP stdio server.

TODO: Replace stubs with real MCP client calls.
"""

from __future__ import annotations

import logging
from typing import List

logger = logging.getLogger(__name__)


async def write_questions_to_ticket(
    ticket_id: str,
    run_id: str,
    questions: List[str],
) -> None:
    """Write pending questions as a work note on the ServiceNow ticket.

    Also embeds run_id so the ServiceNow Business Rule can include it in
    the resume webhook payload.

    TODO: Implement using MCP tool:
      snow__SN-Update-Record(table_name="sc_req_item", sys_id=..., data={
          "work_notes": f"Provisioning paused — please answer:\\n{formatted_questions}\\n\\nrun_id: {run_id}"
      })
    """
    formatted = "\n".join(f"- {q}" for q in questions)
    logger.info(
        "STUB: would write to ticket=%s run_id=%s questions:\n%s",
        ticket_id, run_id, formatted,
    )
    # TODO: real MCP call


async def update_ticket_with_pr(ticket_id: str, pr_url: str, summary: str) -> None:
    """Update the ServiceNow ticket with the GitHub PR URL on completion.

    TODO: Implement using MCP tool:
      snow__SN-Update-Record(table_name="sc_req_item", sys_id=..., data={
          "work_notes": f"Terraform PR ready for review: {pr_url}\\n{summary}"
      })
    """
    logger.info("STUB: would update ticket=%s with PR=%s", ticket_id, pr_url)
    # TODO: real MCP call
