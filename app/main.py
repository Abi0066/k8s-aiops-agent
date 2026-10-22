"""
Application de démonstration — cible des incidents simulés.

Ce service n'a aucune logique métier : son rôle est d'être une cible facile à
casser, et d'exposer les signaux qu'un système de monitoring doit observer.

Endpoints
---------
GET /health   Sonde de vivacité. Répond 200 tant que le process tourne.
              Kubernetes l'utilise dans le readinessProbe / livenessProbe.
GET /crash    Simule un crash applicatif : le process se termine avec le
              code 1. Kubernetes redémarre alors le conteneur, ce qui produit
              l'état CrashLoopBackOff. C'est le scénario d'incident
              "l'application plante en boucle".
GET /oom      Simule un dépassement de mémoire : on alloue plus que la
              memory limit du conteneur, le kernel OOM-kill le process. Le pod
              se termine alors avec la raison OOMKilled. C'est le scénario
              d'incident que l'agent devra savoir reconnaître et corriger
              (étape 4 : augmentation de la memory limit).
GET /metrics  Endpoint Prometheus. Expose les compteurs et histogrammes
              définis ci-dessous.
"""

import logging
import os
import time

from flask import Flask, Response, jsonify, request
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Histogram, generate_latest

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# Logging vers stdout : dans un conteneur, stdout est capturé par le
# runtime et visible via `kubectl logs`. Écrire sur un fichier local
# ne serait pas collecté.
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
logger = logging.getLogger("demo-app")

app = Flask(__name__)

# ---------------------------------------------------------------------------
# Métriques Prometheus
# ---------------------------------------------------------------------------

# Counter : compteur monotone qui ne fait qu'augmenter. On l'utilise pour
# compter des événements (ici : nombre de requêtes par endpoint).
# Le label "endpoint" permet de.slice les métriques par route.
REQUESTS = Counter(
    "demo_app_requests_total",
    "Nombre total de requêtes reçues par l'application de démonstration",
    ["endpoint"],
)

# Histogram : mesure la distribution d'une valeur (ici : la latence).
# Prometheus stocke la valeur dans plusieurs "buckets" (0.005s, 0.01s, ...),
# ce qui permet de calculer des percentiles (p95, p99) ensuite.
LATENCY = Histogram(
    "demo_app_request_duration_seconds",
    "Latence des requêtes traitées par l'application de démonstration",
    ["endpoint"],
)

# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@app.route("/health")
def health():
    """Sonde de vivacité.

    Kubernetes appelle ce endpoint via les probes :
    - readinessProbe : le pod peut-il recevoir du trafic ?
    - livenessProbe : le pod doit-il être redémarré ?

    On reste volontairement simple : c'est un always-ok tant que le process
    répond. Un vrai service vérifierait ses dépendances ici.
    """
    REQUESTS.labels(endpoint="health").inc()
    return jsonify(status="ok", service="demo-app"), 200


@app.route("/crash")
def crash():
    """Provoque un crash applicatif volontaire.

    `os._exit(1)` termine immédiatement le process, sans exécuter les
    `finally` Python ni les hooks de graceful shutdown (atexit, signal
    handlers). C'est volontairement le comportement le plus brutal : c'est ce
    que produit un vrai segfault ou un `kill -9`, et c'est ce que l'agent
    devra diagnostiquer.

    POURQUOI CELA PRODUIT UN REDEMARRAGE DU POD
    --------------------------------------------
    Le point clé, c'est que le serveur HTTP est « waitress », qui est
    mono-processus : le process Python qui exécute ce code EST le PID 1 du
    conteneur. Le noyau Linux et le kubelet surveillent ce PID 1 ; quand il
    meurt, le conteneur s'arrête et Kubernetes le redémarre (CrashLoopBackOff).

    Avec gunicorn, ce serait différent : gunicorn est un superviseur. Le
    PID 1 est le master, et les workers sont des enfants. `os._exit(1)` ne
    tuerait QUE le worker qui a servi la requête ; le master constaterait sa
    mort et en démarrerait un nouveau. Le conteneur ne mourrait jamais, et le
    pod ne redémarrerait pas — l'incident serait invisible.

    C'est pour cette raison que l'image utilise waitress et non gunicorn :
    ce n'est pas un choix de performance, c'est un choix de scénario.

    Alternatives écartées :
    - `sys.exit(1)` : lève SystemExit, le process se termine proprement.
      Kubernetes verrait quand même un exit code 1, mais la distinction
      "arrêt propre" vs "crash" se perd dans les logs.
    - `raise Exception` : le serveur Flask remonte l'erreur et le process
      meurt. Plus lent et moins déterministe.
    - `os.kill(1, signal.SIGKILL)` : NE MARCHE PAS. Le noyau ignore les
      signaux sans handler envoyés au PID 1 d'un namespace, et SIGKILL ne
      peut pas être intercepté. Le signal est donc silencieusement perdu.
    """
    REQUESTS.labels(endpoint="crash").inc()
    logger.warning("Crash simulé sur /crash — terminaison du process (exit 1)")
    # sys.stdout.flush() pour s'assurer que le log est bien émis avant que
    # le process ne meure.
    os._exit(1)


@app.route("/oom")
def oom():
    """Provoque un dépassement mémoire volontaire.

    On alloue des blocs de mémoire jusqu'à dépasser la memory limit
    configurée sur le conteneur. Le noyau (kernel Linux) réagit en
    OOM-killing le process : le conteneur se termine alors avec la raison
    `OOMKilled`, visible dans `kubectl describe pod`.

    Point important pour la suite du projet : c'est le kernel qui tue le
    process, pas Python. Le signal SIGKILL n'est pas rattrapable — aucun
    handler Python ne peut s'exécuter, donc le message "Mémoire exhausted"
    n'apparaît PAS dans les logs de l'application. L'agent devra déduire la
    cause depuis les événements Kubernetes et l'état du pod, pas depuis les
    logs de la seule application. C'est précisément ce qui rend le
    scénario intéressant.
    """
    REQUESTS.labels(endpoint="oom").inc()
    logger.warning("OOM simulé sur /oom — allocation mémoire au-delà de la limit")

    # On alloue par blocs de 8 Mo. La boucle continue tant qu'on n'a pas
    # dépassé la limite : le noyau va tuer le process avant la fin.
    blocks = []
    try:
        while True:
            blocks.append(bytearray(8 * 1024 * 1024))
            time.sleep(0.05)
    except MemoryError:
        # Cas où l'allocation échoue avant que le kernel ne tue le process
        # (cgroup avec limite stricte). On rend l'échec explicite.
        logger.error("MemoryError Python — la limite mémoire a été atteinte")
        return jsonify(status="oom-simulated", detail="MemoryError"), 500


@app.route("/metrics")
def metrics():
    """Expose les métriques au format texte Prometheus.

    Prometheus scrape cette URL périodiquement (configuré dans
    monitoring/prometheus/config.yaml).
    """
    return Response(generate_latest(), mimetype=CONTENT_TYPE_LATEST)


# ---------------------------------------------------------------------------
# Middleware : mesure de la latence
# ---------------------------------------------------------------------------


@app.before_request
def _before():
    request._start_time = time.perf_counter()


@app.after_request
def _after(response):
    """Enregistre la latence de chaque requête.

    Ce middleware illustre le pattern "observabilité transverse" : on mesure
    au niveau de l'application, là où l'on connaît le contexte (quelle route),
    plutôt qu'à l'extérieur où l'on ne verrait qu'un chemin d'URL.
    """
    endpoint = request.endpoint or "unknown"
    LATENCY.labels(endpoint=endpoint).observe(time.perf_counter() - request._start_time)
    return response


if __name__ == "__main__":
    # Point d'entrée : waitress sert l'application WSGI.
    #
    # Pourquoi waitress et pas gunicorn ?
    #
    # gunicorn est un superviseur : il lance un master (PID 1 dans le
    # conteneur) et N workers. Un worker qui meurt est relayé par le master.
    # Notre endpoint /crash appelle os._exit(1) : avec gunicorn, seul le
    # worker qui a servi la requête est tué, le master en relance un autre, et le conteneur
    # survit — donc aucun redémarrage de pod, aucun incident observable.
    #
    # waitress est mono-processus : le process Python EST le PID 1. os._exit(1)
    # tue donc le PID 1, le conteneur s'arrête, et Kubernetes redémarre le pod.
    #
    # Vérifié : avec waitress, le conteneur sort avec le code 1, ce que
    # Kubernetes affiche comme "Reason: Error, Exit Code: 1".
    from waitress import serve

    serve(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8000")))
