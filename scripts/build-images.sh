#!/usr/bin/env bash
#
# build-images.sh — Construit les images du projet et les charge dans le cluster.
#
# Le flux en deux temps est spécifique au développement local :
#   1. podman build  -> crée l'image sur la machine hôte
#   2. kind load    -> copie l'image dans le runtime containerd du nœud kind
#
# Sur un cluster réel (EKS), l'étape 2 disparaît : l'image est poussée vers un
# registre (ECR) et le kubelet la tire. C'est ce qui se passerait à l'étape 5
# avec le pipeline GitHub Actions.
#
set -euo pipefail

CLUSTER_NAME="k8s-aiops"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

log()  { printf '\033[0;36m[build]\033[0m %s\n' "$*"; }
warn() { printf '\033[0;33m[build]\033[0m %s\n' "$*"; }
die()  { printf '\033[0;31m[build]\033[0m %s\n' "$*" >&2; exit 1; }

# Runtime de conteneurs : podman en priorite, docker en secours.
if command -v podman >/dev/null 2>&1; then
    RUNTIME="podman"
    export KIND_EXPERIMENTAL_PROVIDER=podman
elif command -v docker >/dev/null 2>&1; then
    RUNTIME="docker"
else
    die "Aucun runtime de conteneurs (podman ou docker) trouvé."
fi
log "Runtime : $RUNTIME"

# Le shim docker->podman : kind load docker-image appelle « docker » en dur.
# Voir scripts/docker-podman-shim.sh pour l'explication complète.
if [[ "$RUNTIME" == "podman" ]]; then
    SHIM_DIR="${HOME}/.local/bin"
    mkdir -p "$SHIM_DIR"
    cp "$REPO_ROOT/scripts/docker-podman-shim.sh" "$SHIM_DIR/docker"
    chmod +x "$SHIM_DIR/docker"
    log "Shim docker->podman installé dans $SHIM_DIR/docker"
fi

# ---------------------------------------------------------------------------
# Construction des images
# ---------------------------------------------------------------------------
# L'image est taguée avec le registre « localhost » : c'est la convention
# Podman pour une image locale. Le nom doit correspondre EXACTEMENT à celui
# utilisé dans les manifests Kubernetes, sinon le kubelet ne la trouvera pas.
build_image() {
    local context="$1" dockerfile="$2" tag="$3"
    log "Construction de l'image $tag (context: $context)..."
    "$RUNTIME" build -t "$tag" -f "$dockerfile" "$context"
}

build_image "app" "app/Dockerfile" "localhost/demo-app:1.0.3"
build_image "agent" "agent/Dockerfile" "localhost/aiops-agent:1.0.0"

# ---------------------------------------------------------------------------
# Chargement des images tierces dans le nœud
# ---------------------------------------------------------------------------
# Prometheus, Alertmanager et Grafana sont des images publiques : on les
# tire une seule fois sur la machine hôte, puis on les charge dans le nœud.
#
# Pourquoi ne pas laisser le kubelet les tirer lui-même ?
#   - cela fonctionne, mais dépend du réseau à chaque recréation de cluster ;
#   - les charger nous-mêmes rend le cluster fonctionnel hors ligne.
# C'est un compromis assumé pour un environnement de développement local.
#
# Les tags sont épinglés. En production (étape 5), on ajoutera le digest
# pour garantir l'exactitude de l'image déployée.
for image in \
    "quay.io/prometheus/prometheus:v3.7.3" \
    "quay.io/prometheus/alertmanager:v0.29.0" \
    "docker.io/grafana/grafana:12.3.0" \
    "registry.k8s.io/kube-state-metrics/kube-state-metrics:v2.17.0" \
    "docker.io/ollama/ollama:latest"
do
    if ! "$RUNTIME" image exists "$image" 2>/dev/null; then
        log "Tirage de $image..."
        "$RUNTIME" pull "$image"
    else
        log "Image déjà présente : $image"
    fi
done

# ---------------------------------------------------------------------------
# Chargement dans le cluster
# ---------------------------------------------------------------------------
if ! kind get clusters 2>/dev/null | grep -qx "$CLUSTER_NAME"; then
    warn "Le cluster '$CLUSTER_NAME' n'existe pas. Lancez 'make cluster-up' d'abord."
    warn "Les images sont construites, mais pas chargées dans un cluster."
    exit 0
fi

log "Chargement de l'image dans le cluster '$CLUSTER_NAME'..."
kind load docker-image "localhost/demo-app:1.0.3" --name "$CLUSTER_NAME"
kind load docker-image "localhost/aiops-agent:1.0.0" --name "$CLUSTER_NAME"

# Chargement des images de monitoring dans le nœud, pour éviter un pull
# réseau à chaque recréation du cluster.
for image in \
    "quay.io/prometheus/prometheus:v3.7.3" \
    "quay.io/prometheus/alertmanager:v0.29.0" \
    "docker.io/grafana/grafana:12.3.0" \
    "registry.k8s.io/kube-state-metrics/kube-state-metrics:v2.17.0" \
    "docker.io/ollama/ollama:latest"
do
    log "Chargement de $image dans le nœud..."
    kind load docker-image "$image" --name "$CLUSTER_NAME"
done

# Le pod va redémarrer : on force un rollout pour qu'il utilise la nouvelle image.
log "Redémarrage du deployment demo-app..."
kubectl -n aiops rollout restart deployment/demo-app 2>/dev/null || \
    warn "Le deployment demo-app n'existe pas encore (cluster-up non lance)."

echo
log "Images construites et chargees."
