from governance.audit_logger import log_governance_event


class GovernanceNode:
    def __init__(self, name: str, checkers: list):
        self.name = name
        self.checkers = checkers

    def __call__(self, state) -> dict:
        findings = [checker(state) for checker in self.checkers]
        blocked = [item for item in findings if item["status"] == "block"]

        result = {
            "stage": self.name,
            "status": "block" if blocked else "allow",
            "findings": blocked,
            "all_checks": findings,
        }

        log_governance_event(
            trace_id=state.get("trace_id", "unknown"),
            ticket_id=state.get("ticket_id"),
            user_id=state.get("user_id"),
            result=result,
            stage=self.name,
        )

        return {
            "governance_result": result,
        }