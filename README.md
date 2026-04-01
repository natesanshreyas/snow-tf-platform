# snow-tf-platform

ServiceNow → Terraform automation platform. Receives post-approval ITSM webhooks and provisions cloud infrastructure by generating, evaluating, and PR-ing Terraform code.

---

## What is agentic vs non-agentic

This is the most important architectural boundary in the system.

### Non-agentic (deterministic Python — no LLM)

| Component | File | What it does |
|---|---|---|
| Webhook receiver | `orchestrator/server.py` | Parses SNOW payload, determines request type |
| Workflow engine | `orchestrator/workflow_engine.py` | Manages state, executes DAG, retries |
| DAG executor | `workflow_engine.py:execute_dag` | Topological sort, concurrent wave execution |
| Environment scan | `agents/azure/environment_scan.py` | Queries Azure Resource Graph — no reasoning |
| Evaluators | `evaluators/*.py` | Pattern/string checks on generated HCL |
| MCP stubs | `mcp/*.py` | GitHub + ServiceNow API calls |

**Why non-agentic?** These components deal with deterministic facts: what exists, what state the workflow is in, whether a tag is present. An LLM adds latency, cost, and non-determinism with zero benefit here.

### Agentic (Microsoft Agent Framework — LLM-driven)

| Component | File | What it does |
|---|---|---|
| Planner Agent | `agents/azure/planner_agent.py` | Interprets request, decomposes into units, applies constraints, raises questions |
| TF Generator Agent | `agents/azure/terraform_agent.py` | Generates HCL per unit, retries based on evaluator feedback |

**Why agentic?** These tasks require natural language understanding and code generation. The planner must reason about ambiguous descriptions ("provision something for cortex-dev") and apply soft constraints. The TF generator must produce valid HCL and respond to evaluator feedback in natural language.

---

## Where Microsoft Agent Framework is used and why

Both agents use **Microsoft Agent Framework RC5** (`agent-framework` PyPI package).

MAF was chosen over a hand-rolled loop (like `snow-terraform-agent`) for two reasons:

1. **Explicit agent boundary** — MAF's `SingleAgentRuntime` makes it clear that a reasoning loop is happening. A `for` loop with `chat_completion_with_tools` works but blurs the line.
2. **Forward compatibility** — MAF is the strategic Microsoft direction (successor to Semantic Kernel + AutoGen). When it hits GA, upgrading will be incremental rather than a rewrite.

**What MAF replaces from the hand-rolled approach:**
- The `for _loop in range(max_iterations)` loop → `SingleAgentRuntime` manages it
- The `messages[]` list → MAF manages conversation history
- Manual tool dispatch (`_dispatch()`) → MAF routes tool calls

**What stays the same:**
- Azure OpenAI endpoint
- Tool schemas
- Evaluator functions
- MCP subprocess management (MAF has native MCP support — wire in when GA)

> ⚠️ MAF is RC5 as of March 2026 — not GA. Import paths and method signatures may change. See the migration guide: https://learn.microsoft.com/en-us/agent-framework/migration-guide/

---

## Where concurrency comes from

Concurrency exists at two levels:

### 1. Unit-level concurrency within a wave (DAG executor)

The `topological_sort()` function in `workflow_engine.py` groups units into **waves**. Units with no dependencies on each other within the same wave run concurrently via `asyncio.gather`.

Example from `examples/sample_plan.json`:
```
Wave 0:  [rg, rg_postgres]    ← no dependencies, run in parallel
Wave 1:  [storage, postgres]  ← depend on wave 0, run in parallel with each other
```

Each unit in a wave gets its own `TerraformGeneratorAgent` call — those LLM calls also run concurrently.

### 2. Workflow-level concurrency (multiple tickets)

Each incoming SNOW webhook creates a new `WorkflowRun` and fires `asyncio.create_task()`. Multiple tickets execute concurrently in the same event loop.

The `_LLM_SEMAPHORE` pattern from `snow-terraform-agent` (limit concurrent LLM calls to avoid TPM quota exhaustion) should be added to `workflow_engine.py` before production use.

---

## Project structure

```
snow-tf-platform/
├── orchestrator/
│   ├── server.py              # FastAPI webhook entrypoint — no LLM
│   ├── workflow_engine.py     # State machine + DAG executor — no LLM
│   └── models.py              # WorkflowState, Plan, PlanUnit, EvaluatorResult
│
├── agents/
│   ├── azure/
│   │   ├── planner_agent.py   # MAF Agent 1: request → Plan
│   │   ├── terraform_agent.py # MAF Agent 3: PlanUnit → Terraform + PR
│   │   ├── environment_scan.py # Non-agent: Azure ARG queries
│   │   └── workflow.py        # Wires agents + orchestrator for Azure domain
│   ├── aws/
│   │   └── stub.py            # TODO: implement
│   └── snowflake/
│       └── stub.py            # TODO: implement
│
├── evaluators/
│   ├── terraform_correctness.py  # Structural checks (module blocks, brackets)
│   ├── terraform_security.py     # Secret detection (pattern matching)
│   └── terraform_compliance.py   # Required tag checks
│
├── mcp/
│   ├── github.py              # GitHub push + PR creation (stub)
│   └── servicenow.py          # SNOW work note updates (stub)
│
├── examples/
│   ├── sample_snow_ticket.json
│   └── sample_plan.json
│
├── main.py                    # Entry point — wires engine + workflows
└── requirements.txt
```

---

## Running locally

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# Required env vars
export AZURE_OPENAI_ENDPOINT="https://your-resource.openai.azure.com/"
export AZURE_OPENAI_DEPLOYMENT_NAME="gpt-4o"
export AZURE_OPENAI_API_KEY="..."
export AZURE_SUBSCRIPTION_ID="..."
export GITHUB_ORG="your-org"
export GITHUB_TERRAFORM_REPO="terraform-modules"

uvicorn main:app --host 0.0.0.0 --port 8030 --reload
```

Submit a test ticket:
```bash
curl -X POST http://localhost:8030/webhook/snow/approval \
  -H "Content-Type: application/json" \
  -d @examples/sample_snow_ticket.json
```

---

## Extending to a new cloud

1. Create `agents/<cloud>/workflow.py` with an async `run(request, run)` function
2. Create `agents/<cloud>/planner_agent.py` — same MAF pattern, cloud-specific system prompt
3. Create `agents/<cloud>/terraform_agent.py` — same MAF pattern, cloud-specific modules
4. Register in `main.py`: `RequestType.<CLOUD>_INFRA: <cloud>_workflow.run`
5. Add keyword to `_KEYWORD_MAP` in `orchestrator/server.py`

The orchestrator, DAG executor, evaluators, and models are unchanged.
