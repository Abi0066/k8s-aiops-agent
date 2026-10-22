"""Tests de la route POST /webhook.

`TestClient` est utilisé sans bloc `with` : le lifespan ne s'exécute pas,
donc aucun appel à Kubernetes ni à Ollama. La tâche de fond est remplacée
pour vérifier ce qui lui est transmis.
"""

import pytest
from fastapi.testclient import TestClient

from app import main


@pytest.fixture
def received(monkeypatch):
    groups = []

    async def fake_process_alerts(group):
        groups.append(group)

    monkeypatch.setattr(main, "process_alerts", fake_process_alerts)
    return groups


@pytest.fixture
def client():
    return TestClient(main.app)


def test_firing_alert_is_accepted_and_analyzed(client, received):
    payload = {
        "version": "4",
        "receiver": "aiops-agent",
        "alerts": [
            {"status": "firing", "labels": {"alertname": "DemoAppCrashLooping"}},
            {"status": "resolved", "labels": {"alertname": "DemoAppDown"}},
        ],
    }
    response = client.post("/webhook", json=payload)
    assert response.status_code == 200
    assert response.json() == {
        "status": "accepted",
        "analyzed": 1,
        "alertnames": ["DemoAppCrashLooping"],
    }
    assert len(received) == 1


def test_only_resolved_alerts_trigger_nothing(client, received):
    payload = {"alerts": [{"status": "resolved", "labels": {"alertname": "X"}}]}
    response = client.post("/webhook", json=payload)
    assert response.status_code == 200
    assert response.json()["analyzed"] == 0
    assert received == []


def test_invalid_json_is_rejected(client, received):
    response = client.post(
        "/webhook", content=b"{not json", headers={"content-type": "application/json"}
    )
    assert response.status_code == 400
    assert received == []


def test_payload_with_wrong_shape_is_rejected(client, received):
    response = client.post("/webhook", json={"alerts": "not-a-list"})
    assert response.status_code == 400
    assert received == []


def test_live_probe_depends_on_nothing(client):
    response = client.get("/live")
    assert response.status_code == 200
    assert response.json() == {"status": "alive"}
