from __future__ import annotations

import app.graph as app_graph


class _RecorderNode:
    def __call__(self, _state):
        return {}


def test_parent_graph_shares_repository_with_all_persisting_subgraphs(monkeypatch) -> None:
    captured: dict[str, object] = {}
    cloud_repository = object()
    governance_repository = object()
    azure_client = object()
    pipeline_store = object()

    def fake_governance_repository(repository):
        captured["governance_backend"] = repository
        return governance_repository

    def fake_pipeline_store(repository):
        captured.setdefault("pipeline_backends", []).append(repository)
        return pipeline_store

    def fake_policy_graph(client=None, *, store=None):
        captured["policy_client"] = client
        captured["policy_store"] = store
        return _RecorderNode()

    def fake_triage_graph(*, client=None, event_writer=None, store=None):
        captured["triage_client"] = client
        captured["triage_event_writer"] = event_writer
        captured["triage_store"] = store
        return _RecorderNode()

    def fake_response_graph(*, client=None, store=None, event_writer=None):
        captured["response_client"] = client
        captured["response_store"] = store
        captured["response_event_writer"] = event_writer
        return _RecorderNode()

    monkeypatch.setattr(app_graph, "DatabaseGovernanceEventRepository", fake_governance_repository)
    monkeypatch.setattr(app_graph, "PipelineStore", fake_pipeline_store)
    monkeypatch.setattr(app_graph, "build_policy_agent_graph", fake_policy_graph)
    monkeypatch.setattr(app_graph, "build_triage_agent_graph", fake_triage_graph)
    monkeypatch.setattr(app_graph, "build_response_agent_graph", fake_response_graph)

    graph = app_graph.build_graph(client=azure_client, repository=cloud_repository)

    assert graph is not None
    assert captured["governance_backend"] is cloud_repository
    assert captured["pipeline_backends"] == [cloud_repository] * 4
    assert captured["triage_store"] is pipeline_store
    assert captured["policy_store"] is pipeline_store
    assert captured["response_store"] is pipeline_store
    assert captured["triage_event_writer"] is governance_repository
    assert captured["response_event_writer"] is governance_repository
    assert captured["policy_client"] is captured["triage_client"] is azure_client
    assert captured["response_client"] is azure_client
    nodes = set(graph.get_graph().nodes) - {"__start__", "__end__"}
    assert "policy_persistence" not in nodes
