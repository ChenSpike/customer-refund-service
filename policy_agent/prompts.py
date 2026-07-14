from __future__ import annotations

import json
from textwrap import dedent

from .models import PolicyAgentDraft, PolicyAgentInput


POLICY_DRAFT_SHAPE = """
{
  "case": {"trace_id": "...", "ticket_id": "...", "policy_version_used": "..."},
  "customer_request": {
    "sanitized_text": "...", "refund_reason": "...",
    "requested_amount": 0.00, "currency": "USD"
  },
  "policy_evaluation": {
    "matched_policies": [{
      "policy_id": "...", "rule_summary": "...", "input_fact_used": "...",
      "effect": "supports_approval|supports_denial|supports_partial|requires_review"
    }],
    "gaps_or_conflicts": [{
      "type": "missing_fact|policy_conflict|low_confidence", "detail": "..."
    }]
  },
  "decision": {
    "type": "approve|deny|partial_refund|request_info|manual_review",
    "refund_amount": 0.00, "confidence": 0.00, "reason": "..."
  },
  "response_guidance": {"customer_safe_summary": "...", "missing_info_to_request": []},
  "handoff": {"next_agent": "response_agent|human_approval|triage_agent", "reason": "..."}
}
""".strip()

POLICY_OUTPUT_SHAPE = POLICY_DRAFT_SHAPE[:-2] + ',\n  "governance": {' + (
    '"semantic_drift_score": 0.00, "interceptor_action": "allow|quarantine|block", "flags": []}'
) + "\n}"


def policy_instructions(policy_context: str) -> str:
    return dedent(
        f"""
        You are the Azure Policy Reasoning Agent in the iDox refund workflow.

        Use only the validated input and the human policy knowledge base below. Do not call or request tools,
        databases, refund issuers, or another knowledge source. Decide the policy outcome; local code will not
        make or override that decision.

        Return JSON only. Use the exact keys and order shown below. Do not include governance yet. Cite the
        human rule IDs you applied. Use request_info only for missing facts, manual_review when policy requires
        human judgment, and refund_amount 0.00 when no amount is approved. Never claim a refund was executed.

        Required JSON shape:
        {POLICY_DRAFT_SHAPE}

        Human policy knowledge base:
        {policy_context}
        """
    ).strip()


def governance_instructions(policy_context: str) -> str:
    return dedent(
        f"""
        You are the separate Azure Governance Agent in the iDox refund workflow.

        Review the policy draft against the original input, the same human policy knowledge base, and governance
        risks. You may preserve or revise the draft. Local code will only validate your JSON and routing consistency;
        it will not decide governance.

        Route request_info to triage_agent, manual_review to human_approval, and approve, deny, or partial_refund
        to response_agent. Quarantine or block must produce manual_review and human_approval. Governance flags
        may be: low_confidence, policy_conflict, semantic_drift, forbidden_tool, pii_risk. Do not turn an ordinary
        policy denial into manual review unless a separate governance or policy-review reason exists.

        Return JSON only in the exact keys and order shown below. Never claim a refund was executed.

        Required JSON shape:
        {POLICY_OUTPUT_SHAPE}

        Human policy knowledge base:
        {policy_context}
        """
    ).strip()


def repair_instructions(target: str, policy_context: str) -> str:
    shape = POLICY_DRAFT_SHAPE if target == "policy draft" else POLICY_OUTPUT_SHAPE
    return dedent(
        f"""
        You are an Azure JSON repair agent. Repair the invalid {target} without changing its policy meaning.
        Return JSON only, with no wrapper, comments, markdown, or extra fields.

        Required JSON shape:
        {shape}

        Human policy knowledge base:
        {policy_context}
        """
    ).strip()


def policy_input_message(policy_input: PolicyAgentInput) -> str:
    return "Return the policy decision draft as JSON for this exact input:\n" + policy_input.model_dump_json(indent=2)


def governance_input_message(policy_input: PolicyAgentInput, draft: PolicyAgentDraft) -> str:
    payload = {
        "policy_input": policy_input.model_dump(mode="json"),
        "policy_reasoning_draft": draft.model_dump(mode="json"),
    }
    return "Return the final governed Policy Agent output as JSON:\n" + json.dumps(payload, indent=2, ensure_ascii=False)


def repair_input_message(
    target: str,
    policy_input: PolicyAgentInput,
    invalid_json: str,
    validation_error: str,
) -> str:
    payload = {
        "target": target,
        "policy_input": policy_input.model_dump(mode="json"),
        "validation_error": validation_error,
        "invalid_json": invalid_json,
    }
    return "Repair this invalid Azure JSON response:\n" + json.dumps(payload, indent=2, ensure_ascii=False)
