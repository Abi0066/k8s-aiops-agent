"""Tests du collecteur, avec de faux objets de l'API Kubernetes.

On ne simule que les attributs que le collecteur lit : pas besoin de
cluster pour vérifier la logique de sélection du pod.
"""

import asyncio
from types import SimpleNamespace as NS

from app.collector import (
    Evidence,
    collect_events,
    collect_metrics,
    extract_last_termination,
    find_target_pod,
    summarize_pod_status,
)
from app.models import Alert


def make_pod(name, labels=None, phase="Running", waiting=None, terminated=None):
    state = NS(
        running=None if waiting else NS(),
        waiting=NS(reason=waiting, message=None) if waiting else None,
        terminated=None,
    )
    last_state = NS(terminated=terminated)
    container = NS(name="app", state=state, last_state=last_state, restart_count=3)
    return NS(
        metadata=NS(name=name, namespace="aiops", labels=labels),
        status=NS(phase=phase, container_statuses=[container]),
    )


class FakeCoreV1:
    """Imite les quelques méthodes de CoreV1Api que lit le collecteur.

    Comme le serveur d'API, `list_namespaced_event` applique le
    `field_selector` `involvedObject.name=...`.
    """

    def __init__(self, pods=(), events=()):
        self.pods = list(pods)
        self.events = list(events)

    async def read_namespaced_pod(self, name, namespace, **_):
        for pod in self.pods:
            if pod.metadata.name == name:
                return pod
        raise LookupError(name)

    async def list_namespaced_pod(self, namespace, **_):
        return NS(items=self.pods)

    async def list_namespaced_event(self, namespace, field_selector=None, **_):
        items = self.events
        if field_selector:
            wanted = field_selector.removeprefix("involvedObject.name=")
            items = [e for e in items if e.involved_object.name == wanted]
        return NS(items=items)


def find(pods, alert):
    return asyncio.run(find_target_pod(FakeCoreV1(pods), alert, "aiops"))


def test_pod_found_by_annotation():
    pods = [make_pod("other"), make_pod("target")]
    pod, method = find(pods, Alert(annotations={"pod_name": "target"}))
    assert pod.metadata.name == "target"
    assert method == "annotation pod_name"


def test_pod_without_labels_does_not_crash_label_matching():
    # Un pod sans aucun label a `metadata.labels = None` dans l'API.
    pods = [make_pod("bare", labels=None), make_pod("demo", labels={"app": "demo-app"})]
    pod, method = find(pods, Alert(labels={"component": "demo-app"}))
    assert pod.metadata.name == "demo"
    assert method == "déduit via label app=demo-app"


def test_falls_back_to_unhealthy_pod():
    pods = [make_pod("healthy"), make_pod("broken", waiting="CrashLoopBackOff")]
    pod, method = find(pods, Alert())
    assert pod.metadata.name == "broken"
    assert method == "déduit par état non-Healthy"


def test_missing_named_pod_falls_back_to_deduction():
    pods = [make_pod("demo", labels={"app": "demo-app"})]
    alert = Alert(labels={"component": "demo-app"}, annotations={"pod_name": "gone"})
    pod, _ = find(pods, alert)
    assert pod.metadata.name == "demo"


def test_empty_namespace():
    assert find([], Alert()) == (None, "aucun pod trouvé")


def test_extract_last_termination_reads_previous_crash():
    terminated = NS(
        reason="OOMKilled",
        exit_code=137,
        signal=None,
        message=None,
        started_at="t0",
        finished_at="t1",
    )
    pod = make_pod("p", waiting="CrashLoopBackOff", terminated=terminated)
    result = extract_last_termination(pod)
    assert result["reason"] == "OOMKilled"
    assert result["exitCode"] == 137
    assert result["restartCount"] == 3


def test_summarize_pod_status_reads_resources_from_status_or_spec():
    # Le client Kubernetes donne requests et limits sous forme de dict.
    def resources(memory):
        return NS(requests={"cpu": "50m"}, limits={"memory": memory})

    def status(name, res):
        return NS(
            name=name,
            image="img",
            ready=True,
            restart_count=0,
            state=NS(running=NS(), waiting=None, terminated=None),
            resources=res,
        )

    pod = NS(
        metadata=NS(name="p"),
        spec=NS(
            node_name="n",
            restart_policy="Always",
            service_account_name="default",
            # Ordre différent de containerStatuses : la spec se lit par nom.
            containers=[
                NS(name="sidecar", resources=resources("128Mi")),
                NS(name="app", resources=resources("64Mi")),
            ],
        ),
        status=NS(
            phase="Running",
            pod_ip="10.0.0.1",
            qos_class="Burstable",
            container_statuses=[
                # Cluster < 1.33 ou conteneur pas encore démarré.
                status("app", None),
                # Redimensionné en place : le statut fait foi, pas la spec.
                status("sidecar", resources("192Mi")),
            ],
        ),
    )
    containers = summarize_pod_status(pod)["containers"]
    assert containers[0]["resources"] == {
        "requests": {"cpu": "50m"},
        "limits": {"memory": "64Mi"},
    }
    assert containers[1]["resources"]["limits"] == {"memory": "192Mi"}


def test_prompt_context_lists_collection_errors():
    evidence = Evidence(alertname="DemoAppDown", namespace="aiops", errors=["aucun pod"])
    context = evidence.to_prompt_context()
    assert "ALERTE : DemoAppDown" in context
    assert "  - aucun pod" in context


def make_event(obj_name, reason, timestamp):
    return NS(
        type="Warning",
        reason=reason,
        message=f"{reason} on {obj_name}",
        involved_object=NS(kind="Pod", name=obj_name),
        count=1,
        last_timestamp=timestamp,
        event_time=None,
    )


def events_for(pod_name, events):
    evidence = Evidence()
    asyncio.run(collect_events(FakeCoreV1(events=events), "aiops", evidence, pod_name))
    return evidence.events


def test_events_are_restricted_to_the_target_pod():
    # Les événements des autres pods ne doivent pas occuper les places du prompt.
    events = [
        make_event("grafana-1", "Unhealthy", "2026-01-01T10:05:00"),
        make_event("demo-app-1", "BackOff", "2026-01-01T10:00:00"),
    ]
    assert [e["object"] for e in events_for("demo-app-1", events)] == ["Pod/demo-app-1"]


def test_events_fall_back_to_namespace_when_pod_has_none():
    events = [make_event("demo-app-rs", "FailedCreate", "2026-01-01T10:00:00")]
    assert [e["reason"] for e in events_for("demo-app-new", events)] == ["FailedCreate"]


class FakePrometheus:
    """Client HTTP minimal : enregistre les requêtes PromQL reçues."""

    def __init__(self, fail_on=None):
        self.queries = []
        self.fail_on = fail_on

    async def get(self, url, params, timeout):
        query = params["query"]
        self.queries.append(query)
        if self.fail_on and self.fail_on in query:
            raise RuntimeError("prometheus down")
        series = [{"metric": {"pod": "demo-app-1", "__name__": "x"}, "value": [0, "4"]}]
        return NS(raise_for_status=lambda: None, json=lambda: {"data": {"result": series}})


def test_metrics_are_scoped_to_the_target_pod():
    prom = FakePrometheus()
    evidence = Evidence()
    asyncio.run(collect_metrics(prom, "http://prom", "aiops", evidence, "demo-app-1"))

    restarts = next(q for q in prom.queries if "restarts_total" in q)
    assert 'namespace="aiops",pod="demo-app-1"' in restarts
    # `up` décrit une cible de scrape, il n'a pas de label pod.
    assert 'up{namespace="aiops"}' in prom.queries
    # Les labels inutiles (__name__) sont retirés du prompt.
    assert evidence.metrics["restarts"]["result"] == [
        {"labels": {"pod": "demo-app-1"}, "value": "4"}
    ]


def test_metrics_escape_label_values_from_the_alert():
    # Le namespace vient du payload de l'alerte : un `"` ne doit pas fermer
    # la chaîne PromQL.
    prom = FakePrometheus()
    asyncio.run(collect_metrics(prom, "http://prom", 'a"} or vector(1) #', Evidence()))
    assert 'up{namespace="a\\"} or vector(1) #"}' in prom.queries


def test_failed_metric_is_reported_and_others_are_kept():
    prom = FakePrometheus(fail_on="working_set")
    evidence = Evidence()
    asyncio.run(collect_metrics(prom, "http://prom", "aiops", evidence, "demo-app-1"))

    assert "memory_working_set_max_15m" not in evidence.metrics
    assert "restarts" in evidence.metrics
    assert any("memory_working_set_max_15m" in e for e in evidence.errors)
