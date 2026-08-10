from __future__ import annotations

import app.graph as app_graph


class _RecorderNode:
    def __init__(self, event_writer=None) -> None:
        self.event_writer = event_writer

    def __call__(self, _state):
        return {}


def test_build_graph_injects_same_governance_repository_into_triage_and_policy(monkeypatch):
    captured: dict[str, object] = {}

    fake_backend = object()
    fake_repository = object()

    monkeypatch.setattr(app_graph.GCPRepository, "from_env", staticmethod(lambda: fake_backend))

    def fake_database_repository(backend):
        assert backend is fake_backend
        return fake_repository

    monkeypatch.setattr(app_graph, "DatabaseGovernanceEventRepository", fake_database_repository)

    monkeypatch.setattr(app_graph.AzureJsonClient, "from_env", staticmethod(lambda: object()))

    def fake_build_policy_agent_graph(client=None, *, event_writer=None):
        captured["policy_client"] = client
        captured["policy_event_writer"] = event_writer
        return _RecorderNode(event_writer=event_writer)

    monkeypatch.setattr(app_graph, "build_policy_agent_graph", fake_build_policy_agent_graph)

    def fake_build_triage_agent_graph(*, client=None, event_writer=None):
        captured["triage_client"] = client
        captured["triage_event_writer"] = event_writer
        return _RecorderNode(event_writer=event_writer)

    monkeypatch.setattr(app_graph, "build_triage_agent_graph", fake_build_triage_agent_graph)

    graph = app_graph.build_graph()

    assert graph is not None
    assert captured["policy_event_writer"] is fake_repository
    assert captured["triage_event_writer"] is fake_repository
    assert captured["policy_client"] is captured["triage_client"]
