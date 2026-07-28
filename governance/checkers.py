import re

EMAIL_RE = re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.IGNORECASE)


def _allow(name: str) -> dict:
    return {"name": name, "status": "allow", "detail": "", "evidence": {}}


def _block(name: str, detail: str, evidence: dict | None = None) -> dict:
    return {
        "name": name,
        "status": "block",
        "detail": detail,
        "evidence": evidence or {},
    }


def check_pii_risk(state) -> dict:
    triage_output = state.get("triage_output", {})
    text = triage_output.get("customer_request", {}).get("sanitized_text", "")

    if not text:
        return _allow("pii_risk")

    emails = EMAIL_RE.findall(text)
    if emails:
        return _block("pii_risk", f"Detected email address: {emails[0]}", {"email": emails[0]})

    return _allow("pii_risk")


def check_semantic_drift(state) -> dict:
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
            return _block("semantic_drift", f"Detected suspicious pattern: {pattern}", {"pattern": pattern})

    return _allow("semantic_drift")


def check_tool_misuse(state) -> dict:
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
            return _block("tool_misuse", f"Detected forbidden tool/action claim: {pattern}", {"pattern": pattern})

    return _allow("tool_misuse")