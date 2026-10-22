"""Collecte des preuves : API Kubernetes et Prometheus.

Ce module rassemble les informations brutes. Il ne contient AUCUNE
intelligence : aucun jugement sur la cause, aucun seuil, aucune interprétation.
Son unique
rôle est de ramener des faits vérifiables, que le module `llm` transformera
ensuite en diagnostic.

Cette séparation a une raison pédagogique : on peut lire `collector.py`
pour savoir exactement quelles preuves sont disponibles, sans être distrait
par la logique d'analyse.

Deux sources de preuves :

  - l'API Kubernetes : l'état du pod, ses logs, les événements du namespace ;
  - l'API Prometheus : l'historique chiffré (redémarrages, raison du dernier
    arrêt, consommation mémoire/CPU).

Ces deux sources sont complémentaires. Le pod dit CE QUI S'EST PASSÉ
("terminé avec le code 137"), Prometheus dit COMBIEN DE TEMPS et AVEC QUELLE
CONSOMMATION. L'agent a besoin des deux.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from kubernetes.aio import client as aio_client
from kubernetes.aio import config as aio_config

from .models import Alert

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Limites de collecte
# ---------------------------------------------------------------------------
# Pourquoi limiter la taille des preuves ?
#
# Le modèle a une fenêtre de contexte de 4096 tokens (num_ctx, voir llm.py).
# Si on lui envoie 20 000 tokens de logs, il tronque — ou pire, refuse. Le
# prompt de diagnostic se retrouverait amputé des informations les plus
# récentes, qui sont justement les plus pertinentes.
#
# On limite donc chaque section. C'est un arbitrage explicite : on préfère
# un diagnostic fondé sur les dernières lignes de log plutôt que sur 500 lignes
# anciennes et hors contexte.
MAX_LOG_LINES = 50
MAX_EVENTS = 15
MAX_PROMETHEUS_SERIES = 10


@dataclass
class Evidence:
    """Le dossier de preuves d'une alerte.

    Un objet simple qui regroupe tout ce qu'on sait sur l'incident. Il est
    ensuite sérialisé en texte pour être envoyé au LLM, et il est loggé en
    JSON pour être relu par un humain.
    """

    # Identité de l'alerte.
    alertname: str = ""
    severity: str = ""
    namespace: str = ""
    pod_name: Optional[str] = None
    summary: str = ""

    # État du pod (objet API brut, sérialisé).
    pod_status: Optional[Dict[str, Any]] = None

    # Raison du dernier arrêt, extraite pour la lisibilité.
    last_termination: Dict[str, Any] = field(default_factory=dict)

    # Logs : log courant + log du conteneur précédent (le plus utile !).
    logs: str = ""
    previous_logs: str = ""

    # Événements du namespace, du plus récent au plus ancien.
    events: List[Dict[str, str]] = field(default_factory=list)

    # Séries Prometheus pertinentes.
    metrics: Dict[str, Any] = field(default_factory=dict)

    # Ce que l'agent a réussi ou échoué à collecter (transparence).
    errors: List[str] = field(default_factory=list)

    def to_prompt_context(self) -> str:
        """Sérialise les preuves en texte pour le prompt du LLM.

        Le format est « section : contenu », lisible par un modèle qui a
        lu de la documentation technique. On ne cherche pas l' elegance,
        on cherche la lisibilité pour le modèle.
        """
        parts: List[str] = []

        parts.append(f"ALERTE : {self.alertname}")
        parts.append(f"SEVERITE : {self.severity}")
        parts.append(f"NAMESPACE : {self.namespace}")
        if self.pod_name:
            parts.append(f"POD : {self.pod_name}")
        if self.summary:
            parts.append(f"RESUME : {self.summary}")

        if self.last_termination:
            lt = self.last_termination
            parts.append(
                "DERNIER ARRET DU CONTENEUR :\n"
                f"  raison   = {lt.get('reason', 'inconnue')}\n"
                f"  code     = {lt.get('exitCode', 'inconnu')}\n"
                f"  signal   = {lt.get('signal', 'aucun')}\n"
                f"  message  = {lt.get('message', 'aucun')}\n"
                f"  redemarrages = {lt.get('restartCount', '?')}"
            )

        if self.pod_status:
            parts.append("ETAT DU POD :\n" + _indent_yaml(self.pod_status))

        if self.logs:
            parts.append("LOGS COURANTS (dernieres lignes) :\n" + self.logs)

        if self.previous_logs:
            parts.append(
                "LOGS DU CONTENEUR PRECEDENT (avant le crash) :\n"
                + self.previous_logs
            )

        if self.events:
            events_text = "\n".join(
                f"  [{e.get('type')}] {e.get('reason')}: {e.get('message')}"
                for e in self.events
            )
            parts.append("EVENEMENTS DU NAMESPACE :\n" + events_text)

        if self.metrics:
            parts.append("METRIQUES PROMETHEUS :\n" + _indent_yaml(self.metrics))

        if self.errors:
            parts.append(
                "LIMITES DE COLLECTE (données manquantes) :\n"
                + "\n".join(f"  - {e}" for e in self.errors)
            )

        return "\n\n".join(parts)


def _indent_yaml(data: Any, indent: int = 2) -> str:
    """Sérialise un dict en YAML indenté lisible.

    On utilise `yaml.safe_dump` si disponible, sinon un repli JSON. Le YAML
    est plus compact et plus lisible par un modèle de langage que du JSON
    avec ses accolades et ses guillemets.
    """
    try:
        import yaml

        return yaml.safe_dump(data, default_flow_style=False, sort_keys=False)
    except ImportError:
        import json

        return json.dumps(data, indent=indent, default=str)


# ---------------------------------------------------------------------------
# Client Kubernetes
# ---------------------------------------------------------------------------
async def build_k8s_client() -> aio_client.CoreV1Api:
    """Construit le client Kubernetes asynchrone.

    Pourquoi le client `Async` ? Dans un handler `async def`, le client
    synchrone bloquerait la boucle d'événements pendant tout l'appel réseau.

    La fonction est `async` car `kubernetes.aio.config.load_kube_config`
    est une coroutine : l'appeler sans `await` ne charge rien.

    Comment s'authentifier ?
    - Dans le cluster : `load_incluster_config()` lit le token et le
      certificat CA que Kubernetes a montés dans le pod, dans
      `/var/run/secrets/kubernetes.io/serviceaccount/`. Il n'y a PAS de
      fichier `~/.kube/config` dans un pod.
    - Hors du cluster : `load_kube_config()` lit le fichier `~/.kube/config`.
      C'est ce mode qui permet de tester l'agent sur sa machine.

    Le test `KUBERNETES_SERVICE_HOST` est le standard : Kubernetes injecte
    cette variable dans chaque pod.
    """
    import os

    in_cluster = "KUBERNETES_SERVICE_HOST" in os.environ
    if in_cluster:
        log.info("Authentification Kubernetes : in-cluster (ServiceAccount)")
        aio_config.load_incluster_config()
    else:
        log.info(
            "Authentification Kubernetes : kubeconfig local "
            "(hors cluster — mode développement)"
        )
        await aio_config.load_kube_config()

    return aio_client.CoreV1Api()


# ---------------------------------------------------------------------------
# Identification du pod concerné
# ---------------------------------------------------------------------------
async def find_target_pod(
    core_v1: aio_client.CoreV1Api,
    alert: Alert,
    namespace: str,
) -> tuple:
    """Trouve le pod concerné par l'alerte.

    Deux stratégies, dans cet ordre :

    1. **Le nom est connu.** L'annotation `pod_name` (ou le label `pod`)
       donne le nom exact. C'est le cas le plus précis et le plus rapide.

    2. **Le nom n'est pas connu.** L'alerte concerne un groupe (latence
       agrégée par service, par exemple). On liste alors les pods du
       namespace et on filtre sur les labels de l'alerte. Le label
       `component` de nos règles vaut "demo-app", qui correspond au label
       `app=demo-app` des pods. C'est une heuristique : si plusieurs pods
       correspondent, on prend le premier.

    Le cas 2 est ce qui rend l'agent **générique** : il ne dépend pas d'un
    label maison et fonctionne sur n'importe quel cluster.

    Le `namespace` est passé en paramètre et non lu depuis l'alerte : une
    alerte peut ne porter aucun label `namespace` (c'est le cas de la
    règle `up == 0`, dont la métrique sous-jacente n'a pas ce label).
    C'est à l'appelant de résoudre le namespace de repli.

    Retourne (pod, methode) où methode explique comment le pod a été trouvé.
    """

    # --- Stratégie 1 : le nom est donné -------------------------------
    if alert.pod_name:
        try:
            pod = await core_v1.read_namespaced_pod(
                name=alert.pod_name, namespace=namespace
            )
            log.info(
                "Pod identifié via annotation pod_name : %s/%s",
                namespace,
                alert.pod_name,
            )
            return pod, "annotation pod_name"
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "Pod %s introuvable dans %s (%s) — on passe à la déduction.",
                alert.pod_name,
                namespace,
                exc,
            )

    # --- Stratégie 2 : déduction par labels ---------------------------
    try:
        pods = await core_v1.list_namespaced_pod(namespace=namespace)
    except Exception as exc:  # noqa: BLE001
        log.error("Impossible de lister les pods de %s : %s", namespace, exc)
        return None, "échec"

    candidates = pods.items

    # Filtre par les labels de l'alerte. On essaie plusieurs conventions :
    #   component=demo-app  ->  app=demo-app   (notre convention)
    #   app=xxx             ->  app=xxx        (convention K8s standard)
    wanted = alert.labels.get("component")
    if wanted:
        for key in ("app", "app.kubernetes.io/name", "component"):
            filtered = [
                p for p in candidates if (p.metadata.labels or {}).get(key) == wanted
            ]
            if filtered:
                log.info(
                    "Pod déduit via label %s=%s dans %s : %d pod(s)",
                    key,
                    wanted,
                    namespace,
                    len(filtered),
                )
                return filtered[0], f"déduit via label {key}={wanted}"

    # Aucun filtre applicable : on prend un pod non-Healthy du namespace,
    # car c'est très probablement lui le coupable.
    unhealthy = [
        p
        for p in candidates
        if p.status.phase not in ("Running", "Succeeded")
        or any(
            cs.state.waiting is not None
            and cs.state.waiting.reason in ("CrashLoopBackOff", "ImagePullBackOff")
            for cs in (p.status.container_statuses or [])
        )
    ]
    if unhealthy:
        log.info(
            "Pod déduit par état non-Healthy dans %s : %s",
            namespace,
            unhealthy[0].metadata.name,
        )
        return unhealthy[0], "déduit par état non-Healthy"

    if candidates:
        log.info(
            "Aucun pod ne correspond aux labels : on prend le premier pod de %s",
            namespace,
        )
        return candidates[0], "premier pod du namespace"

    return None, "aucun pod trouvé"


# ---------------------------------------------------------------------------
# Extraction de l'état du conteneur
# ---------------------------------------------------------------------------
def extract_last_termination(pod) -> Dict[str, Any]:
    """Extrait la raison du dernier arrêt du conteneur.

    C'est l'information LA PLUS VALABLE du diagnostic. Dans
    `pod.status.containerStatuses[].lastState.terminated`, on trouve :

      - `reason`    : "OOMKilled" (le kernel a tué pour dépassement mémoire)
                      ou "Error" (le process a lui-même quitté en erreur) ;
      - `exitCode`  : 137 = 128 + 9 (SIGKILL, donc OOM) ou 1 (erreur
                      applicative classique) ;
      - `signal`    : 9 pour SIGKILL.

    Connaître la règle "exit code 137 = SIGKILL" permet de déduire un OOM
    même quand la raison n'est pas disponible.
    """
    result: Dict[str, Any] = {}
    statuses = pod.status.container_statuses or []
    for cs in statuses:
        last = cs.last_state
        terminated = last.terminated if last else None
        if terminated:
            result = {
                "reason": terminated.reason,
                "exitCode": terminated.exit_code,
                "signal": terminated.signal,
                "message": terminated.message,
                "startedAt": str(terminated.started_at),
                "finishedAt": str(terminated.finished_at),
                "restartCount": cs.restart_count,
                "container": cs.name,
            }
            break
        # Aucune terminaison précédente, mais le conteneur est en attente :
        # c'est le cas typique du CrashLoopBackOff.
        if cs.state and cs.state.waiting:
            result = {
                "reason": cs.state.waiting.reason,
                "message": cs.state.waiting.message,
                "restartCount": cs.restart_count,
                "container": cs.name,
                "state": "en attente",
            }
            break
    return result


def summarize_pod_status(pod) -> Dict[str, Any]:
    """Résume l'état du pod en quelques champs lisibles.

    On ne passe pas l'objet Pod complet au LLM : il fait plusieurs
    kilo-octets de YAML, dont la majorité est du bruit (annotations
    Kubernetes, managedFields...). On extrait ce qui sert au diagnostic.
    """
    return {
        "name": pod.metadata.name,
        "phase": pod.status.phase,
        "podIP": pod.status.pod_ip,
        "node": pod.spec.node_name,
        "restartPolicy": pod.spec.restart_policy,
        "serviceAccount": pod.spec.service_account_name,
        "qosClass": pod.status.qos_class,
        "containers": [
            {
                "name": cs.name,
                "image": cs.image,
                "ready": cs.ready,
                "restartCount": cs.restart_count,
                "state": (
                    "running"
                    if cs.state and cs.state.running
                    else "waiting"
                    if cs.state and cs.state.waiting
                    else "terminated"
                    if cs.state and cs.state.terminated
                    else "inconnu"
                ),
                "stateReason": (
                    cs.state.waiting.reason
                    if cs.state and cs.state.waiting
                    else cs.state.terminated.reason
                    if cs.state and cs.state.terminated
                    else None
                ),
                "resources": {
                    "requests": _resource_dict(cs.resources.requests),
                    "limits": _resource_dict(cs.resources.limits),
                },
            }
            for cs in (pod.status.container_statuses or [])
        ],
    }


def _resource_dict(resources) -> Dict[str, str]:
    """Convertit un objet de ressources Kubernetes en dict simple."""
    if not resources:
        return {}
    out = {}
    for key in ("cpu", "memory"):
        value = getattr(resources, key, None)
        if value is not None:
            out[key] = str(value)
    return out


# ---------------------------------------------------------------------------
# Logs et événements
# ---------------------------------------------------------------------------
async def collect_logs(core_v1: aio_client.CoreV1Api, pod, evidence: Evidence) -> None:
    """Récupère les logs du conteneur.

    Deux lectures :

    - les logs **courants** (du conteneur vivant) ;
    - les logs **précédents** (`previous=True`), c'est-à-dire ceux du
      conteneur qui est MORT. C'est la source la plus importante : elle
      contient ce que l'application a écrit juste avant de mourir.

    Sur un OOMKill, les logs précédents sont **vides** : le noyau a tué le
    processus par SIGKILL, aucun handler n'a pu s'exécuter. Ce vide est
    lui-même une information (le signal est dans `lastState`, pas dans les
    logs).
    """
    name = pod.metadata.name
    namespace = pod.metadata.namespace

    try:
        logs = await core_v1.read_namespaced_pod_log(
            name=name, namespace=namespace, tail_lines=MAX_LOG_LINES
        )
        evidence.logs = logs.strip()
    except Exception as exc:  # noqa: BLE001
        evidence.errors.append(f"logs courants illisibles ({exc})")

    try:
        previous = await core_v1.read_namespaced_pod_log(
            name=name, namespace=namespace, previous=True, tail_lines=MAX_LOG_LINES
        )
        evidence.previous_logs = (previous or "").strip()
    except Exception as exc:  # noqa: BLE001
        # C'est normal si le conteneur n'a jamais redémarré.
        evidence.errors.append(f"logs précédents indisponibles ({exc})")


async def collect_events(
    core_v1: aio_client.CoreV1Api, namespace: str, evidence: Evidence
) -> None:
    """Récupère les événements récents du namespace.

    Les événements racontent l'HISTORIQUE du pod en clair :
    "Back-off restarting failed container", "OOMKilled", "Killing container".
    C'est la version narrative de ce que les métriques disent en chiffres.
    """
    try:
        events = await core_v1.list_namespaced_event(namespace=namespace)
    except Exception as exc:  # noqa: BLE001
        evidence.errors.append(f"événements illisibles ({exc})")
        return

    items = []
    for ev in events.items:
        items.append(
            {
                "type": ev.type,
                "reason": ev.reason,
                "message": (ev.message or "")[:400],
                "object": ev.involved_object.kind
                + "/"
                + (ev.involved_object.name or ""),
                "count": ev.count,
                "lastTimestamp": str(ev.last_timestamp or ev.event_time or ""),
            }
        )

    # On trie du plus récent au plus ancien. La comparaison de timestamps
    # ISO est fiable car le format est normalisé (UTC).
    items.sort(key=lambda e: e["lastTimestamp"], reverse=True)
    evidence.events = items[:MAX_EVENTS]


# ---------------------------------------------------------------------------
# Prometheus
# ---------------------------------------------------------------------------
# Les requêtes PromQL envoyées à Prometheus.
#
# Pourquoi des requêtes FIXES plutôt que de laisser le LLM écrire du PromQL ?
#   1. sécurité : un LLM ne doit jamais construire une requête arbitraire ;
#   2. reproductibilité : les mêmes preuves à chaque fois ;
#   3. le LLM ne connaît pas forcément les noms de métriques de NOTRE
#      cluster — il les hallucinerait.
#
# On note que `container_memory_working_set_bytes` vient du job
# `kubernetes-cadvisor`, ajouté à la config Prometheus.
PROMETHEUS_QUERIES = {
    "restarts": (
        'increase(kube_pod_container_status_restarts_total'
        '{namespace="%(namespace)s"}[15m])'
    ),
    "last_terminated_reason": (
        'kube_pod_container_status_last_terminated_reason'
        '{namespace="%(namespace)s"}'
    ),
    "last_terminated_exitcode": (
        'kube_pod_container_status_last_terminated_exitcode'
        '{namespace="%(namespace)s"}'
    ),
    "memory_limit_bytes": (
        'kube_pod_container_resource_limits'
        '{namespace="%(namespace)s",resource="memory"}'
    ),
    "up": 'up{namespace="%(namespace)s"}',
}


async def collect_metrics(
    http_client,
    prometheus_url: str,
    namespace: str,
    evidence: Evidence,
) -> None:
    """Interroge Prometheus et stocke les résultats.

    L'API Prometheus `/api/v1/query` prend une expression PromQL en
    paramètre `query` et renvoie :

        {"status": "success",
         "data": {"resultType": "vector",
                  "result": [{"metric": {...}, "value": [timestamp, "valeur"]}]}}

    On stocke le résultat brut (avec les labels) plutôt que de l'agréger :
    le LLM a besoin de voir le nom du pod, pas juste une valeur.
    """
    for name, template in PROMETHEUS_QUERIES.items():
        query = template % {"namespace": namespace}
        try:
            response = await http_client.get(
                f"{prometheus_url}/api/v1/query", params={"query": query}
            )
            response.raise_for_status()
            payload = response.json()
            data = payload.get("data", {})
            series = data.get("result", [])

            if not series:
                evidence.metrics[name] = {"query": query, "result": []}
                continue

            # On limite le nombre de séries pour ne pas noyer le prompt.
            trimmed = []
            for serie in series[:MAX_PROMETHEUS_SERIES]:
                labels = {
                    k: v
                    for k, v in (serie.get("metric") or {}).items()
                    if k in ("pod", "container", "job", "instance", "reason")
                }
                trimmed.append({"labels": labels, "value": serie.get("value", [None, None])[1]})

            evidence.metrics[name] = {"query": query, "result": trimmed}
        except Exception as exc:  # noqa: BLE001
            evidence.errors.append(f"métrique Prometheus '{name}' indisponible ({exc})")
