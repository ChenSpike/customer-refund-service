import re

from governance.models import GovernanceCheckResult

EMAIL_RE = re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.IGNORECASE)
PHONE_RE = re.compile(r"\b(?:\+?\d[\d\s().-]{7,}\d)\b")
IDENTIFIER_RE = re.compile(r"\b(?:TRACE|TICKET|ORDER|ORD|WORKFLOW)[-_:][A-Z0-9-]+\b", re.IGNORECASE)


def allow_result(name: str, *, source: str = "deterministic") -> GovernanceCheckResult:
    return GovernanceCheckResult(name=name, status="allow", source=source)


def block_result(name: str, detail: str, evidence: dict | None = None, *, source: str = "deterministic") -> GovernanceCheckResult:
    return GovernanceCheckResult(
        name=name,
        status="block",
        detail=detail,
        evidence=evidence or {},
        source=source,
    )


def check_pii_risk(state) -> GovernanceCheckResult:
    triage_output = state.get("triage_output", {})
    text = triage_output.get("customer_request", {}).get("sanitized_text", "")

    if not text:
        return allow_result("pii_risk")

    emails = EMAIL_RE.findall(text)
    if emails:
        return block_result("pii_risk", f"Detected email address: {emails[0]}", {"email": emails[0]})

    return allow_result("pii_risk")


def check_semantic_drift(state) -> GovernanceCheckResult:
    triage_output = state.get("triage_output", {})
    text = triage_output.get("customer_request", {}).get("sanitized_text", "").lower()

    patterns = [
        "ignore previous instructions",
        "ignore all previous instructions",
        "bypass policy",
        "override policy",
        "disregard the rules",
        "jailbreak",
        "system prompt",
    ]

    for pattern in patterns:
        if pattern in text:
            return block_result("semantic_drift", f"Detected suspicious pattern: {pattern}", {"pattern": pattern})

    return allow_result("semantic_drift")


def check_tool_misuse(state) -> GovernanceCheckResult:
    reason = (state.get("policy_decision", {}) or {}).get("reason", "").lower()

    patterns = [
        "i already issued the refund",
        "refund has been processed",
        "database updated",
        "i checked the database directly",
        "tool executed",
    ]

    for pattern in patterns:
        if pattern in reason:
            return block_result("forbidden_tool", f"Detected forbidden tool/action claim: {pattern}", {"pattern": pattern})

    return allow_result("forbidden_tool")


def check_sensitive_identifier_patterns(state) -> GovernanceCheckResult:
    triage_output = state.get("triage_output", {})
    text = triage_output.get("customer_request", {}).get("sanitized_text", "")

    if not text:
        return allow_result("pii_risk")

    phone = PHONE_RE.search(text)
    if phone:
        return block_result("pii_risk", f"Detected phone number pattern: {phone.group(0)}", {"phone": phone.group(0)})

    identifier = IDENTIFIER_RE.search(text)
    if identifier:
        return block_result("pii_risk", f"Detected internal identifier pattern: {identifier.group(0)}", {"identifier": identifier.group(0)})

    return allow_result("pii_risk")


def check_abnormal_input_shape(state) -> GovernanceCheckResult:
    triage_output = state.get("triage_output", {})
    text = triage_output.get("customer_request", {}).get("sanitized_text", "")

    if not text:
        return allow_result("semantic_drift")

    if len(text) > 2000:
        return block_result("semantic_drift", "Detected unusually long customer input", {"length": len(text)})

    suspicious_markers = ["<system>", "```json", "function_call", "tool_result", "developer message"]
    for marker in suspicious_markers:
        if marker in text.lower():
            return block_result("semantic_drift", f"Detected suspicious payload marker: {marker}", {"marker": marker})

    return allow_result("semantic_drift")


def check_required_evidence_completeness(state) -> GovernanceCheckResult:
    policy_context = state.get("policy_context") or {}
    evidence_manifest = policy_context.get("evidence_manifest") or {}
    required_fact_paths = evidence_manifest.get("required_fact_paths") or []
    evidence_items = evidence_manifest.get("evidence_items") or []

    if not required_fact_paths:
        return block_result("forbidden_tool", "Missing required_fact_paths in policy evidence manifest")
    if not evidence_items:
        return block_result("forbidden_tool", "Missing evidence_items in policy evidence manifest")

    return allow_result("forbidden_tool")


def check_handoff_safety(state) -> GovernanceCheckResult:
    policy_decision = state.get("policy_decision") or {}
    decision = policy_decision.get("decision")
    refund_amount = policy_decision.get("refund_amount")

    if decision in {"approve", "partial_refund"} and (refund_amount is None or refund_amount <= 0):
        return block_result("forbidden_tool", f"Unsafe handoff state: {decision} requires positive refund_amount")

    return allow_result("forbidden_tool")