"""
models.py — Core data contracts shared across orchestrator and agents.

These are plain dataclasses / enums. No LLM logic here.
The Plan is the contract between Agent 1 (Planner) and the orchestrator DAG executor.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional


# ---------------------------------------------------------------------------
# Workflow state
# ---------------------------------------------------------------------------


class WorkflowStatus(str, Enum):
    WAITING_FOR_APPROVAL = "WAITING_FOR_APPROVAL"
    WAITING_FOR_HUMAN_INPUT = "WAITING_FOR_HUMAN_INPUT"
    WAITING_FOR_COST_APPROVAL = "WAITING_FOR_COST_APPROVAL"
    EXECUTING = "EXECUTING"
    COMPLETE = "COMPLETE"
    FAILED = "FAILED"


class RequestType(str, Enum):
    AZURE_INFRA = "AzureInfra"
    AWS_INFRA = "AWSInfra"
    SNOWFLAKE_INFRA = "SnowflakeInfra"


class UnitStatus(str, Enum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    COMPLETE = "COMPLETE"
    FAILED = "FAILED"
    SKIPPED = "SKIPPED"


# ---------------------------------------------------------------------------
# ServiceNow request (inbound webhook payload)
# ---------------------------------------------------------------------------


@dataclass
class SnowRequest:
    """Parsed payload from a ServiceNow post-approval webhook."""

    ticket_id: str                          # e.g. RITM0001234
    short_description: str
    description: str
    requested_by: str
    approval_state: str                     # must be "approved" to proceed
    request_type: RequestType               # determined by orchestrator router
    application: str = ""                  # app name from catalog item (u_application)
    environment: str = "dev"               # target environment: dev / staging / prod
    github_repo: str = ""                  # terraform repo derived from application
    sys_id: str = ""                       # SNOW internal UUID (current.sys_id from BR payload)
    raw: Dict[str, Any] = field(default_factory=dict)  # original webhook body


# ---------------------------------------------------------------------------
# Plan (output of Planner Agent, input to DAG executor)
# ---------------------------------------------------------------------------


@dataclass
class UnitConstraints:
    """Invariant constraints for a single infra unit.

    The Planner Agent populates these. The TF Generator Agent enforces them.
    The orchestrator never interprets constraint semantics.
    """

    required_rg: Optional[str] = None      # e.g. "rg-postgres-dev"
    forbidden_rg: Optional[str] = None     # e.g. "app_rg"
    location: Optional[str] = None
    extra: Dict[str, Any] = field(default_factory=dict)


@dataclass
class PlanUnit:
    """A single deployable infra unit within a Plan.

    id          — stable identifier referenced in depends_on lists
    type        — resource type string (resource_group | postgres_flex | storage | etc.)
    depends_on  — ids of units that must complete before this one runs
    constraints — invariants the TF Generator must respect
    """

    id: str
    type: str
    depends_on: List[str] = field(default_factory=list)
    constraints: UnitConstraints = field(default_factory=UnitConstraints)
    terraform_output: Optional[str] = None  # populated after TF generation
    resolved_repo: Optional[str] = None     # GitHub repo resolved by GH Search Agent
    status: UnitStatus = UnitStatus.PENDING
    retry_count: int = 0
    error: Optional[str] = None
    wave: int = 0                                       # DAG wave index (for UI grouping)
    eval_scores: Optional[Dict[str, int]] = None        # e.g. {"correctness": 5, "security": 4}
    module_info: Optional[Dict[str, Any]] = None        # {repo, path, sha, readme_chars, readme_url}


@dataclass
class Plan:
    """Output contract from the Planner Agent.

    The orchestrator reads `units` to build the execution DAG.
    If `questions` is non-empty the workflow pauses for human input.
    """

    units: List[PlanUnit]
    questions: List[str] = field(default_factory=list)
    scan_results: Optional[Dict[str, Any]] = None  # populated after env scan
    finalized: bool = False                          # True after questions answered


# ---------------------------------------------------------------------------
# Workflow run (orchestrator state)
# ---------------------------------------------------------------------------


@dataclass
class RunStep:
    """One visible step in the workflow pipeline (for UI polling)."""

    id: str
    label: str
    status: str = "pending"     # pending | running | complete | failed | waiting
    detail: Optional[str] = None
    started_at: Optional[str] = None   # ISO string — plain str for easy JSON serialization
    finished_at: Optional[str] = None


@dataclass
class McpCall:
    """One MCP tool invocation recorded during a workflow run.

    Agents append these at runtime so the UI can trace which external tools
    were called, why, and what they returned.
    """

    id: str = field(default_factory=lambda: str(uuid.uuid4())[:8])
    step_id: str = ""                    # workflow step that triggered this call
    server: str = ""                     # mcp-github | mcp-servicenow | mcp-azure-resource-graph | ...
    tool: str = ""                       # create_branch | get_file_contents | query_resources | ...
    reasoning: str = ""                  # why the agent invoked this tool
    input_summary: str = ""
    output_summary: str = ""
    status: str = "complete"             # running | complete | failed
    timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    duration_ms: int = 0


@dataclass
class WorkflowRun:
    """Full mutable state for one workflow execution.

    Created by the orchestrator when a webhook arrives.
    Persisted to storage (stub) between pause/resume cycles.
    """

    run_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    request: Optional[SnowRequest] = None
    request_type: Optional[RequestType] = None
    status: WorkflowStatus = WorkflowStatus.WAITING_FOR_APPROVAL
    plan: Optional[Plan] = None
    pr_url: Optional[str] = None
    branch_name: Optional[str] = None                   # feature/{ticket_id} — one branch per ticket
    created_at: datetime = field(default_factory=datetime.utcnow)
    updated_at: datetime = field(default_factory=datetime.utcnow)
    error: Optional[str] = None
    pending_questions: List[str] = field(default_factory=list)
    human_answers: Dict[str, str] = field(default_factory=dict)
    steps: List[RunStep] = field(default_factory=list)  # granular step tracking for UI
    cloud: str = ""                                      # azure | aws | snowflake (for UI routing)
    mcp_calls: List[McpCall] = field(default_factory=list)  # ordered log of MCP tool invocations
    hitl_question: str = ""                              # HITL question text (preserved after answer)
    cost_quota_result: Optional[CostQuotaResult] = None  # populated before HITL 2
    cost_approved: Optional[bool] = None                  # None=pending, True=approved, False=rejected

    def transition(self, new_status: WorkflowStatus) -> None:
        """Update status and touch updated_at. Orchestrator calls this — no LLM."""
        self.status = new_status
        self.updated_at = datetime.utcnow()


# ---------------------------------------------------------------------------
# Cost + quota check (output of cost_quota.py — populated before HITL 2)
# ---------------------------------------------------------------------------


@dataclass
class UnitCostEstimate:
    unit_id: str
    unit_type: str
    monthly_usd: float


@dataclass
class CostQuotaResult:
    unit_estimates: List[UnitCostEstimate]
    total_monthly_usd: float
    vcpus_needed: int
    vcpus_available: Optional[int]       # None if quota check unavailable
    vcpus_current_usage: Optional[int]
    quota_ok: bool
    quota_detail: str


# ---------------------------------------------------------------------------
# Evaluator result (output of each evaluator function)
# ---------------------------------------------------------------------------


@dataclass
class EvaluatorResult:
    """Result from one Terraform evaluator.

    score   — 1 (worst) to 5 (best)
    passed  — True if score >= threshold (default 3)
    reason  — feedback injected back into TF Generator if failed
    """

    evaluator: str
    score: int
    passed: bool
    reason: str
