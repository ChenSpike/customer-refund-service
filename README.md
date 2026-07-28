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
               |                          |        |                 |                 |
               |                          v        v                 |                 v
               +----------------------> [ Response Agent ] <---------+---------[ Human Approval ]
                                          |                                            
                                          v                                            
                                       [ END ] 
```

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