"""Tests du collecteur, avec de faux objets de l'API Kubernetes.

On ne simule que les attributs que le collecteur lit : pas besoin de
cluster pour vérifier la logique de sélection du pod.
"""

import asyncio
from types import SimpleNamespace as NS

from app.collector import (
    Evidence,
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
    def __init__(self, pods):
        self.pods = pods

    async def read_namespaced_pod(self, name, namespace):
        for pod in self.pods:
            if pod.metadata.name == name:
                return pod
        raise LookupError(name)

    async def list_namespaced_pod(self, namespace):
        return NS(items=self.pods)


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
