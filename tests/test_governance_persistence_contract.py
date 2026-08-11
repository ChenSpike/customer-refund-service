from __future__ import annotations

import app.graph as app_graph


class _RecorderNode:
    def __call__(self, _state):
        return {}


def test_parent_graph_shares_repository_and_persists_policy_once(monkeypatch) -> None:
    captured: dict[str, object] = {}
    cloud_repository = object()
    governance_repository = object()
    azure_client = object()
    pipeline_store = object()

    def fake_governance_repository(repository):
        captured["governance_backend"] = repository
        return governance_repository

    def fake_pipeline_store(repository):
        captured["pipeline_backend"] = repository
        return pipeline_store

    def fake_persistence_node(store):
        captured["persistence_store"] = store
        return _RecorderNode()

    def fake_policy_graph(client=None):
        captured["policy_client"] = client
        return _RecorderNode()

    def fake_triage_graph(*, client=None, event_writer=None):
        captured["triage_client"] = client
        captured["triage_event_writer"] = event_writer
        return _RecorderNode()

    monkeypatch.setattr(app_graph, "DatabaseGovernanceEventRepository", fake_governance_repository)
    monkeypatch.setattr(app_graph, "PipelineStore", fake_pipeline_store)
    monkeypatch.setattr(app_graph, "PolicyPersistenceNode", fake_persistence_node)
    monkeypatch.setattr(app_graph, "build_policy_agent_graph", fake_policy_graph)
    monkeypatch.setattr(app_graph, "build_triage_agent_graph", fake_triage_graph)

    graph = app_graph.build_graph(client=azure_client, repository=cloud_repository)

    assert graph is not None
    assert captured["governance_backend"] is cloud_repository
    assert captured["pipeline_backend"] is cloud_repository
    assert captured["persistence_store"] is pipeline_store
    assert captured["triage_event_writer"] is governance_repository
    assert captured["policy_client"] is captured["triage_client"] is azure_client
    nodes = set(graph.get_graph().nodes) - {"__start__", "__end__"}
    assert "policy_persistence" in nodes
    edges = {(edge.source, edge.target) for edge in graph.get_graph().edges}
    assert ("policy_agent", "policy_persistence") in edges
    assert not any(
        source == "policy_agent" and target != "policy_persistence"
        for source, target in edges
    )
