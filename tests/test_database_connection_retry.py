from __future__ import annotations

import mysql.connector

from db import database
from db.database import GCPRepository


def test_connect_retries_cloud_sql_read_timeout(monkeypatch) -> None:
    attempts: list[int] = []
    sleeps: list[int] = []
    connection = object()

    def connect(**_config):
        attempts.append(1)
        if len(attempts) == 1:
            raise mysql.connector.Error(
                msg="The Read Operation timed out",
                errno=3024,
            )
        return connection

    monkeypatch.setattr(database.mysql.connector, "connect", connect)
    monkeypatch.setattr(database.time, "sleep", sleeps.append)

    repository = GCPRepository(
        {
            "host": "example.invalid",
            "user": "demo",
            "password": "not-used",
            "database": "final",
        }
    )

    assert repository._connect() is connection
    assert len(attempts) == 2
    assert sleeps == [1]
