from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import dashboard_app.api as dashboard_api
from dashboard_app.repository import DashboardRepositoryError


def _config(database: str, status: str = "ok") -> dict:
    return {
        "status": status,
        "database": database,
        "configured": {
            "MYSQL_HOST": True,
            "MYSQL_USER": True,
            "MYSQL_PASSWORD": True,
        },
        "missing": [],
    }


def test_health_fails_closed_before_connecting_when_database_is_not_final(monkeypatch):
    connected = False

    def from_env():
        nonlocal connected
        connected = True
        raise AssertionError("health must not connect to an unsafe database")

    monkeypatch.setattr(
        dashboard_api,
        "database_config_status",
        lambda: _config("main_db", "unsafe_database"),
    )
    monkeypatch.setattr(dashboard_api.DashboardRepository, "from_env", from_env)

    with TestClient(dashboard_api.app) as client:
        response = client.get("/api/health")

    assert response.status_code == 503
    assert response.json()["status"] == "degraded"
    assert response.json()["database"] == {"status": "not_checked"}
    assert connected is False


def test_health_rejects_an_unexpected_selected_database(monkeypatch):
    monkeypatch.setattr(
        dashboard_api,
        "database_config_status",
        lambda: _config("final"),
    )
    unsafe_repository = SimpleNamespace(
        database_name="final",
        ping=lambda: {"status": "ok", "database": "main_db"},
    )
    monkeypatch.setattr(
        dashboard_api.DashboardRepository,
        "from_env",
        lambda: unsafe_repository,
    )

    with TestClient(dashboard_api.app) as client:
        response = client.get("/health")

    assert response.status_code == 503
    assert response.json()["status"] == "degraded"
    assert response.json()["database"] == {
        "status": "ok",
        "database": "main_db",
    }


def test_dashboard_dependency_factory_rejects_non_final_repository(monkeypatch):
    monkeypatch.setattr(
        dashboard_api.DashboardRepository,
        "from_env",
        lambda: SimpleNamespace(database_name="main_db"),
    )

    with pytest.raises(DashboardRepositoryError, match="restricted to the final database"):
        dashboard_api.get_dashboard_service()
