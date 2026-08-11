from importlib import import_module


__all__ = [
    "PolicyAgentInput",
    "PolicyAgentOutput",
    "PolicyAgentService",
    "AppStatePolicyNode",
    "build_policy_agent_graph",
    "policy_input_from_state",
    "policy_output_from_state",
    "policy_result_from_state",
    "policy_stage_usage_from_state",
    "policy_usage_from_state",
    "reconstruct_policy_state",
]


def __getattr__(name: str):
    if name in {"PolicyAgentInput", "PolicyAgentOutput"}:
        module = import_module(".models", __name__)
        return getattr(module, name)

    if name in {
        "AppStatePolicyNode",
        "policy_input_from_state",
        "policy_output_from_state",
        "policy_result_from_state",
        "policy_stage_usage_from_state",
        "policy_usage_from_state",
        "reconstruct_policy_state",
    }:
        module = import_module(".policy_node", __name__)
        return getattr(module, name)

    if name == "build_policy_agent_graph":
        module = import_module(".graph", __name__)
        return getattr(module, name)

    if name == "PolicyAgentService":
        module = import_module(".service", __name__)
        return getattr(module, name)

    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
