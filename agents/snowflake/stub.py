"""
stub.py — Snowflake infrastructure workflow (stub).

TODO: Implement following the same pattern as agents/azure/workflow.py.
Snowflake units are different (databases, schemas, warehouses, roles)
but the orchestrator contract (Plan, PlanUnit, DAG) is identical.
"""

from __future__ import annotations

import logging

from orchestrator.models import SnowRequest, WorkflowRun, WorkflowStatus
from orchestrator.workflow_engine import store_run

logger = logging.getLogger(__name__)


async def run(request: SnowRequest, run: WorkflowRun) -> None:
    """Snowflake infrastructure workflow — not yet implemented."""
    logger.warning("run=%s Snowflake workflow is not implemented", run.run_id)
    run.error = "Snowflake workflow not implemented"
    run.transition(WorkflowStatus.FAILED)
    store_run(run)
    # TODO: implement following agents/azure/workflow.py pattern
