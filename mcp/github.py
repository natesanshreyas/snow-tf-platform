"""
github.py — GitHub MCP stubs.

In production these calls go through the @modelcontextprotocol/server-github
MCP stdio server, exactly as implemented in snow-terraform-agent.

TODO: Replace stubs with real MCP client calls using MultiMCPClient from
      snow-terraform-agent/src/multi_mcp_client.py (or equivalent).
"""

from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)


async def push_terraform_files(
    ticket_id: str,
    unit_id: str,
    main_tf: str,
    variables_tf: str,
) -> str:
    """Push Terraform files to GitHub and open a PR.

    Returns the PR URL.

    TODO: Implement using MCP tools:
      1. github__create_branch(owner, repo, branch=f"provision/{ticket_id}/{unit_id}", from_branch="main")
      2. github__push_files(owner, repo, branch, files=[
           {"path": f"provisioned/{ticket_id}/{unit_id}/main.tf", "content": main_tf},
           {"path": f"provisioned/{ticket_id}/{unit_id}/variables.tf", "content": variables_tf},
         ])
      3. github__create_pull_request(owner, repo, title, body, head, base)
    """
    github_org  = os.environ.get("GITHUB_ORG", "your-org")
    github_repo = os.environ.get("GITHUB_TERRAFORM_REPO", "terraform-modules")
    branch      = f"provision/{ticket_id}/{unit_id}"

    logger.info(
        "STUB: would push to %s/%s branch=%s (main.tf %d chars)",
        github_org, github_repo, branch, len(main_tf),
    )

    # TODO: real MCP call
    stub_pr_url = f"https://github.com/{github_org}/{github_repo}/pull/STUB-{ticket_id}-{unit_id}"
    return stub_pr_url


async def read_module_readme(module_type: str) -> str:
    """Read a Terraform module README/example via GitHub MCP.

    TODO: Implement using MCP tool:
      github__get_file_contents(owner, repo, path=f"modules/{module_type}/README.md")
    """
    logger.info("STUB: would read module README for %s", module_type)
    return f"# {module_type} module\n# TODO: real module README via GitHub MCP"
