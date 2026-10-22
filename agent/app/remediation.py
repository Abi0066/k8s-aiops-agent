"""Auto-guérison : l'action corrective, décidée par le code.

Étape 4 du projet. Jusqu'ici l'agent diagnostiquait ; il peut maintenant
corriger lui-même UN type d'incident, celui qu'il sait trancher avec
certitude : le conteneur tué parce qu'il a dépassé sa limite de mémoire
(verdict `OOMKilled`, confiance `high`). L'action : relever la limite de
mémoire du conteneur dans le Deployment qui a créé le pod.

Pourquoi une seule action, et celle-là
--------------------------------------
Agir sur le cluster est autrement plus risqué que d'écrire un diagnostic.
Un OOMKilled est le cas le plus net : Kubernetes écrit lui-même
`reason: OOMKilled`, il n'y a rien à interpréter. Un exit code 137 seul
(SIGKILL sans confirmation, confiance `medium`) ne suffit PAS : on ne
relève pas une limite pour un incident qui n'est peut-être pas un manque
de mémoire.

Relever la limite ne soigne qu'une limite SOUS-DIMENSIONNÉE. Face à une
fuite mémoire, l'application finira par dépasser la nouvelle limite aussi :
c'est pour cela qu'il y a un plafond et un délai entre deux actions. La
correction durable reste humaine (reporter la valeur dans le dépôt, ou
corriger la fuite).

Les garde-fous
--------------
1. **Désactivée par défaut** (`AUTOHEAL_ENABLED`) : il faut le demander
   explicitement.
2. **Dry-run par défaut** (`AUTOHEAL_DRY_RUN`) : le patch est envoyé avec
   `dryRun=All`. Le serveur d'API le traite en entier (droits RBAC,
   validation, admission) mais n'enregistre rien. On sait donc que l'action
   AURAIT marché, sans l'avoir faite.
3. **Un plafond** (`AUTOHEAL_MEMORY_MAX`) : la limite ne monte jamais
   au-delà.
4. **Une action par Deployment et par fenêtre** (`AUTOHEAL_COOLDOWN_S`).
   L'instant de la dernière action est écrit dans une annotation du
   Deployment, pas en mémoire : il survit au redémarrage de l'agent et se
   lit avec `kubectl describe`.
5. **Le LLM ne décide rien.** Ce module ne lit que le verdict déterministe
   et l'état du cluster. Le LLM est informé APRÈS coup de ce qui a été fait,
   pour l'expliquer.
6. **Tout est tracé** : chaque décision, y compris « ne rien faire », est
   renvoyée sous forme de dictionnaire et journalisée dans ANALYSIS_RESULT.
"""

from __future__ import annotations

import logging
import math
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Dict, Mapping, Optional

from kubernetes.aio import client as aio_client
from kubernetes.utils.quantity import parse_quantity

from .collector import API_TIMEOUT_S
from .diagnosis import Verdict

log = logging.getLogger("aiops-agent.remediation")

ACTION_TYPE = "increase_memory_limit"

# Annotations écrites sur le Deployment à chaque action réelle. La première
# sert au délai entre deux actions ; la seconde dit à un humain ce qui a
# changé, sans avoir à fouiller les logs de l'agent.
ANNOTATION_AT = "aiops-agent/autoheal-at"
ANNOTATION_CHANGE = "aiops-agent/autoheal-memory"

# Le patch est de type « strategic merge » : le serveur fusionne la liste
# `containers` par NOM de conteneur. Avec un « merge patch » JSON classique,
# la liste envoyée REMPLACERAIT la liste existante : le Deployment perdrait
# l'image, les sondes, et tous les autres conteneurs.
STRATEGIC_MERGE = "application/strategic-merge-patch+json"

MI = 1024**2

_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off"}


@dataclass(frozen=True)
class AutohealConfig:
    """Les réglages de l'auto-guérison. Les valeurs par défaut sont sûres."""

    enabled: bool = False
    dry_run: bool = True
    memory_factor: float = 1.5
    memory_max: str = "1Gi"
    cooldown_s: float = 3600.0

    def __post_init__(self) -> None:
        # Une configuration absurde doit faire échouer le démarrage, pas
        # produire une action absurde au premier incident.
        if not self.memory_factor > 1:
            raise ValueError(
                f"AUTOHEAL_MEMORY_FACTOR doit être > 1 (reçu {self.memory_factor}) : "
                "un facteur <= 1 ne relèverait pas la limite"
            )
        if parse_quantity(self.memory_max) < MI:
            raise ValueError(f"AUTOHEAL_MEMORY_MAX trop petit : {self.memory_max}")
        if self.cooldown_s < 0:
            raise ValueError(f"AUTOHEAL_COOLDOWN_S doit être >= 0 (reçu {self.cooldown_s})")

    @classmethod
    def from_env(cls, env: Optional[Mapping[str, str]] = None) -> "AutohealConfig":
        """Lit la configuration, en restant du côté sûr en cas de doute.

        L'action n'est activée que par une valeur explicitement vraie, et le
        dry-run n'est coupé que par une valeur explicitement fausse : une
        faute de frappe (« flase ») laisse le système dans son état sûr.
        """
        env = os.environ if env is None else env
        enabled = env.get("AUTOHEAL_ENABLED", "").strip().lower() in _TRUE
        dry_run = env.get("AUTOHEAL_DRY_RUN", "").strip().lower() not in _FALSE
        return cls(
            enabled=enabled,
            dry_run=dry_run,
            memory_factor=float(env.get("AUTOHEAL_MEMORY_FACTOR", cls.memory_factor)),
            memory_max=env.get("AUTOHEAL_MEMORY_MAX", cls.memory_max),
            cooldown_s=float(env.get("AUTOHEAL_COOLDOWN_S", cls.cooldown_s)),
        )

    def describe(self) -> str:
        if not self.enabled:
            return "désactivée"
        mode = "dry-run" if self.dry_run else "RÉELLE"
        return (
            f"activée ({mode}), limite mémoire x{self.memory_factor}, "
            f"plafond {self.memory_max}, une action par Deployment "
            f"toutes les {self.cooldown_s:.0f} s"
        )


def build_apps_client(core_v1) -> aio_client.AppsV1Api:
    """Client des Deployments, sur la même connexion que CoreV1Api.

    Partager l'ApiClient évite une seconde connexion TLS, et fermer celui
    de `core_v1` à l'arrêt ferme les deux.
    """
    return aio_client.AppsV1Api(core_v1.api_client)


def is_eligible(verdict: Optional[Verdict]) -> bool:
    """Seul un OOMKilled certain déclenche une action."""
    return verdict is not None and verdict.code == "OOMKilled" and verdict.confidence == "high"


def owner_deployment_name(pod) -> Optional[str]:
    """Nom du Deployment qui a créé le pod, ou None.

    Un pod de Deployment appartient à un ReplicaSet nommé
    `<deployment>-<pod-template-hash>`, et porte le label
    `pod-template-hash`. On en déduit le nom du Deployment SANS lire le
    ReplicaSet : le RBAC de l'agent reste limité aux Deployments. La
    déduction est ensuite vérifiée sur le Deployment lui-même (son
    sélecteur doit correspondre aux labels du pod).
    """
    owners = pod.metadata.owner_references or []
    replica_set = next((o for o in owners if o.kind == "ReplicaSet" and o.controller), None)
    template_hash = (pod.metadata.labels or {}).get("pod-template-hash")
    if replica_set is None or not template_hash:
        return None
    suffix = f"-{template_hash}"
    if not replica_set.name.endswith(suffix):
        return None
    return replica_set.name[: -len(suffix)]


def compute_new_limit(current: str, factor: float, maximum: str) -> Optional[str]:
    """Nouvelle limite : `current` x `factor`, plafonnée, en Mio entiers.

    Renvoie None si la limite ne peut plus monter (plafond atteint). Le
    calcul se fait en octets (Decimal, sans erreur d'arrondi), puis la
    valeur est arrondie au Mio supérieur pour rester lisible : 192Mi
    plutôt que 201326592.
    """
    current_b = parse_quantity(current)
    cap_b = parse_quantity(maximum)
    target_b = min(current_b * Decimal(str(factor)), cap_b)
    mib = math.ceil(target_b / MI)
    if mib * MI > cap_b:
        # Le plafond n'est pas un nombre entier de Mio (ex. « 500M ») :
        # on reste en dessous.
        mib = math.floor(cap_b / MI)
    if mib * MI <= current_b:
        return None
    return f"{mib}Mi"


def _parse_time(value: Optional[str]) -> Optional[datetime]:
    try:
        return datetime.fromisoformat(value) if value else None
    except ValueError:
        return None


def _api_error(exc: Exception) -> str:
    # Une ApiException porte le code HTTP : 403 = RBAC, 409 = le Deployment
    # a changé entre la lecture et le patch. Son str() contient aussi les
    # en-têtes HTTP complets, inutiles dans une ligne de log.
    status = getattr(exc, "status", None)
    if status:
        return f"HTTP {status} {getattr(exc, 'reason', '')}".strip()
    return f"{type(exc).__name__}: {exc}"


async def remediate(
    apps_v1,
    pod,
    verdict: Optional[Verdict],
    container: Optional[str],
    config: AutohealConfig,
    now: Optional[datetime] = None,
) -> Optional[Dict[str, Any]]:
    """Décide et applique l'action corrective pour un incident.

    Renvoie None si le verdict n'appelle aucune action. Sinon, renvoie la
    trace de la décision : `status` vaut `applied`, `dry-run`, `disabled`,
    `skipped` (un garde-fou a bloqué) ou `failed` (l'API a refusé). Aucune
    exception ne remonte : une action ratée ne doit pas empêcher le
    diagnostic d'être rédigé et journalisé.
    """
    if not is_eligible(verdict):
        return None

    now = now or datetime.now(timezone.utc)
    action: Dict[str, Any] = {
        "type": ACTION_TYPE,
        "status": None,
        "reason": None,
        "deployment": None,
        "container": container,
        "from": None,
        "to": None,
    }

    def done(status: str, reason: Optional[str] = None) -> Dict[str, Any]:
        action["status"] = status
        action["reason"] = reason
        return action

    if not config.enabled:
        return done("disabled", "auto-guérison désactivée (AUTOHEAL_ENABLED)")
    if apps_v1 is None:
        return done("failed", "client Kubernetes non initialisé")

    name = owner_deployment_name(pod)
    if name is None:
        return done("skipped", "le pod n'a pas été créé par un Deployment")
    action["deployment"] = name
    namespace = pod.metadata.namespace

    try:
        deployment = await apps_v1.read_namespaced_deployment(
            name=name, namespace=namespace, _request_timeout=API_TIMEOUT_S
        )
    except Exception as exc:  # noqa: BLE001
        return done("failed", f"lecture du Deployment impossible : {_api_error(exc)}")

    # Le nom a été déduit d'une convention : on vérifie que ce Deployment
    # sélectionne bien ce pod avant de le modifier.
    selector = deployment.spec.selector.match_labels or {}
    pod_labels = pod.metadata.labels or {}
    if not selector or any(pod_labels.get(k) != v for k, v in selector.items()):
        return done("skipped", f"le Deployment {name} ne sélectionne pas ce pod")

    annotations = deployment.metadata.annotations or {}
    last_action = _parse_time(annotations.get(ANNOTATION_AT))
    if last_action and (now - last_action).total_seconds() < config.cooldown_s:
        return done(
            "skipped",
            f"cooldown : dernière action le {last_action.isoformat()}, "
            f"une seule action toutes les {config.cooldown_s:.0f} s",
        )

    spec = next(
        (c for c in deployment.spec.template.spec.containers if c.name == container), None
    )
    if spec is None:
        return done("skipped", f"conteneur {container} absent du Deployment {name}")
    current = ((spec.resources and spec.resources.limits) or {}).get("memory")
    if not current:
        return done("skipped", f"le conteneur {container} n'a pas de limite de mémoire")
    action["from"] = current

    new_limit = compute_new_limit(current, config.memory_factor, config.memory_max)
    if new_limit is None:
        return done("skipped", f"plafond atteint ({current} >= {config.memory_max})")
    action["to"] = new_limit

    body = {
        "metadata": {
            # Verrou optimiste : si le Deployment a changé depuis la lecture
            # (autre action, déploiement en cours), le serveur répond 409 au
            # lieu d'écraser la modification de quelqu'un d'autre. C'est
            # aussi ce qui empêche deux analyses simultanées d'agir deux fois.
            "resourceVersion": deployment.metadata.resource_version,
            "annotations": {
                ANNOTATION_AT: now.isoformat(),
                ANNOTATION_CHANGE: f"{container}: {current} -> {new_limit}",
            },
        },
        "spec": {
            "template": {
                "spec": {
                    "containers": [
                        {"name": container, "resources": {"limits": {"memory": new_limit}}}
                    ]
                }
            }
        },
    }
    try:
        await apps_v1.patch_namespaced_deployment(
            name=name,
            namespace=namespace,
            body=body,
            dry_run="All" if config.dry_run else None,
            # Visible dans les managedFields : on sait qui a changé la valeur.
            field_manager="aiops-agent",
            _content_type=STRATEGIC_MERGE,
            _request_timeout=API_TIMEOUT_S,
        )
    except Exception as exc:  # noqa: BLE001
        return done("failed", f"patch refusé : {_api_error(exc)}")

    if config.dry_run:
        return done("dry-run", "accepté par le serveur d'API, rien n'a été modifié")
    return done("applied")


def format_remediation_for_prompt(action: Optional[Dict[str, Any]]) -> str:
    """Informe le LLM de la décision du code, pour qu'il l'explique.

    Le texte est ajouté APRÈS le verdict : le LLM n'a pas de choix à faire,
    seulement une action à décrire. Sans la consigne explicite, un modèle
    local tend à proposer sa propre correction à côté de celle appliquée.
    """
    if action is None:
        return ""
    change = (
        f"limite memoire du conteneur {action.get('container')} du Deployment "
        f"{action.get('deployment')} : {action.get('from')} -> {action.get('to')}"
    )
    if action["status"] == "applied":
        return (
            f"ACTION AUTOMATIQUE APPLIQUEE (decision du code, pas la tienne) : {change}.\n"
            "Ta phrase « action » decrit cette action deja faite. N'en propose "
            "pas d'autre."
        )
    if action["status"] == "dry-run":
        return (
            f"ACTION AUTOMATIQUE SIMULEE (dry-run, rien n'a ete modifie) : {change}.\n"
            "Ta phrase « action » dit que cette action a ete simulee et attend "
            "une validation humaine. N'en propose pas d'autre."
        )
    return (
        f"AUCUNE ACTION AUTOMATIQUE : {action.get('reason')}.\n"
        "Recommande l'action ci-dessus a un humain, sans pretendre qu'elle a "
        "ete faite."
    )
