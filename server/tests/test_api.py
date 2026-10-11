"""Тесты HTTP-слоя: health и приём событий."""

from fastapi.testclient import TestClient

from app.main import create_app

client = TestClient(create_app())


def test_health() -> None:
    response = client.get("/api/v1/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_event_requires_api_key() -> None:
    payload = {
        "project": "flytaxi",
        "host": "db-master-01",
        "check": "disk_space",
        "severity": "critical",
        "summary": "Диск заполнен на 95%",
    }
    response = client.post("/api/v1/events", json=payload)
    assert response.status_code == 401


def test_event_accepted_and_dedup_key_built() -> None:
    payload = {
        "project": "flytaxi",
        "host": "db-master-01",
        "check": "disk_space",
        "severity": "critical",
        "summary": "Диск заполнен на 95%",
    }
    response = client.post("/api/v1/events", json=payload, headers={"X-API-Key": "test-key"})
    assert response.status_code == 202
    assert response.json()["dedup_key"] == "flytaxi:disk_space:db-master-01"


def test_event_rejects_blank_summary() -> None:
    payload = {
        "project": "flytaxi",
        "host": "db-master-01",
        "check": "disk_space",
        "severity": "warning",
        "summary": "   ",
    }
    response = client.post("/api/v1/events", json=payload, headers={"X-API-Key": "test-key"})
    assert response.status_code == 422
