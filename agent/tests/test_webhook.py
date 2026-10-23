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
    batches = []

    async def fake_process_alerts(alerts):
        batches.append(alerts)

    monkeypatch.setattr(main, "process_alerts", fake_process_alerts)
    # L'anti-doublon est un état du module : chaque test part de zéro.
    monkeypatch.setattr(main, "_recent_alerts", {})
    return batches


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
        "duplicates": 0,
        "alertnames": ["DemoAppCrashLooping"],
    }
    assert [[a.alertname for a in batch] for batch in received] == [["DemoAppCrashLooping"]]


def test_same_incident_is_analyzed_once(client, received):
    # Alertmanager renvoie tout le groupe quand une alerte y entre : l'alerte
    # déjà diagnostiquée ne doit pas repartir pour une inférence.
    alert = {"status": "firing", "fingerprint": "abc", "startsAt": "2026-01-01T10:00:00Z"}
    newcomer = {"status": "firing", "fingerprint": "def", "startsAt": "2026-01-01T10:01:00Z"}

    client.post("/webhook", json={"alerts": [alert]})
    second = client.post("/webhook", json={"alerts": [alert, newcomer]}).json()

    assert (second["analyzed"], second["duplicates"]) == (1, 1)
    assert [[a.fingerprint for a in batch] for batch in received] == [["abc"], ["def"]]


def test_new_occurrence_of_same_alert_is_analyzed(client, received):
    # Même empreinte, nouveau `startsAt` : l'incident s'est résolu puis a repris.
    first = {"status": "firing", "fingerprint": "abc", "startsAt": "2026-01-01T10:00:00Z"}
    again = {**first, "startsAt": "2026-01-01T11:00:00Z"}

    client.post("/webhook", json={"alerts": [first]})
    assert client.post("/webhook", json={"alerts": [again]}).json()["analyzed"] == 1


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
