"""Tests de l'auto-guérison, avec un faux client AppsV1.

Comme pour le collecteur, on ne simule que ce que le code lit et écrit :
le Deployment (sélecteur, conteneurs, annotations) et les appels
`read`/`patch`. Le faux client ENREGISTRE chaque patch : c'est ce qui
permet de vérifier ce que l'agent aurait envoyé au serveur d'API.
"""

import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace as NS

import pytest

from app import main
from app.diagnosis import Verdict, diagnose
from app.models import Alert
from app.remediation import (
    ANNOTATION_AT,
    AutohealConfig,
    compute_new_limit,
    format_remediation_for_prompt,
    owner_deployment_name,
    remediate,
)

NOW = datetime(2026, 10, 22, 12, 0, tzinfo=timezone.utc)

OOM = Verdict(code="OOMKilled", cause="oom", evidence="reason=OOMKilled", confidence="high")

# Activé, en mode réel : la configuration la plus permissive. Chaque test
# qui vérifie un garde-fou part de là et n'en désactive qu'un.
LIVE = AutohealConfig(enabled=True, dry_run=False, memory_max="512Mi", cooldown_s=3600)


def make_pod(name="demo-app-7d9f8c6b5-abcde", rs="demo-app-7d9f8c6b5", owner_kind="ReplicaSet"):
    """Un pod OOMKilled, créé par un ReplicaSet de Deployment."""
    terminated = NS(
        reason="OOMKilled",
        exit_code=137,
        signal=None,
        message=None,
        started_at=None,
        finished_at=None,
    )
    container = NS(
        name="demo-app",
        image="localhost/demo-app:1.0.4",
        ready=True,
        state=NS(running=NS(), waiting=None, terminated=None),
        last_state=NS(terminated=terminated),
        restart_count=1,
        resources=NS(requests=None, limits=None),
    )
    return NS(
        metadata=NS(
            name=name,
            namespace="aiops",
            labels={"app": "demo-app", "pod-template-hash": "7d9f8c6b5"},
            owner_references=[NS(kind=owner_kind, name=rs, controller=True)],
        ),
        spec=NS(node_name="node", restart_policy="Always", service_account_name="default"),
        status=NS(phase="Running", pod_ip="10.0.0.1", qos_class="Burstable",
                  container_statuses=[container]),
    )


def make_deployment(memory="128Mi", annotations=None, selector=None):
    container = NS(
        name="demo-app",
        resources=NS(limits={"memory": memory, "cpu": "200m"}, requests={"memory": "64Mi"}),
    )
    return NS(
        metadata=NS(
            name="demo-app",
            namespace="aiops",
            annotations=annotations,
            resource_version="4242",
        ),
        spec=NS(
            selector=NS(match_labels=selector or {"app": "demo-app"}),
            template=NS(spec=NS(containers=[container])),
        ),
    )


class FakeAppsV1:
    """Imite `read_namespaced_deployment` et `patch_namespaced_deployment`.

    Chaque patch est enregistré avec ses options (`dry_run`,
    `_content_type`) : le test vérifie ce qui aurait été envoyé.
    """

    def __init__(self, deployment=None, patch_error=None):
        self.deployment = deployment
        self.patch_error = patch_error
        self.patches = []

    async def read_namespaced_deployment(self, name, namespace, **_):
        if self.deployment is None or self.deployment.metadata.name != name:
            raise LookupError(name)
        return self.deployment

    async def patch_namespaced_deployment(self, name, namespace, body, **kwargs):
        if self.patch_error:
            raise self.patch_error
        self.patches.append({"name": name, "namespace": namespace, "body": body, **kwargs})
        return self.deployment


def run(apps, verdict=OOM, config=LIVE, pod=None, container="demo-app", now=NOW):
    return asyncio.run(
        remediate(apps, pod or make_pod(), verdict, container, config, now=now)
    )


def patched_memory(patch):
    containers = patch["body"]["spec"]["template"]["spec"]["containers"]
    return {c["name"]: c["resources"]["limits"]["memory"] for c in containers}


# ---------------------------------------------------------------------------
# Régression : le chemin complet, de l'alerte au patch
# ---------------------------------------------------------------------------
class FakeCoreV1:
    """Juste ce que `analyze_one_alert` lit dans CoreV1Api."""

    def __init__(self, pod):
        self.pod = pod

    async def read_namespaced_pod(self, name, namespace, **_):
        if name != self.pod.metadata.name:
            raise LookupError(name)
        return self.pod

    async def list_namespaced_pod(self, namespace, **_):
        return NS(items=[self.pod])

    async def list_namespaced_event(self, namespace, **_):
        return NS(items=[])

    async def read_namespaced_pod_log(self, *_, **__):
        return "allocation de 8 Mio\n"


def test_oomkilled_alert_patches_the_deployment_memory_limit(monkeypatch, caplog):
    """Un OOMKilled certain doit relever la limite du Deployment, et le dire.

    Avant l'étape 4, l'agent diagnostiquait sans agir : aucun patch, et
    aucune clé `remediation` dans ANALYSIS_RESULT.
    """
    pod = make_pod()
    apps = FakeAppsV1(make_deployment("128Mi"))
    prompts = []

    async def fake_ollama(_client, _url, _model, _context, verdict_block, **_):
        prompts.append(verdict_block)
        return {"ok": True, "analysis": "ok", "error": None, "duration_s": 0}

    async def no_metrics(*_args, **_kwargs):
        return None

    monkeypatch.setattr(main, "core_v1", FakeCoreV1(pod))
    monkeypatch.setattr(main, "apps_v1", apps, raising=False)
    monkeypatch.setattr(main, "AUTOHEAL", LIVE, raising=False)
    monkeypatch.setattr(main, "http_client", object())
    monkeypatch.setattr(main, "collect_metrics", no_metrics)
    monkeypatch.setattr(main, "call_ollama", fake_ollama)

    alert = Alert(
        labels={"alertname": "DemoAppOOMKilled", "namespace": "aiops", "severity": "critical"},
        annotations={"pod_name": pod.metadata.name},
    )
    with caplog.at_level(logging.INFO, logger="aiops-agent"):
        asyncio.run(main.analyze_one_alert(alert))

    assert len(apps.patches) == 1
    patch = apps.patches[0]
    assert patch["name"] == "demo-app"
    assert patch["_content_type"] == "application/strategic-merge-patch+json"
    assert patch["dry_run"] is None
    assert patched_memory(patch) == {"demo-app": "192Mi"}

    # Le LLM est informé de l'action, mais ne l'a pas choisie.
    assert "ACTION AUTOMATIQUE" in prompts[0]

    line = next(r.getMessage() for r in caplog.records if "ANALYSIS_RESULT" in r.getMessage())
    result = json.loads(line.split("ANALYSIS_RESULT ", 1)[1])
    assert result["remediation"]["status"] == "applied"
    assert result["remediation"]["from"] == "128Mi"
    assert result["remediation"]["to"] == "192Mi"


# ---------------------------------------------------------------------------
# Garde-fous
# ---------------------------------------------------------------------------
def test_disabled_by_default_sends_nothing():
    apps = FakeAppsV1(make_deployment())
    action = run(apps, config=AutohealConfig())
    assert apps.patches == []
    assert action["status"] == "disabled"


def test_dry_run_sends_a_server_side_dry_run():
    apps = FakeAppsV1(make_deployment())
    action = run(apps, config=AutohealConfig(enabled=True, dry_run=True))
    assert action["status"] == "dry-run"
    assert apps.patches[0]["dry_run"] == "All"
    assert patched_memory(apps.patches[0]) == {"demo-app": "192Mi"}


def test_patch_is_tied_to_the_version_read():
    # resourceVersion dans le corps : si le Deployment a changé entre la
    # lecture et le patch, le serveur répond 409 au lieu d'écraser.
    apps = FakeAppsV1(make_deployment())
    run(apps)
    body = apps.patches[0]["body"]
    assert body["metadata"]["resourceVersion"] == "4242"
    assert body["metadata"]["annotations"][ANNOTATION_AT] == NOW.isoformat()


def test_cooldown_blocks_a_second_action():
    recent = (NOW - timedelta(minutes=10)).isoformat()
    apps = FakeAppsV1(make_deployment(annotations={ANNOTATION_AT: recent}))
    action = run(apps)
    assert apps.patches == []
    assert action["status"] == "skipped"
    assert "cooldown" in action["reason"]


def test_cooldown_expired_allows_a_new_action():
    old = (NOW - timedelta(hours=2)).isoformat()
    apps = FakeAppsV1(make_deployment(annotations={ANNOTATION_AT: old}))
    assert run(apps)["status"] == "applied"


def test_cap_reached_skips():
    apps = FakeAppsV1(make_deployment("512Mi"))
    action = run(apps)
    assert apps.patches == []
    assert action["status"] == "skipped"
    assert "plafond" in action["reason"]


def test_cap_clamps_the_new_limit():
    apps = FakeAppsV1(make_deployment("400Mi"))
    run(apps)
    assert patched_memory(apps.patches[0]) == {"demo-app": "512Mi"}


def test_uncertain_verdict_is_not_eligible():
    # exitCode 137 sans reason=OOMKilled : SIGKILL de cause inconnue.
    verdict = diagnose({"reason": "Error", "exitCode": 137})
    assert verdict.confidence == "medium"
    apps = FakeAppsV1(make_deployment())
    assert run(apps, verdict=verdict) is None
    assert apps.patches == []


def test_pod_without_deployment_owner_skips():
    apps = FakeAppsV1(make_deployment())
    action = run(apps, pod=make_pod(owner_kind="StatefulSet"))
    assert apps.patches == []
    assert action["status"] == "skipped"


def test_deployment_whose_selector_does_not_match_pod_skips():
    apps = FakeAppsV1(make_deployment(selector={"app": "autre"}))
    action = run(apps)
    assert apps.patches == []
    assert action["status"] == "skipped"


def test_api_error_is_reported_not_raised():
    apps = FakeAppsV1(make_deployment(), patch_error=RuntimeError("403 Forbidden"))
    action = run(apps)
    assert action["status"] == "failed"
    assert "403" in action["reason"]


# ---------------------------------------------------------------------------
# Briques
# ---------------------------------------------------------------------------
def test_owner_deployment_name_strips_the_template_hash():
    assert owner_deployment_name(make_pod()) == "demo-app"


@pytest.mark.parametrize(
    "current, expected",
    [
        ("128Mi", "192Mi"),
        ("192Mi", "288Mi"),
        ("100M", "144Mi"),  # 1,5 x 100 Mo = 143,05 Mio, arrondi au Mio supérieur
        ("400Mi", "512Mi"),
        ("512Mi", None),
        ("1Gi", None),
    ],
)
def test_compute_new_limit(current, expected):
    assert compute_new_limit(current, 1.5, "512Mi") == expected


def test_config_from_env_is_fail_safe():
    # Une valeur mal tapée ne doit jamais activer l'action ni couper le dry-run.
    config = AutohealConfig.from_env({"AUTOHEAL_ENABLED": "oui", "AUTOHEAL_DRY_RUN": "flase"})
    assert config.enabled is False
    assert config.dry_run is True

    config = AutohealConfig.from_env({"AUTOHEAL_ENABLED": "true", "AUTOHEAL_DRY_RUN": "false"})
    assert config.enabled is True
    assert config.dry_run is False


def test_config_rejects_a_factor_that_would_not_raise_the_limit():
    with pytest.raises(ValueError):
        AutohealConfig.from_env({"AUTOHEAL_MEMORY_FACTOR": "0.8"})


def test_prompt_says_the_code_decided():
    text = format_remediation_for_prompt(
        {"status": "applied", "deployment": "demo-app", "container": "demo-app",
         "from": "128Mi", "to": "192Mi", "reason": None}
    )
    assert "ACTION AUTOMATIQUE" in text
    assert "192Mi" in text
    assert "AUCUNE ACTION AUTOMATIQUE" in format_remediation_for_prompt(
        {"status": "disabled", "reason": "auto-guérison désactivée"}
    )
