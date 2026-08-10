# Refund Integration

An automated, state-driven multi-agent workflow for evaluating and processing e-commerce customer refund requests.

---

## System Workflow Pipeline

```text
                                    [ User Input ]
                                          |
                                          v
                                   [ Triage Agent ]
                                          |
                                          v
                                [ Triage Governance ]
                                          |
                                          v
                                  [ Triage Router ]
                                          |
               +--------------------------+--------------------------+
               | (governance block)       | (data missing)           | (allow & complete)
               v                          |                          v
     [ Human Approval ]                   |                   [ Policy Agent ]
               |                          |                          |
               |                          |                          v
               |                          |                [ Policy Governance ]
               |                          |                          |
               |                          |                          v
               |                          |                  [ Policy Router ]
               |                          |                          |
               |                          |        +-----------------+-----------------+
               |                          |        | (approve)       | (deny / info)   | (block / review)
               |                          |        v                 |                 |
               |                          |  [ Refund Agent ]        |                 |
               |                          |        v                 |                 |
               |                          |  [ Refund Router ]       |                 |
               |                          |        |                 |                 |
               |                          v        v                 |                 v
               +----------------------> [ Response Agent ] <---------+---------[ Human Approval ]
                                          |                                            
                                          v                                            
                                       [ END ] 
```

## Core State Table

Keep only the core workflow state.

| State Field | Type | Description | Main Stages |
|---|---|---|---|
| `user_id` | `str` | User who submitted the request | Input, Governance |
| `message` | `str` | Current user message | Triage |
| `conversation_history` | `list` | Prior conversation context | Triage |
| `request_context` | `dict` | External metadata such as channel, locale, or source | Input |
| `trace_id` | `str` | End-to-end workflow trace ID | All stages |
| `ticket_id` | `str` | Case or ticket identifier | All stages |
| `current_stage` | `str` | Current workflow node | All stages |
| `workflow_status` | `str` | Overall workflow status such as `running`, `waiting_user`, `waiting_human`, or `completed` | All stages |
| `missing_fields` | `list[str]` | Required fields that are still missing | Triage, Response |
| `user_action_required` | `bool` | Whether the workflow is waiting for user input | Triage Router, Response |
| `human_review_required` | `bool` | Whether the case must be reviewed by a human | Governance, Policy Router |
| `final_outcome` | `str` | Final case result such as `approved`, `denied`, `need_info`, `refund_failed`, or `manual_review` | End stages |
| `requested_order_id` | `str` | Order ID extracted from the user message | Triage |
| `clarification_question` | `str` | Follow-up question sent back to the user | Response |
| `order_lookup_result` | `dict` | Raw order data returned by the lookup tool | Triage, Audit |
| `triage_output` | `dict` | Structured case payload produced by triage | Triage Governance, Policy |
| `triage_governance_result` | `dict` | Triage governance decision with allow or block result | Triage Router, Human Approval |
| `policy_governance_result` | `dict` | Policy governance decision with allow or block result | Policy Router, Human Approval |
| `risk_flags` | `dict` | Consolidated risk signals such as PII, content filter, injection, or tool misuse | Governance |
| `policy_decision` | `dict` | Final policy decision for the refund case | Policy Governance, Routers, Downstream |
| `policy_context` | `dict` | Supporting policy metadata such as rule version or retrieval context | Policy, Audit |
| `refund_result` | `dict` | Output from the refund execution branch | Refund Agent |
| `response_result` | `dict` | Output from the user response branch | Response Agent |
| `human_review` | `dict` | Output from the human approval branch | Human Approval |
| `errors` | `Annotated[list[dict], operator.add]` | Append-only list of workflow errors or exceptions | All stages |
| `audit_trail` | `Annotated[list[dict], operator.add]` | Append-only list of audit records written across important steps | Governance, Persistence |
| `snapshots` | `Annotated[list[dict], operator.add]` | Append-only list of state snapshots for tracing and replay | Middleware, Observability |
| `llm_input_tokens` | `Annotated[int, operator.add]` | Additive total of input tokens reported by each LLM node | Triage, Policy |
| `llm_output_tokens` | `Annotated[int, operator.add]` | Additive total of output tokens reported by each LLM node | Triage, Policy |

Removed from the core state table:

- `case` -> use `request_context`
- `awaiting_info` -> use `missing_fields` and `user_action_required`
- `awaiting_order_id` -> use `missing_fields` and `user_action_required`
- `next_agent` -> routing belongs to routers
- `buggy_db` -> test-only, not part of core workflow state
- `content_filter_blocked` -> use `risk_flags`
- `injection_flag` -> use `risk_flags`

## Stage Flow Using The New State Table

### 1. User Input

This stage prepares the minimum context for the workflow.

- Input fields: `user_id`, `message`, `conversation_history`, `request_context`
- System fields created or continued here: `trace_id`, `ticket_id`
- Initial control state:
        - `current_stage = "triage"`
        - `workflow_status = "running"`
        - `missing_fields = []`
        - `user_action_required = False`
        - `human_review_required = False`
        - `final_outcome = ""`

### 2. Triage Agent

Triage converts the raw user message into structured case data.

Reads:

- `user_id`
- `message`
- `conversation_history`
- `request_context`
- `trace_id`
- `ticket_id`

Writes:

- `current_stage = "triage"`
- `requested_order_id`
- `order_lookup_result`
- `triage_output`
- `clarification_question`
- `missing_fields`
- `user_action_required`
- `workflow_status`
- `conversation_history`
- `llm_input_tokens`
- `llm_output_tokens`
- `errors` if extraction or lookup fails unexpectedly

Typical results:

- If order data is complete:
        - `triage_output` is ready
        - `missing_fields = []`
        - `user_action_required = False`
        - `workflow_status = "running"`
- If data is missing:
        - `missing_fields = ["order_id"]`
        - `user_action_required = True`
        - `clarification_question` contains the follow-up question
        - `workflow_status = "waiting_user"`

### 3. Triage Governance

This stage checks whether the triage result is safe before policy evaluation.

Reads:

- `triage_output`
- `trace_id`
- `ticket_id`
- `user_id`

Writes:

- `current_stage = "triage_governance"`
- `triage_governance_result`
- `risk_flags`
- `audit_trail`
- `snapshots`
- `human_review_required` when blocked
- `workflow_status` when blocked

Typical results:

- If safe:
        - `triage_governance_result.status = "allow"`
- If blocked:
        - `triage_governance_result.status = "block"`
        - `human_review_required = True`
        - `workflow_status = "waiting_human"`

### 4. Triage Router

This stage does not create business data. It only reads control state and selects the next node.

Reads:

- `triage_governance_result`
- `missing_fields`
- `user_action_required`
- `triage_output`

Routing rules:

- If `triage_governance_result.status == "block"` -> `Human Approval`
- If `user_action_required == True` -> `Response Agent`
- If `triage_output` is complete -> `Policy Agent`

### 5. Policy Agent

Policy evaluates the structured case and decides the refund outcome.

Reads:

- `triage_output`
- `trace_id`
- `ticket_id`

Writes:

- `current_stage = "policy"`
- `policy_decision`
- `policy_context`
- `llm_input_tokens`
- `llm_output_tokens`
- `errors` if policy generation fails

Typical results:

- `policy_decision.decision` becomes one of:
        - `approve`
        - `deny`
        - `request_info`
        - `manual_review`

### 6. Policy Governance

This stage checks whether the policy result is safe and valid.

Reads:

- `policy_decision`
- `policy_context`
- `trace_id`
- `ticket_id`
- `user_id`

Writes:

- `current_stage = "policy_governance"`
- `policy_governance_result`
- `risk_flags`
- `audit_trail`
- `snapshots`
- `human_review_required` when blocked
- `workflow_status` when blocked

Typical results:

- If safe:
        - `policy_governance_result.status = "allow"`
- If blocked:
        - `policy_governance_result.status = "block"`
        - `human_review_required = True`
        - `workflow_status = "waiting_human"`

### 7. Policy Router

This stage reads the policy result and sends the workflow to the correct final branch.

Reads:

- `policy_governance_result`
- `policy_decision`

Routing rules:

- If `policy_governance_result.status == "block"` -> `Human Approval`
- If `policy_decision.decision == "approve"` -> `Refund Agent`
- If `policy_decision.decision in {"deny", "request_info"}` -> `Response Agent`
- If `policy_decision.decision == "manual_review"` -> `Human Approval`

### 8. Refund Agent

This is the execution branch for approved refund outcomes.

Reads:

- `policy_decision`
- `trace_id`
- `ticket_id`
- `requested_order_id`
- `order_lookup_result`

Writes:

- `current_stage = "refund_agent"`
- `refund_result`
- `final_outcome`
- `workflow_status = "running"`

Typical results:

- If fully approved: `final_outcome = "approved"`
- If refund execution fails: `final_outcome = "refund_failed"`

### 9. Refund Router

This stage reads the refund execution result and sends the workflow to the response branch.

Reads:

- `refund_result`

Routing rules:

- If `refund_result.status == "success"` -> `Response Agent`
- If `refund_result.status == "failed"` -> `Response Agent`

### 10. Response Agent

This is the user-facing response branch. It is used for missing data and final business responses.

Reads:

- `clarification_question`
- `missing_fields`
- `user_action_required`
- `policy_decision`
- `refund_result`

Writes:

- `current_stage = "response_agent"`
- `response_result`
- `final_outcome`
- `workflow_status`

Typical results:

- If waiting for user data:
        - `response_result` asks for the missing information
        - `final_outcome = "need_info"`
        - `workflow_status = "waiting_user"`
- If refund succeeded:
        - `response_result` confirms the refund was processed
        - `final_outcome = "approved"`
        - `workflow_status = "completed"`
- If refund failed:
        - `response_result` explains the refund could not be completed
        - `final_outcome = "refund_failed"`
        - `workflow_status = "completed"`
- If policy denied:
        - `response_result` explains the denial
        - `final_outcome = "denied"`
        - `workflow_status = "completed"`
- If policy requested more info:
        - `response_result` asks for the required information
        - `final_outcome = "need_info"`
        - `workflow_status = "waiting_user"`

### 11. Human Approval

This is the manual review branch for blocked or ambiguous cases.

Reads:

- `policy_governance_result`
- `policy_decision`
- `trace_id`
- `ticket_id`

Writes:

- `current_stage = "human_approval"`
- `human_review`
- `human_review_required = True`
- `final_outcome = "manual_review"`
- `workflow_status = "waiting_human"`

Typical result:

- The workflow stops and waits for a human reviewer to take over.

## Core Development Rules

1. **Agents**: Return business data patches only. Never include routing fields (`next_agent`) or database calls in agent nodes.
2. **Routers**: Read shared state and return the string key of the next target node. No business logic transformation inside routers.
3. **Governance**: Operates as standalone nodes (Triage Governance, Policy Governance). Runs security and compliance checks, logs audit events, and outputs `status: allow/block`.
4. **Middlewares**: Capture execution traces, token metrics, and state snapshots asynchronously.
5. **Tools**: Universal SDK wrappers for external APIs (Azure, DBs, RAG). Agents and governance modules must access external services via `tools/`.

## Project Directory Structure

```text
C:.
├─ agents/                # LLM reasoning nodes (State patch outputs only)
│  ├─ triage/             # Intent recognition & order facts extraction
│  ├─ policy/             # Refund rule evaluation & business decision
│  ├─ refund/             # Execution layer for payment/refund processing
│  └─ response/           # Uniform outbound text generation
├─ app/                   # Graph orchestration & runtime control
│  ├─ state.py            # Shared State Schema (Context, Payload, Control)
│  ├─ graph.py            # StateGraph definition & node wiring
│  ├─ routers/            # Conditional edges (triage_router, policy_router)
│  └─ middlewares/        # Async tracing & observability handlers
├─ governance/            # Security & compliance checks
│  ├─ node.py             # Entrypoint executing configured checkers
│  ├─ checkers.py         # Strategy implementations (Injection, PII, Rules)
│  └─ audit_logger.py     # Structured envelope audit log writer
├─ db/                    # Persistence layer
│  ├─ database.py         # DB connection engine & session management
│  ├─ pipeline_store.py   # State checkpointing & human approval persistence
│  └─ migrations/         # SQL schema migration scripts
├─ tools/                 # External service clients & utility functions
│  ├─ azure_client.py     # Azure OpenAI SDK wrapper
│  ├─ llm_helpers.py      # Structured output parser & retry wrappers
│  ├─ order_lookup.py     # Internal OMS/CRM data retrieval
│  └─ policy_retriever.py # RAG vector store interface
└─ tests/                 # Unit, integration, and live tests
```

## Quick Start

1. Configure environment variables in `.env`.
2. Run unit and integration test suites:

```bash
pytest tests/unit/
pytest tests/integration/
```