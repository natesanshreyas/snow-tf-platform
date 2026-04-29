"""
demo_server.py — FastAPI server for the SNOW multi-agent platform.

LLM credentials (one of):
  AZURE_OPENAI_ENDPOINT + AZURE_OPENAI_DEPLOYMENT_NAME   — Azure OpenAI
  OPENAI_API_KEY                                          — OpenAI (gpt-4o)
  MOCK_LLM=true                                           — local dev only, no real LLM

MCP credentials (optional — set DEMO_MODE=true to skip):
  GITHUB_PERSONAL_ACCESS_TOKEN  — real branch/PR creation in terraform-demo-app
  SERVICENOW_INSTANCE_URL etc.  — real SNOW work note updates

Usage (with real LLM):
    export OPENAI_API_KEY=sk-...
    python -m uvicorn demo_server:app --port 8001 --reload

Usage (local dev, no credentials):
    MOCK_LLM=true python -m uvicorn demo_server:app --port 8001 --reload
"""

import logging
import os

# DEMO_MODE stubs out GitHub/ServiceNow MCP calls so real API tokens are not
# required. It does NOT affect the LLM — configure that separately above.
os.environ.setdefault("DEMO_MODE", "true")

logging.basicConfig(level=logging.INFO, format="%(levelname)s  %(name)s  %(message)s")

from orchestrator.server import app  # noqa: E402

__all__ = ["app"]
