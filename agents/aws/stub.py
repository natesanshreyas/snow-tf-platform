"""
stub.py — AWS infrastructure workflow (stub).

TODO: Implement following the same pattern as agents/azure/workflow.py:
  1. AWSPlannerAgent   — interprets request, outputs Plan with AWS unit types
  2. AWSEnvironmentScan — queries AWS resource inventory (boto3 / ARG equivalent)
  3. AWSTerraformAgent  — generates AWS provider Terraform

The orchestrator boundary is identical — only the agents differ.
"""

from __future__ import annotations

import logging

from orchestrator.models import SnowRequest, WorkflowRun, WorkflowStatus
from orchestrator.workflow_engine import store_run

logger = logging.getLogger(__name__)


async def run(request: SnowRequest, run: WorkflowRun) -> None:
    """AWS infrastructure workflow — not yet implemented."""
    logger.warning("run=%s AWS workflow is not implemented", run.run_id)
    run.error = "AWS workflow not implemented"
    run.transition(WorkflowStatus.FAILED)
    store_run(run)
    # TODO: implement following agents/azure/workflow.py pattern
