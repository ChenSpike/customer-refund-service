# Policy Agent

The Policy Agent is the policy-reasoning step in the iDox customer refund workflow. It starts after the Triage Agent has already converted a customer ticket into structured request and order facts. It does not parse raw tickets, query orders directly, or issue refunds. Its job is to evaluate refund eligibility with the policy knowledge base, run a governance review, and write the next workflow handoff back to GCP.

This implementation is cloud-backed. It requires Azure OpenAI for policy and governance reasoning and GCP MySQL `main_db` for workflow input/output. There is no mock execution path or local rule-based decision fallback.

## How It Works

```text
GCP main_db.agent_handoffs
  triage_agent -> policy_agent output_json
    -> validate and normalize PolicyAgentInput
    -> load policy knowledge base by case.policy_version
    -> Azure policy-reasoning agent drafts the decision
    -> Azure governance agent reviews and finalizes the output
    -> validate exact PolicyAgentOutput contract
    -> write policy_agent -> next_agent handoff
    -> update workflow, audit, governance, and human-approval tables
```

Routing is determined by the final decision:

```text
approve / deny / partial_refund -> response_agent
request_info                  -> triage_agent
manual_review                 -> human_approval
quarantine / block            -> human_approval
```

Token usage from Azure is stored on the Policy Agent handoff row as `input_tokens` and `output_tokens`. Workflow-level usage is the sum of token columns across all handoffs with the same `trace_id`.

## Database Contract

The source input is the Triage Agent handoff:

```text
table:      main_db.agent_handoffs
from_agent: triage_agent
to_agent:   policy_agent
input:      output_json
```

The Policy Agent writes or updates one downstream handoff per trace:

```text
table:         main_db.agent_handoffs
from_agent:    policy_agent
to_agent:      response_agent | human_approval | triage_agent
input_json:    normalized PolicyAgentInput
output_json:   final PolicyAgentOutput
input_tokens:  Azure prompt/input tokens
output_tokens: Azure generated/output tokens
```

It also writes supporting rows when applicable:

- `audit_log`: records successful evaluations and failures.
- `governance_events`: records Policy Agent governance flags such as semantic drift, PII risk, forbidden tool use, or policy conflict.
- `human_approvals`: creates or updates pending review rows when the output routes to human approval.
- `workflow_runs`: advances the workflow to the next agent or marks it as pending/paused/failed.

## Setup

From `customer-refund-service`:

```powershell
python -m pip install -r policy_agent\requirements.txt
Copy-Item policy_agent\.env.example policy_agent\.env
```

Fill in `policy_agent/.env` with Azure OpenAI and GCP MySQL settings. Environment variables take precedence over `.env` values. The GCP database name is fixed in code as `main_db`.

Required Azure settings:

```text
AZURE_OPENAI_ENDPOINT
AZURE_OPENAI_API_KEY
AZURE_OPENAI_DEPLOYMENT
AZURE_OPENAI_API_VERSION
AZURE_OPENAI_MAX_OUTPUT_TOKENS
```

Required MySQL settings:

```text
MYSQL_HOST
MYSQL_PORT
MYSQL_USER
MYSQL_PASSWORD
MYSQL_CONNECT_TIMEOUT
```

`AZURE_OPENAI_API_VERSION` must be `2025-03-01-preview` or later because the implementation uses the Responses API.

## Commands

Check configuration and required GCP schema:

```powershell
python -m policy_agent.cli check
```

Process only workflows currently waiting at `policy_agent`:

```powershell
python -m policy_agent.cli run --pending
```

Reprocess all triage-to-policy handoffs:

```powershell
python -m policy_agent.cli run --all
```

Reprocess one trace:

```powershell
python -m policy_agent.cli run --trace TRACE-POL-001
```

Reset GCP back to the triage-only baseline before a clean run:

```powershell
python -m policy_agent.cli reset --confirm main_db
```

The reset command deletes Policy Agent artifacts only. It preserves the original Triage Agent handoffs, tickets, orders, customers, and baseline workflow rows.

## Live Integration Test

```powershell
python -m pytest policy_agent\tests -q
```

This is a live integration test, not a unit test. It resets `main_db` to the triage-to-policy baseline, runs all 20 cloud cases through Azure, writes the Policy Agent outputs back to GCP, and verifies the JSON contract, routing, token usage, workflow state, audit rows, governance rows, and human approvals.

The current verification report is stored at:

```text
policy_agent/reports/Policy_Agent_GCP_20_Case_Performance_Report.docx
```

## Files

| File | Purpose |
|---|---|
| `README.md` | This guide for how the Policy Agent works, how to run it, and what each file does. |
| `__init__.py` | Exposes the main package objects: `PolicyAgentInput`, `PolicyAgentOutput`, and `PolicyAgentService`. |
| `models.py` | Defines the strict Pydantic input/output schemas, allowed decision routes, governance flags, token usage model, and input normalization helper. |
| `service.py` | Orchestrates one Policy Agent run: fetch source handoffs, validate input, load policy context, call Azure agents, and persist results. |
| `azure_agent.py` | Handles Azure OpenAI configuration, policy-agent and governance-agent calls, JSON repair attempts, response validation, and token accounting. |
| `prompts.py` | Builds the policy reasoning, governance review, and JSON repair prompts. It also defines the exact JSON shapes Azure must return. |
| `cloud_db.py` | Handles all GCP MySQL reads/writes, schema checks, reset behavior, artifact ID generation, workflow updates, audit rows, governance rows, and human approvals. |
| `cli.py` | Provides command-line entry points: `check`, `run --pending`, `run --all`, `run --trace`, and `reset`. |
| `requirements.txt` | Lists runtime dependencies: MySQL connector, OpenAI SDK, Pydantic, and pytest. |
| `.env.example` | Template for Azure OpenAI and GCP MySQL configuration. |
| `.env` | Local ignored environment file containing real credentials and runtime settings. Do not commit real values. |
| `.gitignore` | Ignores generated Policy Agent report artifacts. |
| `data/policy_context_v1.md` | Human-readable refund policy knowledge base selected by `case.policy_version = v1.0`. Azure receives this text as policy context; local code does not execute these rules directly. |
| `tests/__init__.py` | Marks the test directory as a Python package. |
| `tests/test_cloud_pipeline_20_cases.py` | Live 20-case GCP/Azure integration test with expected decisions, route checks, token checks, workflow checks, and artifact ID checks. |
| `reports/Policy_Agent_GCP_20_Case_Performance_Report.docx` | Generated verification report from the clean 20-case benchmark run. |

## Important Boundaries

- The Policy Agent consumes post-triage structured state only.
- It uses Azure OpenAI and the policy knowledge base for decisions.
- It must not call `Order_Database`, `Refund_Issuer`, or any external policy source.
- It never claims that a refund has been executed.
- It writes workflow decisions and governance metadata, but refund execution remains downstream.
