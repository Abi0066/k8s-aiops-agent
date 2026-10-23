#!/usr/bin/env bash
#
# build-images.sh — Construit les images du projet et les charge dans le cluster.
#
# Le flux en deux temps est spécifique au développement local :
#   1. podman build -> crée l'image sur la machine hôte
#   2. kind load    -> copie l'image dans le runtime containerd du nœud kind
#
# Sur un cluster réel (EKS), l'étape 2 disparaît : l'image est poussée vers un
# registre (ECR) et le kubelet la tire. C'est ce qui se passerait à l'étape 5
# avec le pipeline GitHub Actions.
#
# Si le cluster n'existe pas encore, les images sont seulement construites :
# cluster-up.sh les chargera en créant le cluster.
#
set -euo pipefail
# shellcheck source=scripts/lib.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
cd "$REPO_ROOT"

detect_runtime
log "Runtime : $RUNTIME"

# ---------------------------------------------------------------------------
# Construction des images
# ---------------------------------------------------------------------------
# L'image est taguée avec le registre « localhost » : c'est la convention
# Podman pour une image locale. Le tag est lu dans le manifest (lib.sh), pour
# correspondre EXACTEMENT à celui que le kubelet va chercher.
build_image() {
    local context="$1" tag="$2"
    log "Construction de l'image $tag (contexte : $context)..."
    "$RUNTIME" build -t "$tag" "$context"
}

build_image app "$DEMO_APP_IMAGE"
build_image agent "$AGENT_IMAGE"

# ---------------------------------------------------------------------------
# Images tierces
# ---------------------------------------------------------------------------
# Prometheus, Alertmanager, Grafana, kube-state-metrics et Ollama sont des
# images publiques : on les tire une seule fois sur la machine hôte, puis on
# les charge dans le nœud.
#
# Pourquoi ne pas laisser le kubelet les tirer lui-même ?
#   - cela fonctionne, mais dépend du réseau à chaque recréation de cluster ;
#   - les charger nous-mêmes rend le cluster fonctionnel hors ligne.
# C'est un compromis assumé pour un environnement de développement local.
#
# La liste est lue dans les manifests : changer une version là-bas suffit.
# Les tags sont épinglés ; en production (étape 5), on ajoutera le digest
# pour garantir l'exactitude de l'image déployée.
mapfile -t THIRD_PARTY_IMAGES < <(third_party_images)
for image in "${THIRD_PARTY_IMAGES[@]}"; do
    # `image inspect` existe sous podman ET docker. `image exists` est propre
    # à podman : sous docker, il échouait, et chaque image repassait par le
    # registre à chaque build.
    if "$RUNTIME" image inspect "$image" >/dev/null 2>&1; then
        log "Image déjà présente : $image"
    else
        log "Tirage de $image..."
        "$RUNTIME" pull "$image"
    fi
done

# ---------------------------------------------------------------------------
# Chargement dans le cluster
# ---------------------------------------------------------------------------
if ! cluster_exists; then
    log "Le cluster '$CLUSTER_NAME' n'existe pas encore : les images sont"
    log "construites, 'make cluster-up' les chargera."
    exit 0
fi

# Nos images sont reconstruites sous le même tag : --force les recharge.
load_image "$DEMO_APP_IMAGE" --force
load_image "$AGENT_IMAGE" --force
for image in "${THIRD_PARTY_IMAGES[@]}"; do
    load_image "$image"
done

# Le tag n'a pas changé : sans redémarrage, les pods garderaient l'ancienne
# image. rollout restart recrée les pods, qui prennent celle qu'on vient de
# charger.
for deployment in demo-app aiops-agent; do
    kubectl --context "kind-$CLUSTER_NAME" -n "$NAMESPACE" \
        rollout restart "deployment/$deployment" 2>/dev/null \
        || warn "Le deployment $deployment n'existe pas encore (make cluster-up)."
done

echo
log "Images construites et chargées."
