# snow-tf-platform — End-to-End Architecture

## Simple overview

```mermaid
flowchart TD
    SNOW(["🎫 ServiceNow\nApproved RITM"])

    subgraph AGENTS["  Agent Pipeline  "]
        direction TB
        A1["① Router Agent\nLLM reads ticket → azure · aws · snowflake"]
        A2["② Planner Agent\ndecompose ticket → resource units + HITL questions"]
        HITL1{{"HITL 1 — Resource Conflict\nresolve existing-resource questions (optional)"}}
        SCAN["Environment Scan\nquery existing cloud resources (deterministic)"]
        COST{{"HITL 2 — Cost & Quota\nestimate monthly cost · vCPU quota check"}}
        A3["③ GH Search Agent\nsearch GitHub org → resolve repo per module type"]
        A4["④ TF Generator × N\nHCL per unit · evaluate · retry  (parallel waves)"]
        A1 --> A2 --> HITL1 --> SCAN --> COST --> A3 --> A4
    end

    subgraph MCP["  MCP Tool Calls  "]
        MG["mcp-github\nsearch_code · get_file_contents\ncreate_or_update_file · create_pull_request"]
        MS["mcp-servicenow\nupdate_work_notes  (write-only)"]
        MC["mcp-azure-resource-graph\nmcp-aws-config · mcp-snowflake"]
    end

    PR(["GitHub Pull Request\nTerraform HCL · pinned module refs"])

    SNOW -->|"webhook + sys_id"| AGENTS

    HITL1 <-->|"POST conflict question"| MS
    COST  <-->|"POST cost + quota summary"| MS
    SCAN  <-->|"query resources"| MC
    A3    <-->|"search_code per module type"| MG
    A4    <-->|"fetch READMEs · commit HCL"| MG

    MG -->|"create_pull_request"| PR
    PR -->|"pr_url"| MS
    MS -->|"update_work_notes"| SNOW

    classDef io      fill:#f0fdf4,stroke:#22c55e,color:#14532d
    classDef agent   fill:#eff6ff,stroke:#3b82f6,color:#1e3a8a
    classDef mcpnode fill:#faf5ff,stroke:#a855f7,color:#581c87
    classDef hitl    fill:#fffbeb,stroke:#f59e0b,color:#78350f

    class SNOW,PR io
    class A1,A2,SCAN,A3,A4 agent
    class MG,MS,MC mcpnode
    class HITL1,COST hitl
```

## Detailed breakdown

```mermaid
flowchart TD
    %% ── Inbound ──────────────────────────────────────────────────────────────
    subgraph INBOUND["Inbound Layer"]
        SNOW["🎫 ServiceNow\nApproved RITM webhook\n(includes sys_id)"]
        DEMO_UI["🖥️ Demo UI\n(Next.js — /snow)"]
        PROXY["Next.js API Proxy\n/api/snow-demo/**"]
    end

    %% ── FastAPI Server ───────────────────────────────────────────────────────
    subgraph SERVER["FastAPI  ·  server.py"]
        HOOK_APPROVAL["POST /webhook/snow/approval"]
        HOOK_UPDATE["POST /webhook/snow/update"]
        DEMO_SUBMIT["POST /demo/submit"]
        DEMO_RESUME["POST /demo/resume/{run_id}"]
        STATUS["GET /runs/{run_id}"]
    end

    %% ── Agent 1: Router ─────────────────────────────────────────────────────
    subgraph ROUTER["Agent 1 — Router  ·  router_agent.py"]
        LLM_ROUTE["Azure OpenAI\nchat/completions"]
        HEURISTIC["Keyword heuristic\n(fallback — no credentials)"]
        CLOUD_OUT["→ cloud + reasoning\n(azure | aws | snowflake)"]
    end

    %% ── Orchestrator ─────────────────────────────────────────────────────────
    subgraph ORCH["WorkflowEngine  ·  workflow_engine.py"]
        WF_STATE["WorkflowRun\nstate machine"]
        DAG["DAG executor\n(topological wave sort)"]
        STORE["In-memory store\nload_run / store_run"]
    end

    %% ── Agent Pipeline ───────────────────────────────────────────────────────
    subgraph PIPELINE["Agent Pipeline  (asyncio background task)"]
        direction TB
        BRANCH["Branch Created\nfeature/{ticket_id}"]

        subgraph A2["Agent 2 — Planner  ·  planner_agent.py"]
            PLAN_INIT["Initial plan\ndecompose ticket → PlanUnits + questions"]
            HITL1_PAUSE{"HITL 1 — Resource Conflict\nquestions?"}
            ENV_SCAN["Environment Scan\nquery existing resources (deterministic)"]
            PLAN_FINAL["Final plan\nfinalized Plan (0 questions)"]
            PLAN_INIT --> HITL1_PAUSE
            HITL1_PAUSE -->|"no questions"| ENV_SCAN
            ENV_SCAN --> PLAN_FINAL
        end

        COST_CHECK["Cost + Quota Check\nestimate monthly cost · vCPU quota (deterministic)"]
        HITL2_PAUSE{"HITL 2 — Cost & Quota\nAPPROVE or REJECT?"}
        COST_CHECK --> HITL2_PAUSE

        subgraph A3["Agent 3 — GH Search  ·  github_search_agent.py"]
            GH_SEARCH["search_code per module type\nresolves unit.resolved_repo"]
            GH_RESOLVE["→ {unit_type: repo_name}\nno hardcoded repo names"]
            GH_SEARCH --> GH_RESOLVE
        end

        subgraph DAG_WAVES["Agent 4 — TF Generator  ·  terraform_agent.py  (per wave, parallel)"]
            direction LR
            WAVE0["Wave 0\ne.g. VPC / RG / Database"]
            WAVE1["Wave 1\ne.g. RDS + S3 / Postgres + Storage / Schema + WH"]
        end

        subgraph UNIT_LOOP["Per Unit"]
            direction TB
            MOD_FETCH["Fetch module README + SHA\nfrom unit.resolved_repo"]
            TF_GEN["TF Generator Agent\ngenerate HCL for unit"]
            EVAL["Evaluators\ncorrectness · security · compliance"]
            RETRY{"score ≥ 3?"}
            PUSH_FILE["Push main.tf to branch"]
        end

        PR_CREATE["Create Pull Request\nnatesanshreyas/terraform-demo-app"]
        DONE["✅ WorkflowStatus: COMPLETE"]
    end

    %% ── External Services ────────────────────────────────────────────────────
    subgraph EXTERNAL["External Services"]
        AZ_OAI["☁️ Azure OpenAI\nchat/completions"]
        GH_ORG["GitHub Org\nsearch_code — find module repos at runtime"]
        GH_MODULES["GitHub\nmodule READMEs + commit SHAs"]
        GH_APP["GitHub\nnatesanshreyas/terraform-demo-app\nbranches + files + PRs"]
        AZ_GRAPH["Azure Resource Graph\nquery existing resources"]
        SNOW_API["ServiceNow REST API\nPATCH work_notes  (write-only · uses sys_id from webhook)"]
    end

    %% ── Frontend Polling ─────────────────────────────────────────────────────
    subgraph FRONTEND["Frontend  ·  SnowProvisioningDemo.tsx"]
        direction LR
        POLL["Poll /runs/{run_id}\nevery 1.2 s"]
        STEPS_UI["Step timeline\n(pending→running→complete)"]
        UNIT_UI["Unit cards\nwave · eval scores · module info"]
        GHSEARCH_UI["GH Search panel\nunit_type → resolved repo"]
        HITL_UI["HITL 1 inline thread\nshow conflict question · collect answer"]
        COST_UI["HITL 2 inline thread\ncost + quota · APPROVE / REJECT"]
        PR_LINK["🔗 Open PR link"]
    end

    %% ── Connections ──────────────────────────────────────────────────────────

    SNOW -->|"webhook + sys_id\nPOST /webhook/snow/approval"| HOOK_APPROVAL
    DEMO_UI --> PROXY --> DEMO_SUBMIT

    HOOK_APPROVAL --> ROUTER
    DEMO_SUBMIT --> ROUTER

    LLM_ROUTE <-->|"ticket text"| AZ_OAI
    LLM_ROUTE -->|"success"| CLOUD_OUT
    HEURISTIC -->|"fallback"| CLOUD_OUT

    CLOUD_OUT --> ORCH
    ORCH --> PIPELINE

    BRANCH --> A2

    HITL1_PAUSE -->|"has questions — POST work_notes"| SNOW_API
    SNOW_API -->|"work_notes updated"| SNOW
    SNOW -->|"human replies\nPOST /webhook/snow/update"| HOOK_UPDATE
    DEMO_UI -->|"HITL 1 answer\nPOST /demo/resume/{run_id}"| DEMO_RESUME
    HOOK_UPDATE --> ORCH
    DEMO_RESUME --> ORCH
    ORCH -->|"resume HITL 1"| ENV_SCAN

    ENV_SCAN <-->|"ARM query"| AZ_GRAPH
    PLAN_FINAL --> COST_CHECK
    HITL2_PAUSE -->|"cost + quota summary — POST work_notes"| SNOW_API
    DEMO_UI -->|"HITL 2 APPROVE/REJECT\nPOST /demo/resume/{run_id}"| DEMO_RESUME
    ORCH -->|"resume HITL 2"| HITL2_PAUSE
    HITL2_PAUSE -->|"APPROVE"| A3

    GH_SEARCH <-->|"search_code per type"| GH_ORG
    GH_RESOLVE --> DAG_WAVES

    WAVE0 --> WAVE1
    WAVE0 --> UNIT_LOOP
    WAVE1 --> UNIT_LOOP

    MOD_FETCH <-->|"contents + commits\nfrom resolved_repo"| GH_MODULES
    MOD_FETCH --> TF_GEN
    TF_GEN <-->|"generate HCL"| AZ_OAI
    TF_GEN --> EVAL
    EVAL --> RETRY
    RETRY -->|"yes"| PUSH_FILE
    RETRY -->|"no — inject feedback"| TF_GEN
    PUSH_FILE -->|"PUT /contents"| GH_APP

    UNIT_LOOP --> PR_CREATE
    PR_CREATE -->|"POST /pulls"| GH_APP
    PR_CREATE --> DONE
    DONE -->|"PATCH work_notes + pr_url"| SNOW_API

    DEMO_UI --> POLL
    POLL -->|"GET /runs/{run_id}"| STATUS
    STATUS --> STEPS_UI
    STATUS --> UNIT_UI
    STATUS --> GHSEARCH_UI
    STATUS -->|"status = WAITING_FOR_HUMAN_INPUT"| HITL_UI
    STATUS -->|"status = WAITING_FOR_COST_APPROVAL"| COST_UI
    STATUS -->|"status = COMPLETE + pr_url"| PR_LINK
    PR_LINK -->|"opens"| GH_APP

    %% ── Styles ───────────────────────────────────────────────────────────────
    classDef ext    fill:#f0f4ff,stroke:#6b7dba,color:#1a237e
    classDef agent  fill:#e8f5e9,stroke:#388e3c,color:#1b5e20
    classDef server fill:#fff8e1,stroke:#f9a825,color:#4a3700
    classDef ui     fill:#fce4ec,stroke:#c2185b,color:#880e4f
    classDef store  fill:#f3e5f5,stroke:#7b1fa2,color:#4a148c
    classDef hitl   fill:#fffbeb,stroke:#f59e0b,color:#78350f

    class AZ_OAI,GH_ORG,GH_MODULES,GH_APP,AZ_GRAPH,SNOW_API,SNOW ext
    class PLAN_INIT,PLAN_FINAL,TF_GEN,MOD_FETCH,ENV_SCAN,GH_SEARCH,GH_RESOLVE,COST_CHECK agent
    class HOOK_APPROVAL,HOOK_UPDATE,DEMO_SUBMIT,DEMO_RESUME,STATUS,PROXY server
    class DEMO_UI,POLL,STEPS_UI,UNIT_UI,GHSEARCH_UI,HITL_UI,COST_UI,PR_LINK ui
    class STORE,WF_STATE,DAG store
    class HITL1_PAUSE,HITL2_PAUSE hitl
```

## Component Summary

| Layer | Component | Agent? | LLM? | Notes |
|-------|-----------|--------|------|-------|
| **Ingestion** | `server.py` FastAPI | — | — | Parses webhook; passes `sys_id` from BR payload |
| **Agent 1 — Router** | `router_agent.py` | ✅ | ✅ Azure OpenAI | Reads ticket → azure / aws / snowflake; keyword fallback |
| **Agent 2 — Planner** | `planner_agent.py` | ✅ | ✅ Azure OpenAI | Ticket → typed units + dependency order + HITL 1 questions |
| **HITL 1 — Resource Conflict** | `workflow.py` step 2 | — | — | Pauses at `WAITING_FOR_HUMAN_INPUT`; human resolves existing-resource conflicts |
| **Env scan** | `environment_scan.py` | — | — | ARM Resource Graph query — deterministic, no LLM |
| **Cost + Quota** | `cost_quota.py` | — | — | Estimates monthly cost (price table) + vCPU quota (ARM API) — deterministic |
| **HITL 2 — Cost & Quota** | `workflow.py` step 5.5 | — | — | Pauses at `WAITING_FOR_COST_APPROVAL`; human APPROVE or REJECT |
| **Agent 3 — GH Search** | `github_search_agent.py` | ✅ | ✅ (ambiguous only) | Searches GitHub org at runtime to resolve repo per module type |
| **Agent 4 — TF Generator** | `terraform_agent.py` | ✅ | ✅ Azure OpenAI | README → HCL; correctness/security/compliance eval loop |
| **Evaluators** | `evaluators/*.py` | — | — | Pattern checks: correctness · security · compliance |
| **Orchestration** | `workflow_engine.py` | — | — | State machine + topological DAG sort |
| **SNOW MCP** | `mcp/servicenow.py` | — | — | Write-only — `PATCH work_notes` using `sys_id` from webhook |
| **GitHub MCP** | `mcp/github.py` | — | — | `search_code` · `get_file_contents` · `create_pull_request` |
| **Frontend** | `SnowProvisioningDemo.tsx` | — | — | 1.2 s polling · inline SNOW work note threads · eval rings |

## SNOW MCP surface

ServiceNow is only ever written to — never read. The `sys_id` arrives in the original webhook payload (`current.sys_id` from the Business Rule), eliminating any GET lookup.

| Direction | Trigger | Method |
|-----------|---------|--------|
| SNOW → platform | Approved RITM | `POST /webhook/snow/approval` |
| SNOW → platform | Human answers HITL 1 question | `POST /webhook/snow/update` (`WAITING_FOR_HUMAN_INPUT`) |
| SNOW → platform | Human approves/rejects cost | `POST /webhook/snow/update` (`WAITING_FOR_COST_APPROVAL`) |
| Platform → SNOW | Planner raises HITL 1 question | `PATCH work_notes` |
| Platform → SNOW | Cost + quota summary (HITL 2) | `PATCH work_notes` with breakdown + APPROVE/REJECT instructions |
| Platform → SNOW | Workflow complete | `PATCH work_notes` + PR URL |
