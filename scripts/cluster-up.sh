#!/usr/bin/env bash
#
# cluster-up.sh — Crée le cluster kind et y déploie l'étape 1 du projet.
#
# Le script est idempotent : on peut le relancer sans risque, meme si le
# cluster existe deja. Il se contente de re-appliquer les manifests.

set -euo pipefail

CLUSTER_NAME="k8s-aiops"
NAMESPACE="aiops"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

log()  { printf '\033[0;36m[cluster-up]\033[0m %s\n' "$*"; }
warn() { printf '\033[0;33m[cluster-up]\033[0m %s\n' "$*"; }
die()  { printf '\033[0;31m[cluster-up]\033[0m %s\n' "$*" >&2; exit 1; }

# ---------------------------------------------------------------------------
# 1. Runtime de conteneurs
# ---------------------------------------------------------------------------
if command -v podman >/dev/null 2>&1; then
    export KIND_EXPERIMENTAL_PROVIDER=podman
    log "Runtime : podman."
elif command -v docker >/dev/null 2>&1; then
    log "Runtime : docker."
else
    die "Aucun runtime de conteneurs (podman ou docker) trouvé."
fi

# kind load docker-image appelle « docker » en dur dans son code source.
# Sur une machine où seul podman est installé, on installe un shim qui
# délègue les commandes docker à podman. Voir scripts/docker-podman-shim.sh.
if command -v podman >/dev/null 2>&1 && ! command -v docker >/dev/null 2>&1; then
    SHIM_DIR="${HOME}/.local/bin"
    mkdir -p "$SHIM_DIR"
    cp "$REPO_ROOT/scripts/docker-podman-shim.sh" "$SHIM_DIR/docker"
    chmod +x "$SHIM_DIR/docker"
    log "Shim docker->podman installé dans $SHIM_DIR/docker"
fi

# ---------------------------------------------------------------------------
# 2. Création du cluster
# ---------------------------------------------------------------------------
if kind get clusters 2>/dev/null | grep -qx "$CLUSTER_NAME"; then
    log "Le cluster '$CLUSTER_NAME' existe déjà."
else
    log "Création du cluster '$CLUSTER_NAME'..."
    kind create cluster --name "$CLUSTER_NAME" --config kind-config.yaml --wait 120s
fi

# Le contexte kubectl doit pointer sur notre cluster.
kubectl config use-context "kind-${CLUSTER_NAME}" >/dev/null

# ---------------------------------------------------------------------------
# 3. Namespace
# ---------------------------------------------------------------------------
log "Application du namespace '$NAMESPACE'..."
kubectl apply -f manifests/namespace.yaml

# ---------------------------------------------------------------------------
# 4. ConfigMaps générés depuis les fichiers sources
# ---------------------------------------------------------------------------
# La configuration Prometheus vit dans monitoring/prometheus/config.yaml pour
# rester lisible et commentée ; on la monte dans un ConfigMap au moment du
# déploiement. Même principe pour les règles d'alerte.
log "Création des ConfigMaps Prometheus (config + règles)..."
kubectl create configmap prometheus-config \
    --namespace="$NAMESPACE" \
    --from-file=prometheus.yml=monitoring/prometheus/config.yaml \
    --dry-run=client -o yaml | kubectl apply -f -

kubectl create configmap prometheus-rules \
    --namespace="$NAMESPACE" \
    --from-file=alerts.yaml=monitoring/prometheus/rules/alerts.yaml \
    --dry-run=client -o yaml | kubectl apply -f -

# La configuration d'Alertmanager (route + receivers) suit la même logique.
log "Création du ConfigMap Alertmanager..."
kubectl create configmap alertmanager-config \
    --namespace="$NAMESPACE" \
    --from-file=alertmanager.yml=monitoring/alertmanager/alertmanager.yaml \
    --dry-run=client -o yaml | kubectl apply -f -

# Les dashboards Grafana sont provisionnés depuis deux fichiers distincts :
# le provider (où chercher) et le dashboard lui-même (au format JSON).
log "Création du ConfigMap des dashboards Grafana..."
kubectl create configmap grafana-dashboards \
    --namespace="$NAMESPACE" \
    --from-file=dashboard.yaml=monitoring/grafana/dashboards/provider.yaml \
    --from-file=demo-app.json=monitoring/grafana/dashboards/demo-app.json \
    --dry-run=client -o yaml | kubectl apply -f -

# ---------------------------------------------------------------------------
# 5. Déploiement
# ---------------------------------------------------------------------------
log "Déploiement de l'application de démonstration..."
kubectl apply -f manifests/demo-app.yaml

log "Déploiement de kube-state-metrics..."
kubectl apply -f monitoring/kube-state-metrics.yaml

log "Déploiement de Prometheus..."
kubectl apply -f monitoring/prometheus/prometheus.yaml

# Alertmanager est déployé AVANT Prometheus : Prometheus a besoin de le joindre
# pour publier ses alertes. L'ordre inverse est sans conséquence pratique
# (Prometheus réessaie), mais cet ordre reflète la dépendance réelle.
log "Déploiement d'Alertmanager..."
kubectl apply -f monitoring/alertmanager/alertmanager-deployment.yaml

log "Déploiement de Grafana..."
kubectl apply -f monitoring/grafana/grafana.yaml

# ---------------------------------------------------------------------------
# 5bis. Étape 3 : Ollama puis l'agent IA
# ---------------------------------------------------------------------------
# L'ordre est IMPORTANT : Ollama doit être déployé AVANT l'agent, parce que
# l'agent a besoin de le joindre (son /health le vérifie) et parce que le
# téléchargement du modèle (initContainer) prend plusieurs minutes.
#
# Note : le premier déploiement d'Ollama va télécharger ~9 Go de modèle (qwen2.5-coder:14b).
# C'est long, mais le PVC fait que ce n'est payé qu'une fois.
log "Déploiement d'Ollama (le modèle peut prendre plusieurs minutes à télécharger)..."
kubectl apply -f manifests/ollama.yaml

log "Déploiement de l'agent IA..."
kubectl apply -f manifests/aiops-agent.yaml

# ---------------------------------------------------------------------------
# 6. Vérification
# ---------------------------------------------------------------------------
log "Attente que les pods soient prêts (timeout 600s)..."
# Le timeout est long (600 s) à cause de l'initContainer d'Ollama, qui
# télécharge le modèle qwen2.5-coder:14b (~9 Go) au premier démarrage.
if ! kubectl wait --for=condition=Ready pod \
        --namespace="$NAMESPACE" \
        --all \
        --timeout=600s 2>/dev/null; then
    warn "Tous les pods ne sont pas Ready. État actuel :"
    kubectl get pods --namespace="$NAMESPACE"
    warn "Les images locales doivent avoir été chargées : make build"
fi

echo
log "============================================================"
log " Étapes 1 à 3 déployées."
log "============================================================"
log "Application : http://localhost:30090/health"
log "Prometheus  : http://localhost:30093"
log "Grafana     : http://localhost:30094  (admin / admin)"
log
log "Agent IA        :  kubectl -n aiops port-forward svc/aiops-agent 8080"
log
log "Tester un crash :  curl http://localhost:30090/crash"
log "Tester un OOM   :  curl http://localhost:30090/oom"
log "Voir les logs   :  kubectl -n aiops logs -l app=demo-app"
log "Logs de l'agent :  kubectl -n aiops logs -l app=aiops-agent -f"
log "============================================================"
