"""L'agent IA : service web qui reçoit les alertes et les diagnostique.

Flux général
------------

    Alertmanager ──POST /webhook──▶ cet agent
                                        │
                        ┌───────────────┴───────────────┐
                        │  réponse HTTP 200 immédiate  │  ← BackgroundTask
                        └───────────────┬───────────────┘
                                        │
                            ┌───────────▼───────────┐
                            │  pour chaque alerte   │
                            │    "firing"           │
                            └───────────┬───────────┘
                                        │
              ┌─────────────────────────┼─────────────────────────┐
              ▼                         ▼                         ▼
        API Kubernetes             Prometheus              Ollama
     (pod, logs, events)     (redémarrages, exit code)   /api/chat
                                        │
                                        ▼
                            cause racine en 3 phrases,
                                    loggée
"""

from __future__ import annotations

import json
import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any, Dict

import httpx
from fastapi import BackgroundTasks, FastAPI, Request, status
from fastapi.responses import JSONResponse

from .collector import (
    Evidence,
    build_k8s_client,
    collect_events,
    collect_logs,
    collect_metrics,
    extract_last_termination,
    find_target_pod,
    summarize_pod_status,
)
from .diagnosis import diagnose, format_verdict_for_prompt
from .llm import call_ollama, check_ollama_health
from .models import Alert, AlertGroup

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
# Tout est lu dans les variables d'environnement : c'est la convention
# Kubernetes (12-factor). Les valeurs par défaut correspondent au
# déploiement local de ce projet.

# L'adresse d'Ollama. Dans le cluster, on utilise le nom DNS interne du
# Service Kubernetes — pas une IP en dur, pour que le pod soit reconfigurable
# et que le Service fasse le load-balancing.
OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://ollama.aiops.svc.cluster.local:11434")

# L'adresse de Prometheus. Idem : nom DNS interne du Service.
PROMETHEUS_URL = os.environ.get("PROMETHEUS_URL", "http://prometheus.aiops.svc.cluster.local:9090")

# Le modèle utilisé. Doit correspondre à un modèle téléchargé dans Ollama.
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "qwen2.5-coder:14b")

# Le namespace Kubernetes que l'agent surveille.
WATCHED_NAMESPACE = os.environ.get("WATCHED_NAMESPACE", "aiops")

# Timeout de l'appel au LLM. Long : un modèle 3B sur CPU est lent.
LLM_TIMEOUT = float(os.environ.get("LLM_TIMEOUT", "300"))

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
# On configure le logging explicitement : par défaut, uvicorn ne montre que
# ses propres logs, pas ceux de l'application.
logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)-8s [%(name)s] %(message)s",
)
log = logging.getLogger("aiops-agent")

# ---------------------------------------------------------------------------
# Cycle de vie
# ---------------------------------------------------------------------------
# Les clients sont créés au démarrage (lifespan) et partagés : les recréer
# à chaque requête serait coûteux (nouvelle connexion TLS à chaque fois).
core_v1 = None
http_client: httpx.AsyncClient | None = None


@asynccontextmanager
async def lifespan(_app: FastAPI):
    """Initialise les clients au démarrage du pod, les ferme à l'arrêt.

    Le code avant `yield` est exécuté par uvicorn juste avant d'accepter
    les requêtes. C'est le bon moment pour créer la connexion HTTP et le
    client Kubernetes : s'ils échouent ici, le pod ne passe pas Ready et
    Kubernetes ne route pas de trafic vers lui — c'est le comportement
    souhaité. Le code après `yield` s'exécute à l'arrêt du pod.

    `lifespan` remplace `@app.on_event("startup"/"shutdown")`, déprécié
    par FastAPI.
    """
    global core_v1, http_client

    log.info("Démarrage de l'agent IA")
    log.info("  Ollama     : %s (modèle : %s)", OLLAMA_URL, OLLAMA_MODEL)
    log.info("  Prometheus : %s", PROMETHEUS_URL)
    log.info("  Namespace  : %s", WATCHED_NAMESPACE)

    core_v1 = await build_k8s_client()

    # timeout=None pour httpx : le timeout par défaut (5 s) est trop court
    # pour l'appel au LLM, qui peut prendre une minute. On gère donc les
    # timeouts explicitement, requête par requête.
    http_client = httpx.AsyncClient(timeout=None)

    # Vérification de la connectivité au démarrage : on veut savoir
    # immédiatement si Ollama est injoignable, plutôt que de le découvrir
    # au premier incident.
    health = await check_ollama_health(http_client, OLLAMA_URL)
    if health["ok"]:
        log.info("Ollama joignable. Modèles : %s", health["models"])
        if OLLAMA_MODEL not in health["models"]:
            log.warning(
                "Le modèle %s n'est PAS dans la liste des modèles "
                "disponibles : %s",
                OLLAMA_MODEL,
                health["models"],
            )
    else:
        log.warning("Ollama injoignable au démarrage : %s", health["error"])

    yield

    await http_client.aclose()
    if core_v1 is not None:
        await core_v1.api_client.close()
    log.info("Clients HTTP et Kubernetes fermés")


# ---------------------------------------------------------------------------
# Application
# ---------------------------------------------------------------------------
app = FastAPI(
    title="K8s AI Ops Agent",
    description=(
        "Agent d'AIOps qui reçoit les alertes Alertmanager, collecte les "
        "preuves dans Kubernetes et Prometheus, et demande une cause racine "
        "à un modèle de langage local."
    ),
    version="1.0.0",
    lifespan=lifespan,
)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
@app.get("/live", summary="Sonde de vivacité (processus en vie)")
async def live() -> JSONResponse:
    """Sonde de VIVACITÉ : le simple fait que le processus répond.

    Elle ne teste AUCUNE dépendance externe. Une sonde de vivacité doit
    vérifier uniquement ce qu'un redémarrage de pod pourrait réparer : ici,
    le processus Python lui-même.

    Pourquoi cette séparation ? Si la vivacité dépendait d'Ollama, une
    panne d'Ollama redémarrerait l'agent en boucle — sans rien réparer,
    puisque redémarrer l'agent ne fait pas revenir Ollama. Ce défaut a
    été observé pendant le développement : l'agent redémarrait toutes les
    30 secondes parce qu'Ollama n'était pas encore prêt.

    La règle à retenir : **la vivacité dépend de ce que je contrôle, la
    disponibilité dépend de ce que je consomme.**
    """
    return JSONResponse(
        status_code=status.HTTP_200_OK,
        content={"status": "alive"},
    )


@app.get("/health", summary="Sonde de disponibilité (dépendances opérationnelles)")
async def health() -> JSONResponse:
    """Sonde de DISPONIBILITÉ : l'agent peut-il faire son travail ?

    Elle vérifie les dépendances réelles de l'agent :

    - Ollama est-il joignable, et le modèle est-il présent ?
    - Prometheus est-il joignable ?
    - L'API Kubernetes répond-elle ?

    On distingue deux états :

    - **200** : l'agent fonctionne, il peut diagnostiquer ;
    - **503** : une dépendance est cassée. Le pod est retiré du Service par
      Kubernetes (il ne reçoit plus de trafic), mais il n'est PAS redémarré
      (c'est le rôle de la sonde /live).

    C'est ce qui distingue une panne « l'agent est mort » d'une panne
    « l'agent est vivant mais ne peut rien faire ».
    """
    checks: Dict[str, Any] = {}

    if http_client is None:
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content={"status": "starting", "checks": {}},
        )

    ollama = await check_ollama_health(http_client, OLLAMA_URL)
    checks["ollama"] = {
        "ok": ollama["ok"],
        "models": ollama["models"],
        "error": ollama["error"],
    }

    try:
        response = await http_client.get(
            f"{PROMETHEUS_URL}/-/ready", timeout=5.0
        )
        prom_ok = response.status_code == 200
        prom_error = None if prom_ok else f"HTTP {response.status_code}"
    except Exception as exc:  # noqa: BLE001
        prom_ok = False
        prom_error = str(exc)

    checks["prometheus"] = {"ok": prom_ok, "error": prom_error}

    # Le client Kubernetes : on vérifie qu'on peut lister les pods.
    try:
        if core_v1 is None:
            raise RuntimeError("client Kubernetes non initialisé")
        await core_v1.list_namespaced_pod(namespace=WATCHED_NAMESPACE, limit=1)
        checks["kubernetes"] = {"ok": True, "error": None}
    except Exception as exc:  # noqa: BLE001
        checks["kubernetes"] = {"ok": False, "error": str(exc)}

    model_present = OLLAMA_MODEL in (ollama["models"] or [])
    checks["model_present"] = model_present

    all_ok = all(c.get("ok") for c in checks.values() if isinstance(c, dict))
    healthy = all_ok and model_present

    return JSONResponse(
        status_code=(
            status.HTTP_200_OK if healthy else status.HTTP_503_SERVICE_UNAVAILABLE
        ),
        content={
            "status": "ok" if healthy else "degraded",
            "model": OLLAMA_MODEL,
            "checks": checks,
        },
    )


@app.post("/webhook", summary="Réception des alertes Alertmanager")
async def webhook(request: Request, background_tasks: BackgroundTasks) -> JSONResponse:
    """Reçoit une notification Alertmanager et lance le diagnostic.

    **Pourquoi la réponse est immédiate.** Alertmanager a un timeout de 10 s
    sur ses notifications. Si ce handler attendait l'inférence du LLM
    (30 à 90 s sur CPU), Alertmanager couperait la connexion, considérerait
    la livraison comme échouée, et **réessaierait** — relançant l'analyse
    en boucle. On répond donc `200` immédiatement et on fait le travail
    dans une tâche de fond.

    **Le code HTTP.** `200` signifie « j'ai bien reçu et pris en charge ».
    Ce n'est pas `202 Accepted` (qui serait plus exact sémantiquement) parce
    qu'Alertmanager ne fait la différence que entre succès et échec — et
    qu'un `202` risque d'être traité comme un échec par certaines versions.

    **Le corps de la requête.** On lit le JSON brut et on le valide
    nous-mêmes avec Pydantic, plutôt que de déclarer le paramètre comme
    modèle FastAPI. Raison : un receiver Alertmanager doit être tolérant.
    Si le payload est invalide, on renvoie `400` avec un message clair —
    Alertmanager le notifiera dans ses logs sans boucler.
    """
    try:
        raw_body = await request.body()
        payload = json.loads(raw_body)
    except json.JSONDecodeError as exc:
        log.warning("Payload Alertmanager illisible : %s", exc)
        return JSONResponse(
            status_code=status.HTTP_400_BAD_REQUEST,
            content={"error": f"JSON invalide : {exc}"},
        )

    try:
        group = AlertGroup.model_validate(payload)
    except Exception as exc:  # noqa: BLE001
        log.warning("Payload Alertmanager ne respecte pas le format v4 : %s", exc)
        return JSONResponse(
            status_code=status.HTTP_400_BAD_REQUEST,
            content={"error": f"Payload incompatible avec le format v4 : {exc}"},
        )

    firing = group.firing_alerts

    log.info(
        "Webhook reçu : version=%s receiver=%s alerts=%d (firing=%d)",
        group.version,
        group.receiver,
        len(group.alerts),
        len(firing),
    )

    # On logge le payload brut : c'est la trace qui permet de vérifier
    # "la chaîne fonctionne" d'un coup d'œil.
    log.info("Payload reçu : %s", json.dumps(payload, ensure_ascii=False)[:2000])

    if not firing:
        log.info("Aucune alerte 'firing' dans le payload : rien à diagnostiquer.")
        return JSONResponse(
            status_code=status.HTTP_200_OK,
            content={"status": "accepted", "analyzed": 0, "reason": "no firing alerts"},
        )

    # On planifie le traitement en tâche de fond. FastAPI l'exécutera APRÈS
    # l'envoi de la réponse HTTP.
    background_tasks.add_task(process_alerts, group)

    return JSONResponse(
        status_code=status.HTTP_200_OK,
        content={
            "status": "accepted",
            "analyzed": len(firing),
            "alertnames": [a.alertname for a in firing],
        },
    )


# ---------------------------------------------------------------------------
# Traitement en tâche de fond
# ---------------------------------------------------------------------------
async def process_alerts(group: AlertGroup) -> None:
    """Traite chaque alerte « firing » : collecte des preuves puis diagnostic.

    Cette fonction est exécutée APRÈS l'envoi de la réponse HTTP. Deux
    conséquences à garder en tête :

    1. **Une exception ici n'affecte pas Alertmanager** (il a déjà reçu son
       200). D'où le try/except global : on loggue, on ne laisse pas
       remonter une trace qui masquerait le contexte.

    2. **Si le pod est redémarré pendant le traitement, la tâche est
       perdue.** Acceptable pour une démonstration ; en production, il
       faudrait une file de travail (Redis, RabbitMQ, ou le ``Queue`` de
       Kubernetes). C'est une limite connue, documentée dans le README.
    """
    try:
        for alert in group.firing_alerts:
            await analyze_one_alert(alert)
    except Exception as exc:  # noqa: BLE001
        log.exception("Erreur globale pendant le traitement des alertes : %s", exc)


async def analyze_one_alert(alert: Alert) -> None:
    """Diagnostique une alerte et journalise le résultat."""
    started = datetime.now(timezone.utc)

    log.info("=" * 70)
    log.info(
        "Analyse de l'alerte %s (sévérité=%s, namespace=%s)",
        alert.alertname,
        alert.severity,
        alert.namespace,
    )

    # Le namespace : on prend celui de l'alerte. S'il est absent — cas
    # réel pour une alerte comme « DemoAppDown », dont la métrique `up`
    # ne porte pas de label namespace — on retombe sur le namespace que
    # l'agent surveille. On ne devine PAS « default » : ce serait analyser
    # l'incident au mauvais endroit, et le RBAC (limité au namespace
    # surveillé) rejetterait la lecture avec un 403.
    namespace = alert.namespace or WATCHED_NAMESPACE
    if alert.namespace is None:
        log.info(
            "Alerte %s sans label namespace : analyse dans le namespace "
            "surveillé (%s)",
            alert.alertname,
            namespace,
        )

    evidence = Evidence(
        alertname=alert.alertname,
        severity=alert.severity,
        namespace=namespace,
        pod_name=alert.pod_name,
        summary=alert.annotations.get("summary", ""),
    )

    # --- 1. Identifier et décrire le pod ------------------------------
    pod = None
    if core_v1 is not None:
        pod, method = await find_target_pod(core_v1, alert, namespace)
        evidence.pod_name = pod.metadata.name if pod else None
        if pod:
            log.info("Pod cible : %s (trouvé via %s)", pod.metadata.name, method)
            evidence.pod_status = summarize_pod_status(pod)
            evidence.last_termination = extract_last_termination(pod)
            await collect_logs(core_v1, pod, evidence)
        else:
            evidence.errors.append("aucun pod identifié")
            log.warning("Aucun pod identifié pour l'alerte %s", alert.alertname)
    else:
        evidence.errors.append("client Kubernetes non initialisé")

    # --- 2. Événements du namespace -----------------------------------
    if core_v1 is not None:
        await collect_events(core_v1, namespace, evidence)

    # --- 3. Métriques Prometheus --------------------------------------
    if http_client is not None:
        await collect_metrics(http_client, PROMETHEUS_URL, namespace, evidence)

    # --- 4. Verdict déterministe -------------------------------------
    # Le code tranche la cause à partir des champs structurés (raison
    # d'arrêt, code de sortie). Le LLM, lui, se contente de rédiger.
    #
    # C'est ce qui empêche un modèle 3B de recomposer une histoire plausible
    # mais fausse à partir de données secondaires (comme la limite de
    # mémoire, qui est une constante de configuration et pas un
    # symptôme). Détail dans le module `diagnosis`.
    verdict = diagnose(evidence.last_termination, evidence.pod_status)
    verdict_block = format_verdict_for_prompt(verdict)
    log.info("Verdict déterministe :\n%s", verdict_block)

    # --- 5. Diagnostic rédigé par le LLM ------------------------------
    context = evidence.to_prompt_context()
    log.info(
        "Preuves collectées (%d caractères) :\n%s",
        len(context),
        context,
    )

    result = {"ok": False, "analysis": None, "error": "LLM non appelé"}

    if http_client is not None:
        result = await call_ollama(
            http_client,
            OLLAMA_URL,
            OLLAMA_MODEL,
            context,
            verdict_block,
            timeout=LLM_TIMEOUT,
        )

    elapsed = (datetime.now(timezone.utc) - started).total_seconds()

    # --- 6. Journalisation du résultat --------------------------------
    log.info("-" * 70)
    if result.get("ok"):
        log.info(
            "DIAGNOSTIC de %s (%.1fs, inférence LLM : %ss) :\n%s",
            alert.alertname,
            elapsed,
            result.get("duration_s"),
            result.get("analysis"),
        )
    else:
        log.error(
            "Diagnostic impossible pour %s (%.1fs) : %s",
            alert.alertname,
            elapsed,
            result.get("error"),
        )

    # Trace structurée : une ligne JSON par incident, facile à extraire
    # pour un tableau de bord ou pour les tests.
    log.info(
        "ANALYSIS_RESULT %s",
        json.dumps(
            {
                "timestamp": started.isoformat(),
                "alertname": alert.alertname,
                "severity": alert.severity,
                "namespace": namespace,
                "pod": evidence.pod_name,
                "last_termination": evidence.last_termination,
                "verdict": (
                    {
                        "code": verdict.code,
                        "cause": verdict.cause,
                        "evidence": verdict.evidence,
                        "confidence": verdict.confidence,
                        "recommended_action": verdict.recommended_action,
                    }
                    if verdict
                    else None
                ),
                "analysis": result.get("analysis"),
                "llm_ok": result.get("ok"),
                "llm_error": result.get("error"),
                "duration_s": round(elapsed, 2),
                "collection_errors": evidence.errors,
            },
            ensure_ascii=False,
        ),
    )
