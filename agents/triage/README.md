# Triage slice (state-driven framework)

The triage stage of the refund pipeline: classify the customer's request, look up
the order, and hand a structured `triage_output` to Policy — with the ASI07
governance check guarding against order-lookup data leakage.

## Flow

```
triage node ──▶ triage_governance (ASI07) ──route_after_triage──▶ policy
                     │                              │
                     │ block                        ├─ user_action_required ─▶ response_agent (need info)
                     ▼                              └─ (block) ──────────────▶ human_approval
              human_approval
```

## Files

| File | Role |
|------|------|
| `agents/triage/node.py` | Pure node: LLM classification + order lookup → `AppState` delta. On a missing/unknown order it sets `user_action_required` + `missing_fields` (declared fields the router and response node key off). No DB writes. |
| `agents/triage/governance_node.py` | **`GovernanceNode(BaseGovernanceNode)`** — deterministic triage governance (PII / semantic-drift / **ASI07** schema+ownership). Returns `triage_governance_result` and, when an event writer is injected by the parent graph, persists a mapped `GovernanceStatement`. |
| `agents/triage/prompts.py`, `helpers.py` | System prompt, valid reasons, `parse_requested_amount`, `light_clean`. |
| `app/routers/triage_router.py` | `route_after_triage`: block → `human_approval`, `user_action_required` → `response_agent`, else → `policy`. |
| `tools/order_lookup.py` | `Order_Database_Lookup` tool schema + thin wrapper over `db.orders`. |
| `db/orders.py` | Read path for `orders`/`customers` in main_db (normal + `buggy` `!=` JOIN). Independent of `db.database` on purpose (see below). |

## Governance style — deterministic, not LLM

Policy governance is an Azure OWASP LLM scan (`GovernanceAssessment`:
semantic_drift / forbidden_tool / pii_risk). Triage governance is **deterministic
by design** — the point is to catch an ownership/schema leak in the raw order
lookup before it reaches Policy. It implements the shared `BaseGovernanceNode`
contract (`__call__(state) -> dict`) but returns a plain verdict dict, not a
`GovernanceAssessment` — ASI07 ownership/schema is not one of the LLM OWASP flags.

**ASI07 checks (pure, on `order_lookup_result`):**
- **Schema** — all contract fields present and correctly typed; valid `item_status`.
- **Ownership** — `contact_customer_id` must equal the requesting `user_id`. A buggy
  JOIN returns a different customer's contact for a valid order; that is the leak.

## Two things to keep in mind

1. **Persistence mapping.** Triage governance now uses the same injected event-writer
   contract as Policy. Because ASI07 ownership/schema is not one of the LLM OWASP
   flags, the node maps blocked checks into the shared `GovernanceStatement`
   payload before the DB adapter writes `governance_events`.
2. **`app.graph` is currently un-importable on refactor HEAD** — a pre-existing
   circular import (`db.database → agents.policy → db.pipeline_store → agents.policy`).
   The triage modules here import cleanly in isolation; full-graph wiring can't be
   exercised until that cycle is fixed by the policy/db owner. `db/orders.py` is
   deliberately independent of `db.database` to avoid dragging that chain in.

## Tests (offline, deterministic — no LLM, no DB)

```bash
pytest tests/test_order_lookup.py tests/test_triage_governance.py tests/test_triage_node.py
```

The order lookup is mocked (`db.orders` connection patched); the node runs against
a fake Responses client. 27 tests.
