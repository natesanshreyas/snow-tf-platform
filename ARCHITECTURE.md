# snow-tf-platform — End-to-End Architecture

## Overview

```mermaid
flowchart TD
    SNOW(["🎫 ServiceNow\nApproved RITM"])
    PR(["GitHub Pull Request\nTerraform HCL"])

    subgraph PIPELINE["Agent Pipeline"]
        direction TB
        A1["① Router Agent\nticket → azure / aws / snowflake"]
        A2["② Planner Agent\nticket → typed resource units"]
        HITL1{{"HITL 1 — Resource Conflict\nuse existing resource or create new?"}}
        SCAN["Environment Scan\nquery existing cloud resources"]
        COST{{"HITL 2 — Cost & Quota\n~$X/mo · vCPU quota OK? → APPROVE / REJECT"}}
        A3["③ GH Search Agent\nresolve Terraform module repo at runtime"]
        A4["④ TF Generator × N\nHCL per unit · eval loop · parallel waves"]
        A1 --> A2 --> HITL1 --> SCAN --> COST --> A3 --> A4
    end

    subgraph MCP["MCP Servers"]
        MG["mcp-github"]
        MS["mcp-servicenow"]
        MC["mcp-cloud\n(azure-resource-graph / aws-config / snowflake)"]
    end

    SNOW -->|"webhook + sys_id"| PIPELINE
    HITL1 <-->|"work note: conflict question"| MS
    COST  <-->|"work note: cost + quota summary"| MS
    SCAN  <-->|"query resources"| MC
    A3    <-->|"search_code"| MG
    A4    <-->|"README fetch · HCL commit"| MG
    MG    -->|"create_pull_request"| PR
    PR    -->|"pr_url"| MS
    MS    -->|"work note"| SNOW

    classDef io      fill:#f0fdf4,stroke:#22c55e,color:#14532d
    classDef agent   fill:#eff6ff,stroke:#3b82f6,color:#1e3a8a
    classDef mcp     fill:#faf5ff,stroke:#a855f7,color:#581c87
    classDef hitl    fill:#fffbeb,stroke:#f59e0b,color:#78350f

    class SNOW,PR io
    class A1,A2,SCAN,A3,A4 agent
    class MG,MS,MC mcp
    class HITL1,COST hitl
```

## Detailed flow

```mermaid
flowchart TD
    subgraph INBOUND["Inbound"]
        SNOW["🎫 ServiceNow RITM"]
        DEMO_UI["🖥️ Demo UI (Next.js)"]
        PROXY["API Proxy /api/snow-demo/**"]
    end

    subgraph SERVER["FastAPI — server.py"]
        HOOK_APPROVAL["POST /webhook/snow/approval"]
        HOOK_UPDATE["POST /webhook/snow/update"]
        DEMO_SUBMIT["POST /demo/submit"]
        DEMO_RESUME["POST /demo/resume/{run_id}"]
        STATUS["GET /runs/{run_id}"]
    end

    subgraph ROUTER["Agent 1 — Router"]
        LLM_ROUTE["Azure OpenAI"]
        HEURISTIC["Keyword fallback"]
        CLOUD_OUT["cloud + reasoning"]
        LLM_ROUTE --> CLOUD_OUT
        HEURISTIC -->|fallback| CLOUD_OUT
    end

    subgraph ORCH["WorkflowEngine"]
        WF_STATE["WorkflowRun state machine"]
        DAG["DAG executor · topological waves"]
        STORE["In-memory run store"]
    end

    subgraph PIPELINE["Agent Pipeline (asyncio background)"]
        direction TB
        BRANCH["Branch: feature/{ticket_id}"]

        subgraph A2["Agent 2 — Planner"]
            PLAN_INIT["Initial plan → PlanUnits"]
            HITL1_PAUSE{"HITL 1\nResource Conflict"}
            ENV_SCAN["Environment Scan"]
            PLAN_FINAL["Final plan (finalized)"]
            PLAN_INIT --> HITL1_PAUSE
            HITL1_PAUSE -->|no questions| ENV_SCAN
            ENV_SCAN --> PLAN_FINAL
        end

        COST_CHECK["Cost + Quota Check\n~$X/mo · vCPU available?"]
        HITL2_PAUSE{"HITL 2\nCost & Quota"}
        COST_CHECK --> HITL2_PAUSE

        subgraph A3["Agent 3 — GH Search"]
            GH_SEARCH["search_code per module type"]
            GH_RESOLVE["unit_type → repo"]
            GH_SEARCH --> GH_RESOLVE
        end

        subgraph A4["Agent 4 — TF Generator (parallel waves)"]
            WAVE0["Wave 0 · e.g. RG / VPC / Database"]
            WAVE1["Wave 1 · e.g. Postgres + Storage"]
            WAVE0 --> WAVE1
        end

        subgraph UNIT_LOOP["Per Unit"]
            MOD_FETCH["Fetch module README + SHA"]
            TF_GEN["Generate HCL"]
            EVAL["Eval: correctness · security · compliance"]
            RETRY{"score ≥ 3?"}
            PUSH_FILE["Push main.tf to branch"]
            MOD_FETCH --> TF_GEN --> EVAL --> RETRY
            RETRY -->|yes| PUSH_FILE
            RETRY -->|no| TF_GEN
        end

        PR_CREATE["Create Pull Request"]
        DONE["✅ COMPLETE"]
    end

    subgraph EXTERNAL["External Services"]
        AZ_OAI["Azure OpenAI"]
        GH_ORG["GitHub · search_code"]
        GH_MODULES["GitHub · module READMEs"]
        GH_APP["GitHub · terraform repo"]
        AZ_GRAPH["Azure Resource Graph"]
        SNOW_API["ServiceNow REST API\nPATCH work_notes (write-only)"]
    end

    subgraph FRONTEND["Frontend · SnowProvisioningDemo.tsx"]
        POLL["Poll every 1.2 s"]
        STEPS_UI["Step timeline"]
        HITL_UI["HITL 1 work note thread"]
        COST_UI["HITL 2 work note thread\nAPPROVE / REJECT"]
        PR_LINK["Open PR"]
    end

    SNOW -->|"webhook + sys_id"| HOOK_APPROVAL
    DEMO_UI --> PROXY --> DEMO_SUBMIT
    HOOK_APPROVAL --> ROUTER
    DEMO_SUBMIT --> ROUTER
    LLM_ROUTE <-->|ticket text| AZ_OAI
    CLOUD_OUT --> ORCH
    ORCH --> PIPELINE
    BRANCH --> A2

    HITL1_PAUSE -->|"work note: conflict question"| SNOW_API
    SNOW -->|"human reply"| HOOK_UPDATE
    DEMO_UI -->|"HITL 1 answer"| DEMO_RESUME
    HOOK_UPDATE --> ORCH
    DEMO_RESUME --> ORCH
    ORCH -->|resume| ENV_SCAN
    ENV_SCAN <-->|query| AZ_GRAPH

    PLAN_FINAL --> COST_CHECK
    HITL2_PAUSE -->|"work note: cost summary"| SNOW_API
    DEMO_UI -->|"APPROVE / REJECT"| DEMO_RESUME
    ORCH -->|resume| HITL2_PAUSE
    HITL2_PAUSE -->|APPROVE| A3

    GH_SEARCH <-->|search_code| GH_ORG
    GH_RESOLVE --> A4
    A4 --> UNIT_LOOP
    MOD_FETCH <-->|README + SHA| GH_MODULES
    TF_GEN <-->|generate HCL| AZ_OAI
    PUSH_FILE -->|commit file| GH_APP

    UNIT_LOOP --> PR_CREATE
    PR_CREATE -->|create PR| GH_APP
    PR_CREATE --> DONE
    DONE -->|pr_url work note| SNOW_API
    SNOW_API --> SNOW

    DEMO_UI --> POLL
    POLL -->|GET| STATUS
    STATUS --> STEPS_UI
    STATUS -->|WAITING_FOR_HUMAN_INPUT| HITL_UI
    STATUS -->|WAITING_FOR_COST_APPROVAL| COST_UI
    STATUS -->|COMPLETE| PR_LINK
    PR_LINK --> GH_APP

    classDef ext    fill:#f0f4ff,stroke:#6b7dba,color:#1a237e
    classDef agent  fill:#e8f5e9,stroke:#388e3c,color:#1b5e20
    classDef server fill:#fff8e1,stroke:#f9a825,color:#4a3700
    classDef ui     fill:#fce4ec,stroke:#c2185b,color:#880e4f
    classDef store  fill:#f3e5f5,stroke:#7b1fa2,color:#4a148c
    classDef hitl   fill:#fffbeb,stroke:#f59e0b,color:#78350f

    class AZ_OAI,GH_ORG,GH_MODULES,GH_APP,AZ_GRAPH,SNOW_API,SNOW ext
    class PLAN_INIT,PLAN_FINAL,TF_GEN,MOD_FETCH,ENV_SCAN,GH_SEARCH,GH_RESOLVE,COST_CHECK agent
    class HOOK_APPROVAL,HOOK_UPDATE,DEMO_SUBMIT,DEMO_RESUME,STATUS,PROXY server
    class DEMO_UI,POLL,STEPS_UI,HITL_UI,COST_UI,PR_LINK ui
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
