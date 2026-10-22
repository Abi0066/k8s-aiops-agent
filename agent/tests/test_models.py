"""Tests des modèles du payload Alertmanager (format v4)."""

from app.models import Alert, AlertGroup


def test_firing_alerts_ignore_resolved():
    group = AlertGroup.model_validate(
        {
            "status": "firing",
            "alerts": [
                {"status": "firing", "labels": {"alertname": "A"}},
                {"status": "resolved", "labels": {"alertname": "B"}},
            ],
        }
    )
    assert [a.alertname for a in group.firing_alerts] == ["A"]


def test_unknown_fields_are_tolerated():
    group = AlertGroup.model_validate(
        {"alerts": [{"labels": {}, "futureField": 1}], "truncatedAlerts": 0}
    )
    assert len(group.alerts) == 1


def test_namespace_label_then_annotation_then_none():
    assert Alert(labels={"namespace": "a"}, annotations={"namespace": "b"}).namespace == "a"
    assert Alert(annotations={"namespace": "b"}).namespace == "b"
    # Pas de repli sur "default" : c'est à l'appelant de décider.
    assert Alert().namespace is None


def test_pod_name_annotation_wins_over_label():
    alert = Alert(labels={"pod": "from-label"}, annotations={"pod_name": "from-annotation"})
    assert alert.pod_name == "from-annotation"
    assert Alert(labels={"pod": "from-label"}).pod_name == "from-label"
    assert Alert().pod_name is None


def test_defaults_for_missing_labels():
    alert = Alert()
    assert alert.alertname == "UnknownAlert"
    assert alert.severity == "unknown"
