"""
demo_server.py — Lightweight FastAPI server for the provisioning demo.

Starts the orchestrator API with ONLY the demo endpoints wired up.
No cloud credentials or agent-framework required — the simulation
generates realistic step-by-step progression without real LLM/API calls.

Usage:
    pip install fastapi uvicorn httpx
    python -m uvicorn demo_server:app --port 8001 --reload

Then visit http://localhost:3000/snow in the WorkbenchIQ frontend.
"""

import logging

logging.basicConfig(level=logging.INFO, format="%(levelname)s  %(name)s  %(message)s")

# Import the FastAPI app — this wires up all routes including /demo/* and /runs/*
# No WorkflowEngine needed for demo routes (they bypass the engine entirely)
from orchestrator.server import app  # noqa: E402  (after logging setup)

# Re-export app so uvicorn picks it up
__all__ = ["app"]
