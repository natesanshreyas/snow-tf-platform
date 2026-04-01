"""
main.py — Application entry point.

Wires together:
- Domain workflows (azure, aws, snowflake)
- WorkflowEngine
- FastAPI server

Run with: uvicorn main:app --host 0.0.0.0 --port 8030 --reload
"""

from __future__ import annotations

from orchestrator.models import RequestType
from orchestrator.server import app, init_engine
from orchestrator.workflow_engine import WorkflowEngine

import agents.azure.workflow as azure_workflow
import agents.aws.stub as aws_workflow
import agents.snowflake.stub as snowflake_workflow

# Register domain workflows — add new clouds here
engine = WorkflowEngine(
    workflows={
        RequestType.AZURE_INFRA:     azure_workflow.run,
        RequestType.AWS_INFRA:       aws_workflow.run,
        RequestType.SNOWFLAKE_INFRA: snowflake_workflow.run,
    }
)

init_engine(engine)

# `app` is the FastAPI instance from orchestrator/server.py
# uvicorn main:app
