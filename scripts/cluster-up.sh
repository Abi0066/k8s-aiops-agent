#!/usr/bin/env bash
#
# cluster-up.sh — Crée le cluster kind et y déploie le projet (étapes 1 à 3).
#
# Déploiement « à la main » : kubectl applique les manifests du disque, y
# compris ceux qui ne sont pas encore commités. C'est le mode du
# développement. Pour un déploiement piloté par Git, voir scripts/gitops.sh.
#
# Le script est idempotent : on peut le relancer sans risque, même si le
# cluster existe déjà. Il se contente alors de réappliquer les manifests.

set -euo pipefail
# shellcheck source=scripts/lib.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
cd "$REPO_ROOT"

# ---------------------------------------------------------------------------
# 1. Cluster et images
# ---------------------------------------------------------------------------
detect_runtime
log "Runtime : $RUNTIME."
prepare_cluster

# Si Flux tourne déjà dans ce cluster (make gitops-up), il réappliquera Git
# à son prochain cycle et effacera ce que l'on applique ici depuis le disque.
# On prévient au lieu de refuser : appliquer un essai local reste utile.
if kubectl get namespace flux-system >/dev/null 2>&1; then
    warn "Flux synchronise ce cluster depuis Git : les changements locaux"
    warn "non poussés seront annulés à sa prochaine réconciliation."
    warn "Pour déployer une modification : git push, puis 'make gitops-sync'."
fi

# ---------------------------------------------------------------------------
# 2. Déploiement
# ---------------------------------------------------------------------------
# Un seul `apply -k` : kustomize rend deploy/ (manifests/ + monitoring/),
# génère les ConfigMaps depuis les fichiers de configuration, puis kubectl
# applique le tout. L'ordre des fichiers n'a pas d'importance : kustomize
# trie les objets par type (Namespace d'abord, puis ConfigMaps, puis
# Deployments...), et les dépendances entre pods (Prometheus -> Alertmanager,
# agent -> Ollama) se résolvent d'elles-mêmes, chacun réessayant tant que
# l'autre n'est pas prêt.
#
# Note : le premier démarrage d'Ollama télécharge ~9 Go de modèle
# (qwen2.5-coder:14b). C'est long, mais le PVC fait que ce n'est payé qu'une
# fois.
log "Déploiement de la stack (kubectl apply -k deploy)..."
kubectl apply -k deploy

# ---------------------------------------------------------------------------
# 3. Vérification
# ---------------------------------------------------------------------------
wait_for_pods

echo
log "============================================================"
log " Étapes 1 à 3 déployées."
log "============================================================"
print_access
log "============================================================"
