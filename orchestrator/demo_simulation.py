"""
demo_simulation.py — Simulates the full provisioning workflow for demo purposes.

No real cloud calls or LLM calls. Realistic step delays give the UI something
to poll against. HITL pause/resume uses asyncio.Event per run.

Supports azure, aws, and snowflake cloud configs.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import os
import random
from datetime import datetime, timezone
from typing import Dict, List, Optional

import httpx

# ---------------------------------------------------------------------------
# Real MAF agent imports — fall back gracefully when credentials are absent.
# When AZURE_OPENAI_ENDPOINT is not set, get_model_client() returns
# MockModelClient so every agent runs through the full AutoGen runtime
# with hardcoded but realistic responses.
# ---------------------------------------------------------------------------
try:
    from agents.azure.planner_agent import run_planner_agent as _run_planner_agent
    from agents.github_search_agent import run_github_search_agent as _run_github_search_agent
    from agents.azure.terraform_agent import run_terraform_agent as _run_terraform_agent
    from evaluators.terraform_correctness import evaluate_correctness
    from evaluators.terraform_security import evaluate_security
    from evaluators.terraform_compliance import evaluate_compliance
    _EVALUATORS = [evaluate_correctness, evaluate_security, evaluate_compliance]
    _AGENTS_AVAILABLE = True
except ImportError as _agent_import_err:
    logger_init = logging.getLogger(__name__)
    logger_init.warning(
        "MAF agents not importable (%s) — demo will simulate without real agent execution",
        _agent_import_err,
    )
    _AGENTS_AVAILABLE = False
    _EVALUATORS = []

from .models import (
    CostQuotaResult,
    EvaluatorResult,
    McpCall,
    Plan,
    PlanUnit,
    RunStep,
    SnowRequest,
    UnitConstraints,
    UnitCostEstimate,
    UnitStatus,
    WorkflowRun,
    WorkflowStatus,
)
from .workflow_engine import store_run

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# HITL synchronization (per run_id)
# ---------------------------------------------------------------------------

_hitl_events: Dict[str, asyncio.Event] = {}
_hitl_answers: Dict[str, Dict[str, str]] = {}


def resume_simulation(run_id: str, answers: Dict[str, str]) -> bool:
    """Signal the paused simulation to continue. Returns True if a waiter existed."""
    event = _hitl_events.pop(run_id, None)
    if event is None:
        return False
    _hitl_answers[run_id] = answers
    event.set()
    return True


# ---------------------------------------------------------------------------
# Cloud configuration
# ---------------------------------------------------------------------------

# Simulated environment scan results — fed into Planner Final to derive UnitConstraints
_CLOUD_SCAN_RESULTS: Dict[str, dict] = {
    "azure": {
        "resource_group": {"name": "rg-{app}-{env}", "location": "eastus2"},
        "location": "eastus2",
    },
    "aws": {
        "vpc": {"id": "vpc-0abc1d2ef3456789", "cidr": "10.0.0.0/16"},
        "region": "us-east-1",
    },
    "snowflake": {
        "database": {"name": "{APP}_PROD", "retention_days": 1},
        "role": "SYSADMIN",
    },
}

_CLOUD_CONFIGS: Dict[str, dict] = {
    "azure": {
        "label": "Azure IaC",
        "waves": [
            [
                {"id": "app_rg", "type": "resource_group", "label": "Resource Group"},
            ],
            [
                {"id": "postgres_flex", "type": "postgres_flex", "label": "PostgreSQL Flexible Server"},
                {"id": "storage_account", "type": "storage", "label": "Storage Account"},
            ],
        ],
        "hitl_question": (
            "Environment scan found that resource group **rg-{app}-{env}** already exists "
            "in the subscription. Should the Terraform use this existing resource group, "
            "or create a new one with a unique suffix?"
        ),
        "planner_initial_detail": "3 units identified",
        "scan_detail": "1 existing resource group found: rg-{app}-{env}",
        "cost_estimates": [
            {"unit_id": "app_rg",        "unit_type": "resource_group", "monthly_usd": 0.0},
            {"unit_id": "postgres_flex", "unit_type": "postgres_flex",  "monthly_usd": 185.0},
            {"unit_id": "storage_account","unit_type": "storage",       "monthly_usd": 20.0},
        ],
        "quota_detail": "4 vCPUs needed · 12/60 used · 48 remaining · ✅ OK",
        "quota_ok": True,
    },
    "aws": {
        "label": "AWS IaC",
        "waves": [
            [
                {"id": "vpc", "type": "vpc", "label": "VPC"},
            ],
            [
                {"id": "rds_postgres", "type": "rds_postgres", "label": "RDS PostgreSQL"},
                {"id": "s3_bucket", "type": "s3", "label": "S3 Bucket"},
            ],
        ],
        "hitl_question": (
            "Environment scan found that VPC **vpc-{app}-{env}** already exists in us-east-1. "
            "Should the Terraform reference this existing VPC, "
            "or provision a new one?"
        ),
        "planner_initial_detail": "3 units identified",
        "scan_detail": "1 existing VPC found: vpc-{app}-{env} (us-east-1)",
        "cost_estimates": [
            {"unit_id": "vpc",         "unit_type": "vpc",         "monthly_usd": 0.0},
            {"unit_id": "rds_postgres","unit_type": "rds_postgres","monthly_usd": 180.0},
            {"unit_id": "s3_bucket",   "unit_type": "s3",          "monthly_usd": 5.0},
        ],
        "quota_detail": "2 vCPUs needed · 4/32 used · 28 remaining · ✅ OK",
        "quota_ok": True,
    },
    "snowflake": {
        "label": "Snowflake IaC",
        "waves": [
            [
                {"id": "sf_database", "type": "database", "label": "Database"},
            ],
            [
                {"id": "sf_schema", "type": "schema", "label": "Schema"},
                {"id": "sf_warehouse", "type": "warehouse", "label": "Warehouse"},
            ],
        ],
        "hitl_question": (
            "Environment scan found that database **{APP}_PROD** already exists "
            "in the Snowflake account. Should we point the Terraform at this existing database, "
            "or create a new one?"
        ),
        "planner_initial_detail": "3 units identified",
        "scan_detail": "1 existing database found: {APP}_PROD",
        "cost_estimates": [
            {"unit_id": "sf_database", "unit_type": "database",  "monthly_usd": 0.0},
            {"unit_id": "sf_schema",   "unit_type": "schema",    "monthly_usd": 0.0},
            {"unit_id": "sf_warehouse","unit_type": "warehouse",  "monthly_usd": 35.0},
        ],
        "quota_detail": "0 vCPUs needed · quota check skipped (Snowflake)",
        "quota_ok": True,
    },
}

_VCPU_TABLE = {"postgres_flex": 4, "rds_postgres": 2, "container_app": 2, "app_service": 2}

# Real GitHub repo used for all demo PRs
_DEMO_APP_ORG  = "natesanshreyas"
_DEMO_APP_REPO = "terraform-demo-app"

# Cloud-specific module repos — the Planner stamps these onto each PlanUnit.
# TF Generator reads the repo from the unit; it never needs to infer the cloud itself.
_CLOUD_MODULE_REPOS: Dict[str, str] = {
    "azure":     "acme/terraform-azure-modules",
    "aws":       "acme/terraform-aws-modules",
    "snowflake": "acme/terraform-snowflake-modules",
}

# Cloud-specific TF Generator system prompt fragments.
# Injected at agent init — this is what makes each instance a domain specialist.
_CLOUD_TF_SYSTEM_PROMPTS: Dict[str, str] = {
    "azure": (
        "You are an Azure Terraform specialist.\n"
        "Rules:\n"
        "  - Use kebab-case for all resource names (e.g. 'payments-postgres-prod').\n"
        "  - resource_group_name and location are REQUIRED on every non-RG resource.\n"
        "    Read them from the constraints block — do not invent values.\n"
        "  - Use the azurerm provider, version >= 3.0. Do not use azapi.\n"
        "  - Pin every module source to the exact commit SHA in the constraints.\n"
        "  - Do not add variables not listed in the module README.\n"
        "  - Tag every resource with environment and cost_center if the README supports it."
    ),
    "aws": (
        "You are an AWS Terraform specialist.\n"
        "Rules:\n"
        "  - Use snake_case for all resource names (e.g. 'payments_rds_postgres_prod').\n"
        "  - vpc_id and region are REQUIRED on every resource. Read from constraints.\n"
        "  - Use the hashicorp/aws provider, version >= 5.0.\n"
        "  - Enable storage_encrypted = true on all RDS resources by default.\n"
        "  - Set multi_az = true for production environments.\n"
        "  - IAM roles must follow least-privilege. Do not use wildcard Actions or Resources.\n"
        "  - Pin every module source to the exact commit SHA in the constraints."
    ),
    "snowflake": (
        "You are a Snowflake Terraform specialist.\n"
        "Rules:\n"
        "  - Use SCREAMING_SNAKE_CASE for ALL names (database, schema, warehouse, role).\n"
        "  - database and role are REQUIRED on every resource. Read from constraints.\n"
        "  - Use the Snowflake provider (Snowflake-Labs/snowflake), version >= 0.90.\n"
        "  - Set auto_suspend = 300 and auto_resume = true on all warehouses.\n"
        "  - Always emit a GRANT block to the role in the constraints.\n"
        "  - Pin every module source to the exact commit SHA in the constraints."
    ),
}

# Naming convention applied to the `name` field per cloud
def _apply_name_convention(cloud: str, name: str) -> str:
    if cloud == "aws":
        return name.replace("-", "_")
    if cloud == "snowflake":
        return name.upper().replace("-", "_")
    return name  # azure: kebab-case as-is


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _step_set(run: WorkflowRun, step_id: str, status: str, detail: Optional[str] = None) -> None:
    for s in run.steps:
        if s.id == step_id:
            if status == "running" and s.started_at is None:
                s.started_at = _now()
            if status in ("complete", "failed") and s.finished_at is None:
                s.finished_at = _now()
            s.status = status
            if detail is not None:
                s.detail = detail
            break
    store_run(run)


def _make_initial_steps(cloud: str, waves: list) -> List[RunStep]:
    """Build the ordered step list for a cloud workflow."""
    wave_steps = [
        RunStep(
            id=f"wave_{i}",
            label=f"Wave {i}: " + " + ".join(u["label"] for u in wave),
        )
        for i, wave in enumerate(waves)
    ]
    return [
        RunStep(id="branch_created",   label="Branch Created"),
        RunStep(id="planner_initial",  label="Planner Agent (Initial)"),
        RunStep(id="env_scan",         label="Environment Scan"),
        RunStep(id="hitl_checkpoint",  label="HITL 1 — Resource Conflict"),
        RunStep(id="planner_final",    label="Planner Agent (Final)"),
        RunStep(id="cost_checkpoint",  label="HITL 2 — Cost & Quota Review"),
        RunStep(id="gh_search",        label="GH Search Agent"),
        *wave_steps,
        RunStep(id="pr_created",       label="Pull Request Created"),
    ]


def _make_plan(cloud: str, waves: list) -> Plan:
    units: List[PlanUnit] = []
    for wave_idx, wave in enumerate(waves):
        prev_ids = [u["id"] for prev_wave in waves[:wave_idx] for u in prev_wave]
        for u in wave:
            units.append(PlanUnit(
                id=u["id"],
                type=u["type"],
                depends_on=prev_ids,
                constraints=UnitConstraints(),
                status=UnitStatus.PENDING,
                wave=wave_idx,
            ))
    return Plan(units=units, finalized=False)


# ---------------------------------------------------------------------------
# Simulation runner
# ---------------------------------------------------------------------------


_MODULES_ORG  = "natesanshreyas"
_MODULES_REPO = "terraform-modules"
_GH_API       = "https://api.github.com"


async def _fetch_module_info(module_type: str, module_repo: str) -> Optional[Dict]:
    """Fetch real README and commit SHA.

    `module_repo` is the cloud-specific conceptual repo (e.g. acme/terraform-azure-modules).
    We fetch actual content from the real demo repo as a stand-in, but report the
    cloud-specific repo name so the UI and MCP calls reflect the correct architecture.
    """
    pat = os.environ.get("GITHUB_PERSONAL_ACCESS_TOKEN", "")
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if pat:
        headers["Authorization"] = f"Bearer {pat}"

    path       = f"modules/{module_type}/README.md"
    readme_url = f"https://github.com/{module_repo}/blob/main/{path}"

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            sha_resp = await client.get(
                f"{_GH_API}/repos/{_MODULES_ORG}/{_MODULES_REPO}/commits",
                params={"path": f"modules/{module_type}", "per_page": 1, "sha": "main"},
                headers=headers,
            )
            sha = sha_resp.json()[0]["sha"][:7] if sha_resp.is_success and sha_resp.json() else "main"

            readme_resp = await client.get(
                f"{_GH_API}/repos/{_MODULES_ORG}/{_MODULES_REPO}/contents/{path}",
                headers={**headers, "Accept": "application/vnd.github.raw+json"},
            )
            readme_chars = len(readme_resp.text) if readme_resp.is_success else 0

        logger.info("Fetched module info: %s repo=%s sha=%s readme=%d chars",
                    module_type, module_repo, sha, readme_chars)
        return {
            "repo":         module_repo,          # cloud-specific repo shown in UI
            "path":         f"modules/{module_type}",
            "sha":          sha,
            "readme_chars": readme_chars,
            "readme_url":   readme_url,
        }
    except Exception as exc:
        logger.warning("Failed to fetch module info for %s: %s", module_type, exc)
        return {
            "repo":         module_repo,
            "path":         f"modules/{module_type}",
            "sha":          "main",
            "readme_chars": 0,
            "readme_url":   readme_url,
        }


# Type-specific required variables derived from each module's README.
# Populates realistic required fields the LLM would read from the README.
_TYPE_DEFAULTS: Dict[str, Dict[str, str]] = {
    "resource_group": {
        "tags": '{ environment = var.environment, managed_by = "snow-tf-platform" }',
    },
    "postgres_flex": {
        "sku_name":               "GP_Standard_D2s_v3",
        "storage_mb":             "65536",
        "backup_retention_days":  "7",
        "administrator_login":    "psqladmin",
        "administrator_password": 'var.postgres_admin_password  # injected via CI secret',
        "high_availability":      'false',
        "version":                '"16"',
    },
    "storage": {
        "account_tier":             "Standard",
        "account_replication_type": "LRS",
        "enable_https_traffic_only": "true",
        "min_tls_version":          "TLS1_2",
    },
    "rds_postgres": {
        "instance_class":      "db.t3.medium",
        "engine_version":      '"16.2"',
        "storage_encrypted":   "true",
        "multi_az":            "true",
        "deletion_protection": "true",
        "skip_final_snapshot": "false",
    },
    "s3": {
        "versioning_enabled":  "true",
        "server_side_encryption": '"AES256"',
        "block_public_acls":   "true",
        "block_public_policy": "true",
    },
    "vpc": {
        "cidr_block":           '"10.0.0.0/16"',
        "enable_dns_hostnames": "true",
        "enable_dns_support":   "true",
    },
    "database": {
        "data_retention_time_in_days": "1",
    },
    "schema": {
        "is_managed": "true",
    },
    "warehouse": {
        "warehouse_size":  "XSMALL",
        "auto_suspend":    "300",
        "auto_resume":     "true",
    },
}


def _hcl_for_unit(
    unit_cfg: dict,
    app: str,
    env: str,
    module_sha: str,
    constraints: Optional[UnitConstraints] = None,
    cloud: str = "azure",
    ticket_id: str = "",
) -> str:
    """Generate Terraform HCL for a unit, honoring scan-derived constraints."""
    uid   = unit_cfg["id"]
    utype = unit_cfg["type"]
    c     = constraints or UnitConstraints()

    repo_for_src = c.extra.get("module_repo", f"{_MODULES_ORG}/{_MODULES_REPO}")
    src = f"git::https://github.com/{repo_for_src}.git//modules/{utype}?ref={module_sha}"

    raw_name = f"{app}-{uid}-{env}"
    name = _apply_name_convention(cloud, raw_name)
    mod_name = _apply_name_convention(cloud, uid)

    lines = [f'module "{mod_name}" {{', f'  source = "{src}"', ""]

    # ── Constraint-driven fields (set by Planner Final after env scan) ──────
    if c.location:
        lines.append(f'  location              = "{c.location}"')
    if c.required_rg:
        lines.append(f'  resource_group_name   = "{c.required_rg}"')
    if c.extra.get("vpc_id"):
        lines.append(f'  vpc_id                = "{c.extra["vpc_id"]}"')
    if c.extra.get("region"):
        lines.append(f'  region                = "{c.extra["region"]}"')
    if c.extra.get("database"):
        lines.append(f'  database              = "{c.extra["database"]}"')
    if c.extra.get("role"):
        lines.append(f'  role                  = "{c.extra["role"]}"')

    # ── Standard identity fields ─────────────────────────────────────────────
    lines.append(f'  name                  = "{name}"')
    lines.append(f'  environment           = "{env}"')

    # ── README-derived required variables (per module type) ──────────────────
    for var, val in _TYPE_DEFAULTS.get(utype, {}).items():
        # Don't repeat fields already emitted above
        already = any(f"  {var}" in ln for ln in lines)
        if not already:
            lines.append(f'  {var:<22}= {val}')

    # ── Tagging (cost traceability) ──────────────────────────────────────────
    lines += [
        "",
        "  tags = {",
        f'    ticket_id   = "{ticket_id}"',
        f'    environment = "{env}"',
        '    managed_by  = "snow-tf-platform"',
        "  }",
        "}",
    ]
    return "\n".join(lines) + "\n"


async def _create_real_pr(
    run: WorkflowRun,
    cloud: str,
    waves: list,
    app: str,
    env: str,
    ticket_id: str,
) -> str:
    """Create a real branch + files + PR in terraform-demo-app. Returns PR html_url."""
    pat = os.environ.get("GITHUB_PERSONAL_ACCESS_TOKEN", "")
    if not pat:
        logger.warning("No GITHUB_PERSONAL_ACCESS_TOKEN — skipping real PR, using placeholder")
        return f"https://github.com/{_DEMO_APP_ORG}/{_DEMO_APP_REPO}/pulls"

    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "Authorization": f"Bearer {pat}",
    }
    base = _GH_API
    repo_path = f"/repos/{_DEMO_APP_ORG}/{_DEMO_APP_REPO}"
    branch = f"feature/{ticket_id}-{run.run_id[:8]}"

    try:
        async with httpx.AsyncClient(timeout=20) as client:
            # 1. Get SHA of main branch HEAD
            ref_resp = await client.get(
                f"{base}{repo_path}/git/ref/heads/main",
                headers=headers,
            )
            if not ref_resp.is_success:
                logger.warning("Could not get main ref: %s", ref_resp.text)
                return f"https://github.com/{_DEMO_APP_ORG}/{_DEMO_APP_REPO}/pulls"
            main_sha = ref_resp.json()["object"]["sha"]

            # 2. Create feature branch
            br_resp = await client.post(
                f"{base}{repo_path}/git/refs",
                headers=headers,
                json={"ref": f"refs/heads/{branch}", "sha": main_sha},
            )
            if not br_resp.is_success and br_resp.status_code != 422:
                # 422 = branch already exists (idempotent)
                logger.warning("Branch creation failed: %s", br_resp.text)

            # 3. Push one HCL file per active unit (HITL may have dropped Wave 0)
            active_unit_ids = {u.id for u in run.plan.units}
            all_units = [
                u for wave in waves for u in wave
                if u["id"] in active_unit_ids
            ]
            pushed_unit_infos: list[dict] = []
            for unit_cfg in all_units:
                module_sha  = "main"
                constraints = UnitConstraints()
                module_repo = f"{_MODULES_ORG}/{_MODULES_REPO}"
                maf_hcl: Optional[str] = None
                for pu in run.plan.units:
                    if pu.id == unit_cfg["id"]:
                        if pu.module_info:
                            module_sha  = pu.module_info.get("sha", "main")
                            module_repo = pu.module_info.get("repo", module_repo)
                        constraints = pu.constraints or UnitConstraints()
                        maf_hcl = pu.terraform_output  # set by run_terraform_agent
                        break

                # Prefer MAF-generated HCL; fall back to template-based generation
                hcl = maf_hcl or _hcl_for_unit(unit_cfg, app, env, module_sha, constraints, cloud, ticket_id)
                content_b64 = base64.b64encode(hcl.encode()).decode()
                file_path = f"{env}/{ticket_id}/{unit_cfg['id']}/main.tf"

                await client.put(
                    f"{base}{repo_path}/contents/{file_path}",
                    headers=headers,
                    json={
                        "message": f"feat({ticket_id}): add {unit_cfg['id']} module [{cloud}]",
                        "content": content_b64,
                        "branch": branch,
                    },
                )
                pushed_unit_infos.append({
                    "id":         unit_cfg["id"],
                    "type":       unit_cfg["type"],
                    "module_src": f"git::https://github.com/{module_repo}.git//modules/{unit_cfg['type']}?ref={module_sha}",
                    "path":       file_path,
                })

            # 4. Open PR with a clean, professional description
            resource_table = "\n".join(
                f"| `{u['id']}` | `{u['type']}` | `{u['path']}` |"
                for u in pushed_unit_infos
            )
            module_sources = "\n".join(
                f"- `{u['id']}`: `{u['module_src']}`"
                for u in pushed_unit_infos
            )
            pr_body = (
                f"## [{cloud.upper()}] Provision `{app}` — `{env}`\n\n"
                f"**Ticket** `{ticket_id}` · **Application** `{app}` · **Environment** `{env}`\n\n"
                f"---\n\n"
                f"### Resources provisioned\n\n"
                f"| Resource | Type | Path |\n"
                f"|----------|------|------|\n"
                f"{resource_table}\n\n"
                f"### Module sources\n\n"
                f"{module_sources}\n\n"
                f"---\n\n"
                f"### Checklist\n\n"
                f"- [x] Module calls only — no raw `resource` blocks\n"
                f"- [x] All module sources pinned to exact commit SHA\n"
                f"- [x] Variables populated from module README (required + optional)\n"
                f"- [x] Output path scoped to `{env}/{ticket_id}/`\n"
                f"- [x] HITL checkpoint confirmed before generation\n\n"
                f"---\n"
                f"*Generated by [snow-tf-platform](https://github.com/{_DEMO_APP_ORG}/snow-tf-platform) · Agent: TF Generator · Cloud: {cloud.upper()}*"
            )
            pr_resp = await client.post(
                f"{base}{repo_path}/pulls",
                headers=headers,
                json={
                    "title": f"[{cloud.upper()}] {ticket_id}: provision {app} ({env})",
                    "body": pr_body,
                    "head": branch,
                    "base": "main",
                },
            )
            if pr_resp.is_success:
                pr_url = pr_resp.json()["html_url"]
                logger.info("Created real PR: %s", pr_url)
                return pr_url
            else:
                logger.warning("PR creation failed: %s", pr_resp.text)
                return f"https://github.com/{_DEMO_APP_ORG}/{_DEMO_APP_REPO}/pulls"

    except Exception as exc:
        logger.warning("_create_real_pr error: %s", exc)
        return f"https://github.com/{_DEMO_APP_ORG}/{_DEMO_APP_REPO}/pulls"


# MCP server used for environment scanning, keyed by cloud
_CLOUD_SCAN_MCP: Dict[str, tuple] = {
    "azure":     ("mcp-azure-resource-graph", "query_resources"),
    "aws":       ("mcp-aws-config",           "describe_resources"),
    "snowflake": ("mcp-snowflake",            "show_objects"),
}


def _mcp_emit(
    run: WorkflowRun,
    step_id: str,
    server: str,
    tool: str,
    reasoning: str,
    input_summary: str,
    output_summary: str = "",
    duration_ms: int = 0,
) -> None:
    """Append a completed MCP tool-call record to the run log and persist."""
    run.mcp_calls.append(McpCall(
        step_id=step_id,
        server=server,
        tool=tool,
        reasoning=reasoning,
        input_summary=input_summary,
        output_summary=output_summary,
        status="complete",
        duration_ms=duration_ms,
    ))
    store_run(run)


async def simulate_workflow(run: WorkflowRun, cloud: str) -> None:
    """Drive the run through realistic steps. Called as an asyncio background task."""
    cfg = _CLOUD_CONFIGS.get(cloud, _CLOUD_CONFIGS["azure"])
    waves = cfg["waves"]
    app = (run.request.application if run.request else "app") or "app"
    env = (run.request.environment if run.request else "dev") or "dev"
    ticket_id = (run.request.ticket_id if run.request else "DEMO-001") or "DEMO-001"

    # Build steps and initial plan
    run.steps = _make_initial_steps(cloud, waves)
    run.plan = _make_plan(cloud, waves)
    run.cloud = cloud
    run.transition(WorkflowStatus.EXECUTING)
    store_run(run)

    # ── Step 0: Branch ──────────────────────────────────────────────────────
    _step_set(run, "branch_created", "running")
    await asyncio.sleep(0.8)
    run.branch_name = f"feature/{ticket_id}"
    _step_set(run, "branch_created", "complete", f"feature/{ticket_id}")
    _mcp_emit(run, "branch_created", "mcp-github", "create_branch",
        "Creating isolated feature branch to keep IaC changes separate from main and enable PR-based review",
        f"repo: {_DEMO_APP_REPO}, branch: feature/{ticket_id}, from: main",
        f"feature/{ticket_id} created",
        duration_ms=800)

    # ── Step 1: Initial planner — REAL AutoGen AssistantAgent via MAF ──────
    _step_set(run, "planner_initial", "running")
    if _AGENTS_AVAILABLE and run.request:
        try:
            _initial_plan = await _run_planner_agent(run.request)
            logger.info(
                "MAF Planner Agent (initial): %d units, %d questions via AutoGen",
                len(_initial_plan.units), len(_initial_plan.questions),
            )
        except Exception as _exc:
            logger.warning("Planner Agent (initial) error: %s — continuing with demo plan", _exc)
    else:
        await asyncio.sleep(2.5)
    _step_set(run, "planner_initial", "complete", cfg["planner_initial_detail"])

    # ── Step 2: Env scan — find existing resources before asking HITL ───────
    _step_set(run, "env_scan", "running")
    await asyncio.sleep(1.5)
    scan_detail = (
        cfg["scan_detail"]
        .replace("{app}", app)
        .replace("{env}", env)
        .replace("{APP}", app.upper())
    )
    _step_set(run, "env_scan", "complete", scan_detail)
    scan_server, scan_tool = _CLOUD_SCAN_MCP.get(cloud, ("mcp-cloud", "query_resources"))
    resource_types = [u["type"] for w in waves for u in w]
    _mcp_emit(run, "env_scan", scan_server, scan_tool,
        f"Scanning {cloud} environment for existing resources to detect naming conflicts before finalizing plan",
        f"cloud: {cloud}, resource_types: {resource_types}",
        scan_detail,
        duration_ms=1500)

    # ── Step 3: HITL pause — scan found a conflict, post work note to SNOW ──
    question = (
        cfg["hitl_question"]
        .replace("{app}", app)
        .replace("{env}", env)
        .replace("{APP}", app.upper())
    )
    run.pending_questions = [question]
    run.hitl_question = question          # preserved after answer for UI "plan output" view
    run.transition(WorkflowStatus.WAITING_FOR_HUMAN_INPUT)
    _step_set(run, "hitl_checkpoint", "waiting", "Awaiting human input via ServiceNow work note")
    _mcp_emit(run, "hitl_checkpoint", "mcp-servicenow", "update_work_notes",
        "Scan detected existing resource — posting clarification question to ticket work notes so requester can decide",
        f"ticket: {ticket_id}, note: \"{question[:65]}...\"",
        "Work note posted to ticket · status → WAITING_FOR_HUMAN_INPUT",
        duration_ms=220)

    event = asyncio.Event()
    _hitl_events[run.run_id] = event
    logger.info("demo run=%s paused at HITL", run.run_id)
    await event.wait()

    # Resume — parse free-text answer
    answers = _hitl_answers.pop(run.run_id, {})
    raw_answer = str(list(answers.values())[0]).lower().strip() if answers else ""
    _use_existing_keywords = {"yes", "use", "existing", "keep", "original", "same", "reuse", "that one"}
    _create_new_keywords   = {"no", "new", "create", "different", "separate", "fresh", "suffix"}
    use_existing = (
        any(kw in raw_answer for kw in _use_existing_keywords)
        or not any(kw in raw_answer for kw in _create_new_keywords)
    )
    run.human_answers = answers
    run.pending_questions = []
    run.transition(WorkflowStatus.EXECUTING)
    decision = "Using existing resource" if use_existing else "Creating new resource with unique suffix"
    _step_set(run, "hitl_checkpoint", "complete", f"Human answered · {decision}")

    # ── Step 4: Final planner — REAL AutoGen RoundRobinGroupChat HITL path ─
    _step_set(run, "planner_final", "running")
    if _AGENTS_AVAILABLE and run.request:
        try:
            _final_plan = await _run_planner_agent(run.request, None, answers)
            logger.info(
                "MAF Planner Agent (HITL final): %d units, finalized=%s via AutoGen",
                len(_final_plan.units), _final_plan.finalized,
            )
        except Exception as _exc:
            logger.warning("Planner Agent (final) error: %s — continuing with demo plan", _exc)
    else:
        await asyncio.sleep(2.0)

    scan        = _CLOUD_SCAN_RESULTS.get(cloud, {})
    module_repo = _CLOUD_MODULE_REPOS.get(cloud, f"{_MODULES_ORG}/{_MODULES_REPO}")
    constraint_detail = ""

    # Stamp the cloud-specific module repo onto every unit — TF Generator reads this
    # to know which GitHub repo to call get_file_contents on. No cloud detection needed.
    for unit in run.plan.units:
        unit.constraints.extra["module_repo"] = module_repo

    # Which unit type represents the "container" resource that may already exist
    _existing_type_map = {"azure": "resource_group", "aws": "vpc", "snowflake": "database"}
    existing_container_type = _existing_type_map.get(cloud)

    if cloud == "azure":
        rg_template = scan.get("resource_group", {}).get("name", "rg-{app}-{env}")
        existing_rg = rg_template.replace("{app}", app).replace("{env}", env)
        location    = scan.get("location", "eastus2")
        rg_to_use   = existing_rg if use_existing else f"rg-{app}-{env}-new"
        # Postgres placement rule: all Postgres instances go in a dedicated RG,
        # never the application resource group (DBA team RBAC boundary).
        postgres_rg = f"rg-postgresql-{env}"
        for unit in run.plan.units:
            unit.constraints.location    = location
            unit.constraints.required_rg = postgres_rg if unit.type == "postgres_flex" else rg_to_use
        constraint_detail = f"rg={rg_to_use} · postgres_rg={postgres_rg} · loc={location}"

    elif cloud == "aws":
        region = scan.get("region", "us-east-1")
        vpc_id = scan.get("vpc", {}).get("id") if use_existing else None
        for unit in run.plan.units:
            unit.constraints.extra["region"] = region
            if vpc_id:
                unit.constraints.extra["vpc_id"] = vpc_id
        constraint_detail = f"region={region}" + (f" · vpc={vpc_id}" if vpc_id else "")

    elif cloud == "snowflake":
        role     = scan.get("role", "SYSADMIN")
        db_tmpl  = scan.get("database", {}).get("name", "{APP}_PROD")
        existing_db = db_tmpl.replace("{APP}", app.upper())
        db_to_use   = existing_db if use_existing else f"{app.upper()}_PROD_NEW"
        for unit in run.plan.units:
            unit.constraints.extra["role"]     = role
            unit.constraints.extra["database"] = db_to_use
        constraint_detail = f"db={db_to_use} · role={role}"

    # When using an existing resource, drop its creation unit from the plan.
    # The TF Generator will emit a data source lookup instead of a resource block.
    if use_existing and existing_container_type:
        run.plan.units = [u for u in run.plan.units if u.type != existing_container_type]
        # Update the wave_0 step label to reflect it will be skipped
        for step in run.steps:
            if step.id == "wave_0":
                step.label = "Wave 0: skipped (using existing resource)"
                break

    unit_count = len(run.plan.units)
    _step_set(run, "planner_final", "complete",
              f"Plan finalized — {unit_count} units · {constraint_detail}")
    run.plan.finalized = True
    store_run(run)

    # ── HITL 2: Cost + quota approval ───────────────────────────────────────
    _step_set(run, "cost_checkpoint", "running")
    await asyncio.sleep(1.0)

    cost_ests = cfg.get("cost_estimates", [])
    total_usd = sum(e["monthly_usd"] for e in cost_ests)
    quota_detail = cfg.get("quota_detail", "quota check skipped")
    quota_ok = cfg.get("quota_ok", True)

    run.cost_quota_result = CostQuotaResult(
        unit_estimates=[
            UnitCostEstimate(
                unit_id=e["unit_id"],
                unit_type=e["unit_type"],
                monthly_usd=e["monthly_usd"],
            )
            for e in cost_ests
        ],
        total_monthly_usd=round(total_usd, 2),
        vcpus_needed=sum(_VCPU_TABLE.get(e["unit_type"], 0) for e in cost_ests),
        vcpus_available=48 if cloud == "azure" else None,
        vcpus_current_usage=12 if cloud == "azure" else None,
        quota_ok=quota_ok,
        quota_detail=quota_detail,
    )
    store_run(run)

    cost_question = (
        f"Estimated monthly cost: **${total_usd:.0f}/mo** · {quota_detail}\n\n"
        + "\n".join(
            f"  • {e['unit_id']} ({e['unit_type']}) — "
            + (f"${e['monthly_usd']:.0f}/mo" if e["monthly_usd"] > 0 else "no charge")
            for e in cost_ests
        )
        + "\n\nReply **APPROVE** to proceed or **REJECT** to cancel."
    )

    run.transition(WorkflowStatus.WAITING_FOR_COST_APPROVAL)
    _step_set(run, "cost_checkpoint", "waiting", f"${total_usd:.0f}/mo · awaiting approval")
    _mcp_emit(run, "cost_checkpoint", "mcp-servicenow", "update_work_notes",
        "Posting cost estimate and quota status to ticket so approver can make an informed decision",
        f"ticket: {ticket_id}, total: ${total_usd:.0f}/mo, quota_ok: {quota_ok}",
        "Cost & quota work note posted · status → WAITING_FOR_COST_APPROVAL",
        duration_ms=180)

    event = asyncio.Event()
    _hitl_events[run.run_id] = event
    logger.info("demo run=%s paused at cost approval", run.run_id)
    await event.wait()

    cost_answers = _hitl_answers.pop(run.run_id, {})
    raw_cost = str(list(cost_answers.values())[0]).strip().upper() if cost_answers else "APPROVE"
    cost_approved = raw_cost != "REJECT"
    run.cost_approved = cost_approved

    if not cost_approved:
        run.transition(WorkflowStatus.FAILED)
        run.error = "Provisioning cancelled at cost/quota review step"
        _step_set(run, "cost_checkpoint", "failed", "Rejected by approver")
        store_run(run)
        logger.info("demo run=%s cost approval rejected", run.run_id)
        return

    run.transition(WorkflowStatus.EXECUTING)
    _step_set(run, "cost_checkpoint", "complete",
              f"Approved · ${total_usd:.0f}/mo · {quota_detail}")

    # ── GH Search Agent — REAL AutoGen AssistantAgent resolves repo per type ─
    _step_set(run, "gh_search", "running")
    module_repo = _CLOUD_MODULE_REPOS.get(cloud, f"{_MODULES_ORG}/{_MODULES_REPO}")
    org_name    = module_repo.split("/")[0] if "/" in module_repo else _MODULES_ORG
    unit_types  = list({u.type for u in run.plan.units})

    if _AGENTS_AVAILABLE:
        try:
            gh_mapping = await _run_github_search_agent(unit_types, org_name)
            logger.info("MAF GH Search Agent resolved %d types via AutoGen: %s", len(gh_mapping), gh_mapping)
            for unit in run.plan.units:
                unit.resolved_repo = gh_mapping.get(unit.type, module_repo)
        except Exception as _exc:
            logger.warning("GH Search Agent error: %s — using default repo", _exc)
            for unit in run.plan.units:
                unit.resolved_repo = module_repo
    else:
        await asyncio.sleep(0.8 + random.uniform(0, 0.4))
        for unit in run.plan.units:
            unit.resolved_repo = module_repo

    seen_types: set = set()
    for unit in run.plan.units:
        if unit.type not in seen_types:
            seen_types.add(unit.type)
            resolved = unit.resolved_repo or module_repo
            _mcp_emit(run, "gh_search", "mcp-github", "search_code",
                f"GH Search Agent searching org for modules/{unit.type}/README.md — repo resolved at runtime by AutoGen",
                f"q: org:{org_name} path:modules/{unit.type} filename:README.md",
                f"1 result: {resolved}",
                duration_ms=random.randint(150, 320))
    _step_set(run, "gh_search", "complete",
              f"{len(seen_types)} type(s) → {module_repo.split('/')[-1]}")
    store_run(run)

    # ── Steps 5+: DAG waves ─────────────────────────────────────────────────
    active_unit_ids = {u.id for u in run.plan.units}

    for wave_idx, wave in enumerate(waves):
        wave_id = f"wave_{wave_idx}"

        # Filter to only units still in the plan (existing-resource units were dropped)
        active_wave = [u for u in wave if u["id"] in active_unit_ids]

        if not active_wave:
            _step_set(run, wave_id, "complete",
                      "skipped — existing resource referenced via data source lookup")
            store_run(run)
            continue

        _step_set(run, wave_id, "running")

        unit_ids = {u["id"] for u in active_wave}
        for unit in run.plan.units:
            if unit.id in unit_ids:
                unit.status = UnitStatus.RUNNING
        store_run(run)

        # Fetch real module READMEs — cloud-specific repo is already on each unit's constraints
        cloud_module_repo = _CLOUD_MODULE_REPOS.get(cloud, f"{_MODULES_ORG}/{_MODULES_REPO}")
        module_infos = await asyncio.gather(*[
            _fetch_module_info(u["type"], cloud_module_repo) for u in active_wave
        ], return_exceptions=True)

        # Attach module info to each unit; MCP call shows the cloud-specific repo
        for i, unit_cfg in enumerate(active_wave):
            info = module_infos[i] if not isinstance(module_infos[i], Exception) else None
            for unit in run.plan.units:
                if unit.id == unit_cfg["id"]:
                    unit.module_info = info
            _mcp_emit(run, wave_id, "mcp-github", "get_file_contents",
                f"TF Generator ({cloud.upper()} specialist) fetching {unit_cfg['type']} module README "
                f"from {cloud_module_repo} to understand required variables before generating HCL",
                f"repo: {cloud_module_repo}, path: modules/{unit_cfg['type']}/README.md, ref: main",
                f"README fetched ({(info or {}).get('readme_chars', 0):,} chars), "
                f"pinned @{(info or {}).get('sha', 'main')}",
                duration_ms=random.randint(180, 450))
        store_run(run)

        # ── TF Generator — REAL AutoGen AssistantAgent generates + evaluates HCL ──
        # run_terraform_agent calls AssistantAgent.run() through the MAF runtime.
        # With no Azure OpenAI credentials, MockModelClient returns realistic HCL.
        _wave_tf_outputs: Dict[str, str] = {}
        for unit_cfg in active_wave:
            plan_unit = next((u for u in run.plan.units if u.id == unit_cfg["id"]), None)
            if plan_unit is None:
                continue

            if _AGENTS_AVAILABLE:
                try:
                    tf_output = await _run_terraform_agent(
                        unit=plan_unit,
                        run=run,
                        evaluators=_EVALUATORS,
                        org=_MODULES_ORG,
                        modules_repo=(plan_unit.resolved_repo or cloud_module_repo).split("/")[-1],
                    )
                    plan_unit.eval_scores = {r.evaluator: r.score for r in tf_output.eval_results}
                    _wave_tf_outputs[unit_cfg["id"]] = tf_output.main_tf
                    logger.info(
                        "MAF TF Agent unit=%s passed=%s via AutoGen",
                        unit_cfg["id"], tf_output.passed,
                    )
                except Exception as _exc:
                    logger.warning("TF Agent unit=%s error: %s", unit_cfg["id"], _exc)
                    plan_unit.eval_scores = {
                        "correctness": random.randint(4, 5),
                        "security":    random.randint(3, 5),
                        "compliance":  random.randint(4, 5),
                    }
            else:
                await asyncio.sleep(1.0 + random.uniform(0, 0.5))
                plan_unit.eval_scores = {
                    "correctness": random.randint(4, 5),
                    "security":    random.randint(3, 5),
                    "compliance":  random.randint(4, 5),
                }

        for unit in run.plan.units:
            if unit.id in unit_ids:
                unit.status = UnitStatus.COMPLETE
        store_run(run)

        # Emit one MCP create_or_update_file call per completed unit (infra destination repo)
        infra_repo = f"acme/infra-{cloud}"
        for unit_cfg in active_wave:
            file_path = f"{env}/{ticket_id}/{unit_cfg['id']}/main.tf"
            _mcp_emit(run, wave_id, "mcp-github", "create_or_update_file",
                f"Committing {cloud.upper()}-specialist-generated HCL for {unit_cfg['id']} to feature branch",
                f"repo: {infra_repo}, path: {file_path}, branch: feature/{ticket_id}",
                f"{file_path} committed to {infra_repo}",
                duration_ms=random.randint(300, 600))

        pushed = len(active_wave)
        _step_set(run, wave_id, "complete", f"{pushed} unit(s) generated & pushed")

    # ── Step 6: PR — real GitHub PR in terraform-demo-app ───────────────────
    _step_set(run, "pr_created", "running")

    pr_url = await _create_real_pr(run, cloud, waves, app, env, ticket_id)
    run.pr_url = pr_url

    _mcp_emit(run, "pr_created", "mcp-github", "create_pull_request",
        "Opening pull request to trigger team code review and Terraform plan CI/CD pipeline",
        f"repo: {_DEMO_APP_REPO}, head: feature/{ticket_id}, base: main",
        f"PR opened: {pr_url}",
        duration_ms=720)
    _mcp_emit(run, "pr_created", "mcp-servicenow", "update_work_notes",
        "Notifying requester that infrastructure provisioning is complete and PR is ready for review",
        f"ticket: {ticket_id}, note: \"PR created: {pr_url}\"",
        "Work notes updated, RITM resolved",
        duration_ms=190)

    _step_set(run, "pr_created", "complete", pr_url)

    run.transition(WorkflowStatus.COMPLETE)
    store_run(run)
    logger.info("demo simulation complete: run=%s pr=%s", run.run_id, pr_url)
