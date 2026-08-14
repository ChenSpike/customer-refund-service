# Agent Integration Guide

This guide shows how each agent integrates with the database and API for logging actions and governance checks.

## Quick Start

All communication happens via HTTP to the Flask API running on `http://localhost:5000` (or your deployment URL).

### Common Pattern

Every agent follows this pattern:

```python
import requests
import json

API_URL = "http://localhost:5000"

# 1. Start workflow
response = requests.post(f"{API_URL}/api/workflows/start", json={
    "ticket_id": "ticket_123",
    "policy_version": "1.0"
})
trace_id = response.json()["trace_id"]

# 2. Do agent work
# ...

# 3. Log handoff to next agent
requests.post(f"{API_URL}/api/handoffs", json={
    "trace_id": trace_id,
    "from_agent": "triage_agent",
    "to_agent": "policy_agent",
    "input_json": {...},  # What triage received
    "output_json": {...}  # What triage produced
})

# 4. Update workflow status
requests.put(f"{API_URL}/api/workflows/{trace_id}/status", json={
    "status": "running",
    "current_agent": "policy_agent"
})
```

---

## Agent-Specific Integration

### **Customer Ticket Node** (Input)

**Role:** Sanitize raw customer input and create initial workflow state.

```python
def handle_customer_ticket(raw_ticket_text: str, customer_id: str, ticket_id: str):
    # 1. Start workflow
    response = requests.post(f"{API_URL}/api/workflows/start", json={
        "ticket_id": ticket_id,
        "policy_version": "1.0"
    })
    trace_id = response.json()["trace_id"]
    
    # 2. Sanitize and parse
    sanitized_text = sanitize_prompt(raw_ticket_text)
    
    # 3. Log audit event (optional, but recommended)
    requests.post(f"{API_URL}/api/audit-log", json={
        "trace_id": trace_id,
        "event_type": "ticket_submitted",
        "agent": "customer_ticket_node",
        "payload": {
            "customer_id": customer_id,
            "raw_text_length": len(raw_ticket_text),
            "injection_flag": detect_prompt_injection(raw_ticket_text)
        }
    })
    
    return {
        "trace_id": trace_id,
        "customer_id": customer_id,
        "sanitized_text": sanitized_text
    }
```

---

### **Triage Agent**

**Role:** Classify issue, retrieve order, validate schema.

**Enforcement:** Strict DB schema validation.

```python
def triage_agent(state: dict):
    trace_id = state["trace_id"]
    
    # 1. Receive input
    input_data = {
        "customer_id": state["customer_id"],
        "ticket_text": state["sanitized_text"]
    }
    
    # 2. Do work: classify, lookup order
    order = lookup_order(state["customer_id"])
    classification = classify_issue(state["sanitized_text"])
    
    # 3. Prepare output
    output_data = {
        "customer_id": state["customer_id"],
        "order": order,
        "classification": classification
    }
    
    # 4. Log handoff to Policy Agent
    requests.post(f"{API_URL}/api/handoffs", json={
        "trace_id": trace_id,
        "from_agent": "triage_agent",
        "to_agent": "policy_agent",
        "input_json": input_data,
        "output_json": output_data
    })
    
    # 5. Update workflow status
    requests.put(f"{API_URL}/api/workflows/{trace_id}/status", json={
        "status": "running",
        "current_agent": "policy_agent"
    })
    
    return output_data
```

**Governance Check (handled by interceptor, but log the event):**

```python
def check_governance_after_triage(trace_id: str, output_data: dict):
    # If ASI07 (data leakage) is detected, the governance interceptor will block
    # and request human approval. This is logged automatically by the interceptor.
    # Your job is just to return clean output.
    pass
```

---

### **Policy Agent**

**Role:** Evaluate refund policy, recommend action, detect goal drift.

**Enforcement:** Semantic drift detection (embedding similarity > 0.65).

```python
def policy_agent(state: dict):
    trace_id = state["trace_id"]
    
    # 1. Receive input from Triage
    input_data = {
        "customer_id": state["customer_id"],
        "order": state["order"],
        "classification": state["classification"]
    }
    
    # 2. Evaluate policy
    policy_recommendation = evaluate_refund_policy(state["order"], state["classification"])
    confidence = policy_recommendation.get("confidence", 0.5)
    
    # 3. Prepare output
    output_data = {
        "policy_decision": policy_recommendation["decision"],
        "confidence": confidence,
        "reasoning": policy_recommendation["reasoning"]
    }
    
    # 4. Log handoff
    requests.post(f"{API_URL}/api/handoffs", json={
        "trace_id": trace_id,
        "from_agent": "policy_agent",
        "to_agent": "response_agent",
        "input_json": input_data,
        "output_json": output_data
    })
    
    # 5. Update status
    requests.put(f"{API_URL}/api/workflows/{trace_id}/status", json={
        "status": "running",
        "current_agent": "response_agent"
    })
    
    return output_data
```

**If governance blocks (goal drift detected):**

The governance interceptor will:
1. Log the governance event
2. Create a human approval request (pause & wait)
3. Set workflow status to `paused_governance`

Your agent doesn't need to handle this—the interceptor does it.

---

### **Response Agent**

**Role:** Draft customer response (text generation only).

**Enforcement:** Hard block on any tool_calls payload.

```python
def response_agent(state: dict):
    trace_id = state["trace_id"]
    
    # 1. Receive input
    input_data = {
        "policy_decision": state["policy_decision"],
        "confidence": state["confidence"],
        "reasoning": state["reasoning"]
    }
    
    # 2. Generate response (no tool calls!)
    draft_response = generate_customer_email(state["policy_decision"], state["reasoning"])
    
    # 3. Prepare output
    output_data = {
        "draft_response": draft_response,
        "ready_to_send": True  # No human approval needed at this stage
    }
    
    # 4. Log handoff
    requests.post(f"{API_URL}/api/handoffs", json={
        "trace_id": trace_id,
        "from_agent": "response_agent",
        "to_agent": "refund_agent",
        "input_json": input_data,
        "output_json": output_data
    })
    
    # 5. Update status
    requests.put(f"{API_URL}/api/workflows/{trace_id}/status", json={
        "status": "running",
        "current_agent": "refund_agent"
    })
    
    return output_data
```

---

### **Refund Agent** (Financial Action)

**Role:** Execute refund (MANDATORY human approval).

**Enforcement:** Token-gated execution + pause & wait.

```python
def refund_agent(state: dict):
    trace_id = state["trace_id"]
    
    # 1. Receive input
    input_data = {
        "policy_decision": state["policy_decision"],
        "amount_to_refund": state["order"]["amount_paid"],  # or calculated
        "customer_id": state["customer_id"]
    }
    
    # 2. Request human approval (MANDATORY for financial actions)
    approval_response = requests.post(f"{API_URL}/api/approvals", json={
        "trace_id": trace_id,
        "reason": "ASI08: Excessive autonomy - Refund requires human confirmation",
        "amount_requested": input_data["amount_to_refund"]
    })
    approval_id = approval_response.json()["approval_id"]
    
    # 3. Pause workflow (wait for human decision)
    requests.put(f"{API_URL}/api/workflows/{trace_id}/status", json={
        "status": "pending_human",
        "current_agent": "refund_agent"
    })
    
    # 4. Poll for approval (or webhook callback)
    # For demo: implement a simple polling loop
    approved = wait_for_approval(approval_id, timeout=300)  # 5 min timeout
    
    if not approved:
        # Log rejection
        requests.post(f"{API_URL}/api/audit-log", json={
            "trace_id": trace_id,
            "event_type": "refund_rejected",
            "agent": "refund_agent",
            "payload": {"approval_id": approval_id}
        })
        return {"status": "rejected", "refund_executed": False}
    
    # 5. Create refund transaction record
    txn_response = requests.post(f"{API_URL}/api/refund-transactions", json={
        "trace_id": trace_id,
        "amount": input_data["amount_to_refund"],
        "currency": "USD",
        "approval_id": approval_id
    })
    transaction_id = txn_response.json()["transaction_id"]
    
    # 6. Execute refund (call payment processor)
    try:
        external_ref = execute_payment(
            customer_id=input_data["customer_id"],
            amount=input_data["amount_to_refund"]
        )
        # Update transaction status
        requests.put(f"{API_URL}/api/refund-transactions/{transaction_id}", json={
            "status": "issued",
            "external_ref": external_ref
        })
        refund_status = "issued"
    except PaymentError as e:
        requests.put(f"{API_URL}/api/refund-transactions/{transaction_id}", json={
            "status": "failed",
            "external_ref": str(e)
        })
        refund_status = "failed"
    
    # 7. Log final outcome
    requests.post(f"{API_URL}/api/audit-log", json={
        "trace_id": trace_id,
        "event_type": "refund_executed",
        "agent": "refund_agent",
        "payload": {
            "transaction_id": transaction_id,
            "status": refund_status,
            "approval_id": approval_id
        }
    })
    
    # 8. Mark workflow complete
    requests.put(f"{API_URL}/api/workflows/{trace_id}/status", json={
        "status": "completed",
        "current_agent": None
    })
    
    return {
        "status": "completed",
        "refund_executed": True,
        "transaction_id": transaction_id,
        "refund_status": refund_status
    }
```

---

## Governance Interceptor Integration

The **Governance Interceptor** runs *after* each agent and intercepts state handoffs.

### How It Works (for reference)

```python
def governance_interceptor(trace_id: str, from_agent: str, output_state: dict):
    """
    Called after each agent produces output, before passing to next agent.
    If violations detected, may:
    - Block (return error)
    - Quarantine (create human approval request)
    - Allow (pass through)
    """
    
    # Check all applicable OWASP rules for this agent
    violations = []
    
    # Example: ASI07 (Data Leakage) check after Triage
    if from_agent == "triage_agent":
        if has_cross_customer_pii(output_state):
            violation = {
                "owasp_category": "ASI07",
                "severity": "high",
                "description": "Cross-customer PII detected"
            }
            violations.append(violation)
    
    # Example: ASI01 (Goal Drift) check after Policy Agent
    if from_agent == "policy_agent":
        drift_score = compute_embedding_similarity(
            output_state["reasoning"],
            "Evaluate if customer qualifies for refund"
        )
        if drift_score > 0.65:
            violation = {
                "owasp_category": "ASI01",
                "severity": "medium",
                "trigger_score": drift_score
            }
            violations.append(violation)
    
    # Log and handle violations
    for violation in violations:
        requests.post(f"{API_URL}/api/governance-events", json={
            "trace_id": trace_id,
            "agent": from_agent,
            "owasp_category": violation["owasp_category"],
            "trigger_score": violation.get("trigger_score"),
            "interceptor_action": "quarantine",  # or "block" or "allow"
            "flags": violation
        })
        
        if violation["severity"] == "high":
            # Create human approval and block
            requests.post(f"{API_URL}/api/approvals", json={
                "trace_id": trace_id,
                "reason": f"{violation['owasp_category']}: {violation['description']}"
            })
            raise GovernanceBlockError(violation)
        elif violation["severity"] == "medium":
            # Quarantine but allow with caution
            requests.put(f"{API_URL}/api/workflows/{trace_id}/status", json={
                "status": "paused_governance",
                "current_agent": from_agent
            })
```

**Key point:** You don't need to implement the interceptor—that's separate. Just return clean output from your agent, and the interceptor will check it.

---

## Testing Locally

1. **Start the API:**
   ```bash
   python api.py
   ```

2. **Initialize database:**
   ```bash
   mysql -u root -p < schema.sql
   ```

3. **Call from your agent:**
   ```python
   import requests
   response = requests.post("http://localhost:5000/api/workflows/start", 
                           json={"ticket_id": "test_123"})
   print(response.json())
   ```

---

## Error Handling

All endpoints return standard JSON:

**Success (2xx):**
```json
{"trace_id": "uuid", "status": "created"}
```

**Error (4xx/5xx):**
```json
{"error": "description of what went wrong"}
```

Always check `response.status_code` before accessing `response.json()`.

---

## Summary

- **Start workflow**: `POST /api/workflows/start`
- **Log handoff**: `POST /api/handoffs`
- **Update status**: `PUT /api/workflows/{trace_id}/status`
- **Log governance event**: `POST /api/governance-events`
- **Request approval**: `POST /api/approvals`
- **Query audit log**: `GET /api/audit-log/query`
- **Log transaction**: `POST /api/refund-transactions`

That's it. Your agent work → log handoff → move on.
