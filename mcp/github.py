"""
github.py — GitHub REST API client for Terraform file management.

All calls go directly to the GitHub REST API via httpx.  In a production
deployment these could be replaced with @modelcontextprotocol/server-github
MCP tool calls using the MultiMCPClient pattern from snow-terraform-agent.

Public API
----------
search_module_repos         — find repos in org that contain a given module type
create_ticket_branch        — one branch per ticket: feature/{ticket_id}
read_module_readme          — fetch modules/{type}/README.md at runtime
get_latest_module_version   — latest commit SHA on main for a module path
push_unit_terraform         — push main.tf + variables.tf into env folder
create_pull_request         — one PR per ticket covering all units
"""

from __future__ import annotations

import base64
import logging
import os
from typing import List

import httpx

logger = logging.getLogger(__name__)

_GITHUB_API = "https://api.github.com"


# ---------------------------------------------------------------------------
# Auth headers
# ---------------------------------------------------------------------------


def _headers() -> dict:
    pat = os.environ.get("GITHUB_PERSONAL_ACCESS_TOKEN", "")
    h = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if pat:
        h["Authorization"] = f"Bearer {pat}"
    return h


def _raw_headers() -> dict:
    """Headers for raw file content responses."""
    return {**_headers(), "Accept": "application/vnd.github.raw+json"}


# ---------------------------------------------------------------------------
# Module metadata — fetched at runtime (Req 5 + 7)
# ---------------------------------------------------------------------------
# Module repo discovery — used by GH Search Agent
# ---------------------------------------------------------------------------


async def search_module_repos(
    module_type: str,
    org: str,
) -> list[str]:
    """Search the GitHub org for repos containing modules/{module_type}/README.md.

    Returns a list of repo names (most relevant first). Used by the GH Search
    Agent to resolve which repo holds a given module type — no hardcoding needed.
    Returns empty list if nothing found or search API is unavailable.
    """
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            f"{_GITHUB_API}/search/code",
            params={
                "q": f"org:{org} path:modules/{module_type} filename:README.md",
                "per_page": 5,
            },
            headers=_headers(),
            timeout=15,
        )

    if resp.status_code == 200:
        items = resp.json().get("items", [])
        # deduplicate, preserve order
        seen: set[str] = set()
        repos: list[str] = []
        for item in items:
            name = item["repository"]["name"]
            if name not in seen:
                seen.add(name)
                repos.append(name)
        logger.info("search_module_repos: %s → %s", module_type, repos)
        return repos

    logger.warning(
        "GitHub code search failed (%s) for module_type=%s", resp.status_code, module_type
    )
    return []


# ---------------------------------------------------------------------------


async def get_latest_module_version(
    module_type: str,
    org: str,
    modules_repo: str,
) -> str:
    """Return the latest commit SHA on main that touched modules/{module_type}/.

    Used to pin the module source to a specific commit hash in generated HCL,
    so every generated file references an exact, reproducible version.

    Returns the full SHA, or "main" if the path cannot be resolved.
    """
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            f"{_GITHUB_API}/repos/{org}/{modules_repo}/commits",
            params={"path": f"modules/{module_type}", "per_page": 1, "sha": "main"},
            headers=_headers(),
            timeout=15,
        )

    if resp.status_code == 200:
        commits = resp.json()
        if commits:
            sha = commits[0]["sha"]
            logger.info(
                "Latest commit for modules/%s in %s/%s: %s",
                module_type, org, modules_repo, sha[:7],
            )
            return sha

    logger.warning(
        "Could not resolve latest commit for modules/%s (%s): falling back to 'main'",
        module_type, resp.status_code,
    )
    return "main"


async def read_module_readme(
    module_type: str,
    org: str,
    modules_repo: str,
) -> str:
    """Fetch modules/{module_type}/README.md from the modules repo.

    The README content is injected into the TF Generator Agent prompt so the
    agent knows which module variables are required vs optional and what their
    defaults are (Req 7).

    Falls back to a minimal stub if the file is not found.
    """
    path = f"modules/{module_type}/README.md"
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            f"{_GITHUB_API}/repos/{org}/{modules_repo}/contents/{path}",
            headers=_raw_headers(),
            timeout=15,
        )

    if resp.status_code == 200:
        logger.info("Fetched README for modules/%s (%d chars)", module_type, len(resp.text))
        return resp.text

    logger.warning(
        "README not found at %s/%s/%s (%s)",
        org, modules_repo, path, resp.status_code,
    )
    return (
        f"# {module_type} module\n"
        f"README not found at {org}/{modules_repo}/{path}.\n"
        f"Generate HCL using standard module conventions.\n"
    )


# ---------------------------------------------------------------------------
# Branch management — one branch per ticket (Req 1)
# ---------------------------------------------------------------------------


async def create_ticket_branch(
    ticket_id: str,
    org: str,
    repo: str,
    base_branch: str = "main",
) -> str:
    """Create feature/{ticket_id} off base_branch.

    Returns the branch name.  If the branch already exists (idempotent
    re-run), logs a warning and returns the existing branch name.
    """
    branch = f"feature/{ticket_id}"

    async with httpx.AsyncClient() as client:
        # Resolve base branch SHA
        ref_resp = await client.get(
            f"{_GITHUB_API}/repos/{org}/{repo}/git/ref/heads/{base_branch}",
            headers=_headers(),
            timeout=15,
        )
        ref_resp.raise_for_status()
        sha = ref_resp.json()["object"]["sha"]

        # Create branch
        create_resp = await client.post(
            f"{_GITHUB_API}/repos/{org}/{repo}/git/refs",
            json={"ref": f"refs/heads/{branch}", "sha": sha},
            headers=_headers(),
            timeout=15,
        )

    if create_resp.status_code == 422:
        logger.warning(
            "Branch %s already exists in %s/%s — reusing for idempotent re-run",
            branch, org, repo,
        )
    elif not create_resp.is_success:
        create_resp.raise_for_status()
    else:
        logger.info("Created branch %s in %s/%s", branch, org, repo)

    return branch


# ---------------------------------------------------------------------------
# File push — environment-scoped paths (Req 8)
# ---------------------------------------------------------------------------


async def push_unit_terraform(
    branch: str,
    ticket_id: str,
    unit_id: str,
    environment: str,
    main_tf: str,
    variables_tf: str,
    org: str,
    repo: str,
) -> None:
    """Push main.tf and variables.tf for one unit into the correct env folder.

    File paths follow: {environment}/{ticket_id}/{unit_id}/main.tf
                       {environment}/{ticket_id}/{unit_id}/variables.tf

    Creates or updates each file (handles re-runs via SHA lookup).
    """
    base_path = f"{environment}/{ticket_id}/{unit_id}"
    files = {
        f"{base_path}/main.tf": main_tf,
        f"{base_path}/variables.tf": variables_tf,
    }

    async with httpx.AsyncClient() as client:
        for path, content in files.items():
            encoded = base64.b64encode(content.encode()).decode()

            # Check if file already exists — needed to get SHA for updates
            check = await client.get(
                f"{_GITHUB_API}/repos/{org}/{repo}/contents/{path}",
                params={"ref": branch},
                headers=_headers(),
                timeout=15,
            )

            body: dict = {
                "message": f"feat: provision {unit_id} for {ticket_id} [{environment}]",
                "content": encoded,
                "branch": branch,
            }
            if check.status_code == 200:
                body["sha"] = check.json()["sha"]  # required for in-place update

            resp = await client.put(
                f"{_GITHUB_API}/repos/{org}/{repo}/contents/{path}",
                json=body,
                headers=_headers(),
                timeout=30,
            )
            resp.raise_for_status()
            logger.info("Pushed %s to %s/%s@%s", path, org, repo, branch)


# ---------------------------------------------------------------------------
# Pull request — one PR per ticket (Req 1)
# ---------------------------------------------------------------------------


async def create_pull_request(
    ticket_id: str,
    environment: str,
    org: str,
    repo: str,
    branch: str,
    unit_ids: List[str],
    description: str = "",
) -> str:
    """Open a single PR covering all units for this ticket.

    Returns the HTML PR URL.
    """
    unit_list = "\n".join(f"- `{uid}`" for uid in unit_ids)
    body = (
        f"## Terraform Provisioning\n\n"
        f"**Ticket:** `{ticket_id}`\n"
        f"**Environment:** `{environment}`\n\n"
        f"### Resources provisioned\n{unit_list}\n\n"
        f"**Output path:** `{environment}/{ticket_id}/`\n\n"
    )
    if description:
        body += f"### Notes\n{description}\n"

    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"{_GITHUB_API}/repos/{org}/{repo}/pulls",
            json={
                "title": f"Provision: {ticket_id} [{environment}]",
                "body": body,
                "head": branch,
                "base": "main",
            },
            headers=_headers(),
            timeout=15,
        )
        resp.raise_for_status()

    pr_url: str = resp.json()["html_url"]
    logger.info("Created PR for %s: %s", ticket_id, pr_url)
    return pr_url


# ---------------------------------------------------------------------------
# Demo mode — replace all public functions with fast stubs when DEMO_MODE=true.
# Set os.environ["DEMO_MODE"] = "true" before importing this module.
# Agents that do `from mcp.github import X` will receive the mock version
# because this block runs at import time and overwrites the module-level names.
# ---------------------------------------------------------------------------
if os.environ.get("DEMO_MODE", "false").lower() == "true":
    from mcp.mock_mcp import (  # noqa: F811  (intentional override)
        search_module_repos,
        read_module_readme,
        get_latest_module_version,
        create_ticket_branch,
        push_unit_terraform,
        create_pull_request,
    )
    logger.info("mcp.github: DEMO_MODE active — using mock implementations")
