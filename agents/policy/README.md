# Policy Agent

The Policy Agent converts a Triage handoff into a governed refund decision. Azure OpenAI is mandatory for policy reasoning and Policy Governance. Python validates the result, maps the handoff, and persists it; it does not replace Azure with a local policy decision.

## Architecture

The parent workflow is:

```text
GCP or parent Triage state
    -> policy_agent subgraph
       -> policy
       -> policy_governance
       -> policy_handoff
    -> policy_persistence
    -> parent handoff mapper
    -> refund_agent | response_agent | human_approval
```

The Policy subgraph accepts only `trace_id`, `ticket_id`, and `triage_output`. This narrow input boundary prevents existing parent token events and risk flags from being returned and counted twice. Every Policy patch is JSON serializable.

`policy_persistence` is outside the subgraph and runs before downstream routing. A persistence error stops the graph, so no downstream agent can act on an unrecorded decision.

## Responsibilities

| Component | Responsibility |
|---|---|
| `policy_node.py` | Normalize the Proposal input, load the policy KB and precedent memory, call Azure, validate the result, and expose JSON state projections. |
| `governance_node.py` | Call Azure for OWASP-only review and merge the deterministic forbidden-tool check. It cannot alter the policy decision or confidence. |
| `graph.py` | Compile `policy -> policy_governance -> policy_handoff`. |
| `routing.py` | Convert a decision and Governance status into the semantic handoff `refund`, `response`, or `human_review`. |
| `db/pipeline_store.py` | Reconstruct the validated result and own the single Policy persistence call. |
| `db/database.py` | Execute the one-transaction GCP write. |
| `service.py` | Retain a standalone GCP worker that invokes the same subgraph and store. |

Refund execution, customer response generation, and human resolution are downstream responsibilities.

## Input Contract

The parent passes the Triage JSON in `AppState.triage_output`. Standalone mode reads the same payload from `main_db.agent_handoffs.output_json` where `from_agent = 'triage_agent'` and `to_agent = 'policy_agent'`.

`exact_policy_input()` removes Triage-only fields such as `case.goal` and constructs the Proposal input in this order:

```json
{
  "case": {
    "trace_id": "...",
    "ticket_id": "...",
    "policy_version": "v1.0"
  },
  "customer_request": {
    "sanitized_text": "...",
    "refund_reason": "damaged",
    "requested_amount": 100.0,
    "currency": "USD"
  },
  "order_facts": {
    "order_id": "...",
    "product_type": "electronics",
    "purchase_date": "2026-07-01",
    "item_status": "delivered",
    "amount_paid": 100.0,
    "prior_refund_total": 0.0
  }
}
```

The root `trace_id` and `ticket_id` must equal the normalized case IDs.

## Policy Reasoning

`data/policy_context_v1.md` is the authoritative, human-edited refund-policy knowledge base. It defines decision rules, precedence, review rules, and output requirements.

`data/precedent_memory_v1.yaml` is read-only advisory memory. It may contain only finalized, non-PII, human-reviewed cases with the same policy version. The loader rejects unknown fields, identifiers, email addresses, duplicate precedent IDs, unsupported decisions, and unknown policy rules.

Missing, empty, malformed, unreadable, or version-incompatible precedent memory is nonfatal. Azure receives an explicit unavailable status and reasons from the policy KB alone. Precedents never create a rule or override the current KB. The checked-in memory is currently empty.

Azure returns one `PolicyReasoningResult` containing:

- preserved case and customer request;
- matched policies, gaps, and conflicts;
- evidence manifest;
- final decision and refund amount;
- discrete confidence and supporting evidence;
- precedent evidence;
- response guidance.

Decision precedence is encoded in the Policy prompt and verified in Python:

1. A policy conflict or matched `R-REVIEW-*` rule requires `manual_review`.
2. Otherwise, an essential missing fact requires `request_info`.
3. Otherwise, no decisive supporting rule requires `manual_review`.
4. Otherwise, low confidence requires `manual_review`.
5. Otherwise, the result may be `approve`, `deny`, or `partial_refund`.

### Confidence

Confidence measures support for the selected policy decision, including a confidently required manual review.

| Score | Level | Meaning |
|---:|---|---|
| `3` | `high` | Complete essential facts, clear policy support, no important conflict, and no relevant precedent disagreement. Unavailable precedent memory is neutral. |
| `2` | `moderate` | Policy supports the decision with a minor interpretive ambiguity or mixed relevant precedents. |
| `1` | `low` | Important uncertainty, policy conflict, weak support, or strong precedent disagreement; requires `manual_review`. |
| `0` | `insufficient` | Essential facts or decisive policy support are missing; requires `request_info` or `manual_review`. |

Actionable approval, denial, and partial-refund decisions require confidence `2` or `3`. Python verifies the confidence level, evidence, fact presence, policy effects, precedent references, decision consistency, and refund bounds.

## Policy Governance

Policy Governance is separate from refund-policy review. It evaluates only:

- `semantic_drift`: prompt injection or policy-bypass instructions;
- `forbidden_tool`: claims that the Policy Agent executed a tool, accessed a database, or issued a refund;
- `pii_risk`: third-party PII or leaked internal or precedent-specific identifiers.

Missing policy evidence, policy conflicts, refund bounds, and invalid handoffs are Policy validation failures, not OWASP findings. The deterministic checker covers genuine forbidden-tool claims. When it duplicates an Azure finding, the deterministic finding wins; findings are deduplicated and ordered consistently.

Governance returns only its assessment, token event, and risk flags. It cannot emit or modify decision or confidence fields. Any finding changes the route to human approval while preserving the underlying business decision.

## State Contract

The Policy node writes:

- `policy_result`: complete `PolicyReasoningResult` as JSON;
- `policy_decision`: downstream decision projection;
- `policy_context`: policy evaluation, response guidance, evidence manifest, and nested `precedent_context`;
- one `policy_reasoning` token event.

Policy Governance writes:

- `policy_governance_result`;
- append-only `risk_flags`;
- one `policy_governance` token event.

The handoff node writes `policy_handoff`. Before writing, persistence verifies that this handoff matches the validated Proposal output. The persistence node then writes `policy_persistence_result` with the handoff ID, downstream agent, and event counts. Parent routing uses that persisted downstream agent and rejects disagreement with `policy_handoff`.

One reconstruction function rebuilds and revalidates the typed policy result from JSON before Governance, output assembly, and persistence. It rejects disagreement between the complete `policy_result` and its projections. Precedent context is accepted only from `policy_context.precedent_context`; there is no top-level fallback.

## Output And Routing

The persisted Proposal output remains ordered as:

`case`, `customer_request`, `policy_evaluation`, `decision`, `response_guidance`, `handoff`, `governance`.

| Governance | Decision | Semantic handoff | Downstream agent |
|---|---|---|---|
| `block` | any | `human_review` | `human_approval` |
| `allow` | `approve`, `partial_refund` | `refund` | `refund_agent` |
| `allow` | `deny`, `request_info` | `response` | `response_agent` |
| `allow` | `manual_review` | `human_review` | `human_approval` |

## GCP Persistence

`PipelineStore` calls `GCPRepository.persist_result()` exactly once after the Policy subgraph succeeds. One MySQL transaction:

- upserts one Policy handoff;
- writes a policy-review event when the business decision is `manual_review`;
- writes only real Policy OWASP findings to `governance_events`;
- upserts one human approval when routed to `human_approval`;
- appends the successful Policy audit payload and evidence manifest;
- updates `workflow_runs`;
- stores aggregate Policy reasoning, Governance, and repair tokens on the Policy handoff.

A human approval has exactly one typed trigger: `policy_review` or `governance`. Its `triggering_event_id` points to the corresponding event. Approval routing preserves the business decision: approved manual reviews continue to `refund_agent`, while a rejected review continues to `response_agent`.

## Standalone Worker

`PolicyAgentService` supports Policy-only execution before the full parent workflow is deployed. It reads a Triage handoff, builds the same AppState input, invokes the same three-node subgraph, reconstructs the same output, and uses the same `PipelineStore`. It does not contain a second Policy implementation.

```powershell
python -m agents.policy.cli run --pending
python -m agents.policy.cli run --trace TRACE-POL-001
python -m agents.policy.cli run --all
```

## Configuration

Install from the repository root:

```powershell
python -m pip install -r requirements.txt
```

Create an ignored root `.env` from `.env.example`, or set the variables in the environment:

```text
AZURE_OPENAI_ENDPOINT
AZURE_OPENAI_API_KEY
AZURE_OPENAI_DEPLOYMENT
AZURE_OPENAI_API_VERSION=2025-03-01-preview
AZURE_OPENAI_MAX_OUTPUT_TOKENS=2400
AZURE_OPENAI_TEMPERATURE=0
MYSQL_HOST
MYSQL_PORT=3306
MYSQL_USER
MYSQL_PASSWORD
MYSQL_CONNECT_TIMEOUT=10
```

The Responses API requires Azure API version `2025-03-01-preview` or later. Credentials are never committed.

Verify configuration and the existing cloud schema without changing data:

```powershell
python -m agents.policy.cli check
```

## Testing

Safe tests use fake Azure and repository dependencies:

```powershell
python -m pytest -q -m "not live"
python -m compileall -q .
git diff --check
```

The live benchmark is destructive only within `TRACE-POL-001` through `TRACE-POL-020`. It snapshots unrelated rows, removes only those traces' previous Policy artifacts, preserves their Triage source handoffs and workflow source data, runs both Azure nodes for every case, and verifies database integrity.

Run it only with explicit authorization:

```powershell
$env:RUN_POLICY_AGENT_LIVE_TESTS = "1"
python -m pytest agents\policy\tests\test_cloud_pipeline_20_cases.py -q -m live
Remove-Item Env:RUN_POLICY_AGENT_LIVE_TESTS
```

Expected benchmark distributions are 5 approvals, 5 denials, 8 manual reviews, and 2 information requests; routes are 4 refund, 6 response, and 10 human approval.

## Current Limitations

- Precedent memory is a versioned YAML file and is currently empty; another process must populate it from finalized human reviews.
- No vector retrieval or Qdrant adapter is implemented.
- The benchmark evaluates Policy outputs and persistence, not downstream refund execution, customer outcomes, or completed human decisions.
- Token counts are stored; cost is not stored.
- Competing writers and concurrent benchmark resets are outside the current scope.

The latest benchmark report is `reports/Policy_Agent_GCP_20_Case_Performance_Report.docx`.
