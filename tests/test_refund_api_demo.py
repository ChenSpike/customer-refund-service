from __future__ import annotations

import inspect

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

import refund_app.api as api
from demo.catalog import DEMO_IDS, load_demo_catalog


class _HealthCursor:
    def __init__(self, selected_database: str | None) -> None:
        self.selected_database = selected_database
        self.closed = False

    def execute(self, statement: str) -> None:
        assert statement == "SELECT DATABASE() AS database_name"

    def fetchone(self):
        return {"database_name": self.selected_database}

    def close(self) -> None:
        self.closed = True


class _HealthConnection:
    def __init__(self, selected_database: str | None) -> None:
        self.cursor_instance = _HealthCursor(selected_database)
        self.closed = False

    def cursor(self, *, dictionary: bool = False):
        assert dictionary is True
        return self.cursor_instance

    def close(self) -> None:
        self.closed = True


class _HealthRepository:
    def __init__(
        self,
        *,
        configured_database: str = "final",
        selected_database: str | None = "final",
        connect_error: Exception | None = None,
    ) -> None:
        self.database_name = configured_database
        self.connection = _HealthConnection(selected_database)
        self.connect_error = connect_error

    def _connect(self):
        if self.connect_error is not None:
            raise self.connect_error
        return self.connection


def _install_health_repository(monkeypatch, repository: _HealthRepository) -> None:
    from db.database import GCPRepository

    monkeypatch.setattr(
        GCPRepository,
        "from_env",
        classmethod(lambda _cls: repository),
    )


def test_cases_endpoint_exposes_only_canonical_twenty() -> None:
    payload = api.cases()

    assert payload["database"] == "final"
    assert [case["case_id"] for case in payload["cases"]] == list(DEMO_IDS)


def test_offline_health_is_ok_without_touching_database(monkeypatch) -> None:
    monkeypatch.setenv("REFUND_MODE", "offline")
    monkeypatch.setenv("REFUND_DB", "fake")
    monkeypatch.setenv("MYSQL_DATABASE", "main_db")
    monkeypatch.setattr(
        api,
        "_check_live_database",
        lambda: pytest.fail("offline health must not touch MySQL"),
    )

    response = TestClient(api.app).get("/api/health")

    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "mode": "offline",
        "database": "final",
        "database_status": "not_checked",
    }


def test_invalid_refund_mode_health_returns_503(monkeypatch) -> None:
    monkeypatch.setenv("REFUND_MODE", "invalid")

    response = TestClient(api.app).get("/api/health")

    assert response.status_code == 503
    assert response.json()["status"] == "misconfigured"
    assert "REFUND_MODE" in response.json()["detail"]


@pytest.mark.parametrize(
    ("refund_db", "mysql_database", "detail"),
    [
        ("fake", "final", "REFUND_DB=real"),
        ("real", "main_db", "MYSQL_DATABASE=final"),
    ],
)
def test_live_health_rejects_unsafe_database_configuration_before_connecting(
    monkeypatch,
    refund_db: str,
    mysql_database: str,
    detail: str,
) -> None:
    monkeypatch.setenv("REFUND_MODE", "live")
    monkeypatch.setenv("REFUND_DB", refund_db)
    monkeypatch.setenv("MYSQL_DATABASE", mysql_database)
    repository = _HealthRepository(connect_error=AssertionError("must not connect"))
    _install_health_repository(monkeypatch, repository)

    response = TestClient(api.app).get("/api/health")

    assert response.status_code == 503
    assert response.json()["status"] == "misconfigured"
    assert detail in response.json()["detail"]


def test_live_health_verifies_selected_final_database_and_closes_resources(monkeypatch) -> None:
    monkeypatch.setenv("REFUND_MODE", "live")
    monkeypatch.setenv("REFUND_DB", "real")
    monkeypatch.setenv("MYSQL_DATABASE", "final")
    repository = _HealthRepository()
    _install_health_repository(monkeypatch, repository)

    response = TestClient(api.app).get("/api/health")

    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "mode": "live",
        "database": "final",
        "database_status": "ok",
    }
    assert repository.connection.cursor_instance.closed is True
    assert repository.connection.closed is True


def test_live_health_rejects_wrong_selected_database_and_closes_resources(monkeypatch) -> None:
    monkeypatch.setenv("REFUND_MODE", "live")
    monkeypatch.setenv("REFUND_DB", "real")
    monkeypatch.setenv("MYSQL_DATABASE", "final")
    repository = _HealthRepository(selected_database="main_db")
    _install_health_repository(monkeypatch, repository)

    response = TestClient(api.app).get("/api/health")

    assert response.status_code == 503
    assert response.json()["status"] == "misconfigured"
    assert response.json()["detail"] == "Connected database is not 'final'"
    assert repository.connection.cursor_instance.closed is True
    assert repository.connection.closed is True


def test_live_health_hides_connection_error_details(monkeypatch) -> None:
    monkeypatch.setenv("REFUND_MODE", "live")
    monkeypatch.setenv("REFUND_DB", "real")
    monkeypatch.setenv("MYSQL_DATABASE", "final")
    repository = _HealthRepository(
        connect_error=TimeoutError("private connection details must not escape")
    )
    _install_health_repository(monkeypatch, repository)

    response = TestClient(api.app).get("/api/health")

    assert response.status_code == 503
    assert response.json() == {
        "status": "degraded",
        "mode": "live",
        "database": "final",
        "detail": "Live database check failed",
        "error_type": "TimeoutError",
    }


def test_offline_api_uses_fixed_case_order_customer_and_message(monkeypatch) -> None:
    monkeypatch.setenv("REFUND_MODE", "offline")
    expected = load_demo_catalog().get("demo10")

    result = api.refund(api.RefundRequest(case_id="demo10"))

    assert result["success"] is True
    assert result["matched_expectations"] is True
    assert result["trace_id"] == "demo10"
    assert result["ticket_id"] == expected.ticket_id
    assert result["customer_id"] == expected.customer_id
    assert result["order_id"] == expected.order_id
    assert result["message"] == expected.message
    assert result["mode"] == {"workflow": "offline", "db": api._db_mode()}


@pytest.mark.parametrize(
    "refund_request",
    [
        api.RefundRequest(message="Make an unseeded refund case"),
        api.RefundRequest(case_id="demo01", order_id="order-demo02"),
        api.RefundRequest(case_id="demo01", user_id="customer-demo02"),
        api.RefundRequest(case_id="demo21"),
    ],
)
def test_api_rejects_unknown_or_mixed_case_identity(refund_request) -> None:
    with pytest.raises(HTTPException) as error:
        api.refund(refund_request)

    assert error.value.status_code == 422


def test_live_api_passes_resolved_seeded_case_to_runner_without_bootstrap(monkeypatch) -> None:
    case = load_demo_catalog().get("demo18")
    calls: list[tuple[str, str]] = []

    class Runner:
        def run_case(self, case_id: str):
            calls.append(("run", case_id))
            return {
                "success": True,
                "matched_expectations": True,
                "case_id": case_id,
                "timings_ms": {"total": 1.0},
            }

    def create(mode: str):
        calls.append(("create", mode))
        return Runner()

    monkeypatch.setenv("REFUND_MODE", "live")
    monkeypatch.setenv("REFUND_DB", "real")
    monkeypatch.setattr(api, "_create_runner", create)

    result = api.refund(
        api.RefundRequest(
            case_id="demo18",
            order_id=case.order_id,
            customer_id=case.customer_id,
            message=case.message,
        )
    )

    assert calls == [("create", "live"), ("run", "demo18")]
    assert result["trace_id"] == "demo18"
    assert result["ticket_id"] == "ticket-demo18"
    assert result["selected_order_id"] == "order-demo18"
    assert "order-demo99" in result["message"]


def test_live_runner_configuration_requires_real_db_before_construction(monkeypatch) -> None:
    constructed: list[str] = []
    monkeypatch.setenv("REFUND_DB", "fake")
    monkeypatch.setattr(api, "DemoRunner", lambda **kwargs: constructed.append(kwargs["mode"]))

    with pytest.raises(RuntimeError, match="REFUND_DB=real"):
        api._create_runner("live")

    assert constructed == []


def test_api_source_contains_no_root_insert_or_random_id_generation() -> None:
    source = inspect.getsource(api)

    assert "INSERT INTO" not in source.upper()
    assert "uuid" not in source
