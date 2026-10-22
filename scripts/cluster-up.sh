#!/usr/bin/env bash
#
# cluster-up.sh — Crée le cluster kind et y déploie le projet (étapes 1 à 3).
#
# Le script est idempotent : on peut le relancer sans risque, même si le
# cluster existe déjà. Il se contente alors de réappliquer les manifests.

set -euo pipefail
# shellcheck source=scripts/lib.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
cd "$REPO_ROOT"

# ---------------------------------------------------------------------------
# 1. Runtime de conteneurs et images
# ---------------------------------------------------------------------------
detect_runtime
log "Runtime : $RUNTIME."

# demo-app et l'agent sont en imagePullPolicy: Never : sans leur image dans le
# nœud, leurs pods resteraient en ErrImageNeverPull. On vérifie qu'elles sont
# construites AVANT de créer le cluster, pour échouer tôt et proprement.
for image in "$DEMO_APP_IMAGE" "$AGENT_IMAGE"; do
    "$RUNTIME" image inspect "$image" >/dev/null 2>&1 \
        || die "Image $image introuvable : lancez d'abord 'make build'."
done

# ---------------------------------------------------------------------------
# 2. Création du cluster
# ---------------------------------------------------------------------------
if cluster_exists; then
    log "Le cluster '$CLUSTER_NAME' existe déjà."
else
    log "Création du cluster '$CLUSTER_NAME'..."
    kind create cluster --name "$CLUSTER_NAME" --config kind-config.yaml --wait 120s
fi

# Le contexte kubectl doit pointer sur notre cluster.
kubectl config use-context "kind-${CLUSTER_NAME}" >/dev/null

# Chargement des images dans le nœud. Les images publiques que l'hôte n'a
# pas encore (make build ne les a pas tirées) seront tirées par le kubelet.
log "Chargement des images dans le nœud..."
load_image "$DEMO_APP_IMAGE"
load_image "$AGENT_IMAGE"
mapfile -t THIRD_PARTY_IMAGES < <(third_party_images)
for image in "${THIRD_PARTY_IMAGES[@]}"; do
    if "$RUNTIME" image inspect "$image" >/dev/null 2>&1; then
        load_image "$image"
    fi
done

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

# Alertmanager est déployé AVANT Prometheus : Prometheus a besoin de le joindre
# pour publier ses alertes. L'ordre inverse est sans conséquence pratique
# (Prometheus réessaie), mais cet ordre reflète la dépendance réelle.
log "Déploiement d'Alertmanager..."
kubectl apply -f monitoring/alertmanager/alertmanager-deployment.yaml

log "Déploiement de Prometheus..."
kubectl apply -f monitoring/prometheus/prometheus.yaml

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
    warn "Au premier démarrage, Ollama télécharge encore son modèle :"
    warn "    kubectl -n $NAMESPACE logs deploy/ollama -c pull-model -f"
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
