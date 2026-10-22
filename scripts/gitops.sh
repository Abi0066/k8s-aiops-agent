#!/usr/bin/env bash
#
# gitops.sh — Déploie la stack par GitOps : Flux synchronise le cluster depuis Git.
#
# Usage :
#   ./scripts/gitops.sh up      # installe Flux et branche le cluster sur le dépôt
#   ./scripts/gitops.sh sync    # force une synchronisation immédiate
#   ./scripts/gitops.sh status  # état de la source et de la synchronisation
#
# Différence avec cluster-up.sh : ici, kubectl n'applique jamais les manifests
# de l'application. Il installe Flux et lui indique quel dépôt suivre ; c'est
# Flux qui applique deploy/ tel qu'il est dans Git. Un changement non poussé
# n'est donc pas déployé, et une modification faite à la main dans le cluster
# est annulée. La source de vérité, c'est Git.
#
# Variables (facultatives) pour suivre un fork ou une autre branche que main :
#   GITOPS_REPO_URL  URL HTTPS du dépôt (public)
#   GITOPS_BRANCH    branche à suivre

set -euo pipefail
# shellcheck source=scripts/lib.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
cd "$REPO_ROOT"

# Flux v2, épinglé. La CLI tourne dans un conteneur : rien à installer sur
# l'hôte, et la version des contrôleurs est celle de la CLI.
FLUX_VERSION="v2.9.6"
FLUX_CLI_IMAGE="ghcr.io/fluxcd/flux-cli:${FLUX_VERSION}"
FLUX_NAMESPACE="flux-system"
# Nom des deux objets de deploy/flux-sync.yaml.
SYNC_NAME="k8s-aiops-agent"

# Toutes les commandes visent notre cluster kind, quel que soit le contexte
# courant de kubectl.
k() { kubectl --context "kind-${CLUSTER_NAME}" "$@"; }

# ---------------------------------------------------------------------------
# up : installer Flux et brancher le cluster sur le dépôt
# ---------------------------------------------------------------------------
cmd_up() {
    detect_runtime
    log "Runtime : $RUNTIME."

    # Même préparation que cluster-up.sh. Flux applique les manifests, mais
    # ne construit pas nos images : demo-app et l'agent (imagePullPolicy:
    # Never) doivent déjà être dans le nœud.
    prepare_cluster

    # 1. Installation de Flux. Deux contrôleurs suffisent :
    #      source-controller     télécharge le dépôt Git ;
    #      kustomize-controller  rend deploy/ et l'applique.
    #    Les autres (Helm, notifications, mise à jour d'images) ne servent pas
    #    ici et consommeraient de la mémoire pour rien.
    #    `flux install --export` écrit les manifests au lieu de les appliquer :
    #    on garde kubectl pour l'application, comme dans le reste du projet.
    #    --server-side parce que les CRDs de Flux dépassent la taille maximale
    #    de l'annotation qu'utilise `kubectl apply` côté client.
    log "Installation de Flux ${FLUX_VERSION} (source + kustomize controllers)..."
    "$RUNTIME" run --rm "$FLUX_CLI_IMAGE" install --export \
        --components=source-controller,kustomize-controller \
        | k apply --server-side -f -

    # Les CRDs doivent être enregistrées avant de créer des objets de ce type.
    k wait --for=condition=Established --timeout=60s \
        crd/gitrepositories.source.toolkit.fluxcd.io \
        crd/kustomizations.kustomize.toolkit.fluxcd.io
    k -n "$FLUX_NAMESPACE" rollout status deploy/source-controller --timeout=180s
    k -n "$FLUX_NAMESPACE" rollout status deploy/kustomize-controller --timeout=180s

    # 2. Ce que Flux doit suivre. L'URL et la branche de flux-sync.yaml
    #    visent le dépôt principal ; sed les remplace à la volée si les
    #    variables sont fournies, sans modifier le fichier versionné.
    #    Le script vide de départ fait de sed un simple `cat` quand aucune
    #    variable n'est fournie.
    local sed_args=(-e '')
    if [[ -n "${GITOPS_REPO_URL:-}" ]]; then
        sed_args+=(-e "s|^  url: .*|  url: ${GITOPS_REPO_URL}|")
    fi
    if [[ -n "${GITOPS_BRANCH:-}" ]]; then
        sed_args+=(-e "s|^    branch: .*|    branch: ${GITOPS_BRANCH}|")
    fi
    sed "${sed_args[@]}" deploy/flux-sync.yaml | k apply -f -
    log "Source : $(k -n "$FLUX_NAMESPACE" get gitrepository "$SYNC_NAME" \
        -o jsonpath='{.spec.url} (branche {.spec.ref.branch})')"

    # 3. Attente de la première synchronisation. La source doit être prête en
    #    quelques secondes (un clonage). La Kustomization, elle, n'est Ready
    #    que lorsque tous les objets le sont (wait: true) : au premier
    #    démarrage, Ollama télécharge son modèle (~9 Go), d'où le long délai.
    log "Attente du téléchargement du dépôt par Flux..."
    k -n "$FLUX_NAMESPACE" wait "gitrepository/$SYNC_NAME" \
        --for=condition=Ready --timeout=120s \
        || die "Flux n'arrive pas à lire le dépôt : $(k -n "$FLUX_NAMESPACE" \
                get gitrepository "$SYNC_NAME" \
                -o jsonpath='{.status.conditions[?(@.type=="Ready")].message}')"

    log "Attente de la synchronisation (jusqu'à 20 min au premier démarrage)..."
    if ! k -n "$FLUX_NAMESPACE" wait "kustomization/$SYNC_NAME" \
            --for=condition=Ready --timeout=1200s; then
        warn "La synchronisation n'est pas encore Ready. État :"
        cmd_status
    fi

    wait_for_pods

    echo
    log "============================================================"
    log " Stack déployée par Flux depuis Git."
    log "============================================================"
    print_access
    log
    log "État de Flux    :  make gitops-status"
    log "Synchroniser    :  git push, puis make gitops-sync"
    log "============================================================"
}

# ---------------------------------------------------------------------------
# sync : forcer une synchronisation sans attendre l'intervalle
# ---------------------------------------------------------------------------
# Flux interroge le dépôt toutes les minutes. Pour ne pas attendre, on fait ce
# que fait `flux reconcile` : poser l'annotation reconcile.fluxcd.io/
# requestedAt avec une valeur nouvelle. Le contrôleur la voit, réconcilie,
# puis recopie cette valeur dans status.lastHandledReconcileAt : c'est le
# signal que la demande a été traitée.
reconcile() {
    local kind="$1" timeout="$2" stamp
    stamp="$(date +%s)"
    k -n "$FLUX_NAMESPACE" annotate --overwrite "$kind/$SYNC_NAME" \
        "reconcile.fluxcd.io/requestedAt=$stamp" >/dev/null
    k -n "$FLUX_NAMESPACE" wait "$kind/$SYNC_NAME" \
        --for=jsonpath='{.status.lastHandledReconcileAt}'="$stamp" \
        --timeout="$timeout" >/dev/null
}

cmd_sync() {
    # La source d'abord, pour récupérer le dernier commit ; puis la
    # Kustomization, pour l'appliquer (et annuler toute dérive).
    log "Récupération du dernier commit..."
    reconcile gitrepository 60s
    log "Application de deploy/ ..."
    reconcile kustomization 300s \
        || warn "Synchronisation toujours en cours (délai dépassé)."
    cmd_status
}

# ---------------------------------------------------------------------------
# status : où en est Flux
# ---------------------------------------------------------------------------
# READY=True et un message « Applied revision: main@sha1:... » : le cluster
# correspond à ce commit.
cmd_status() {
    k -n "$FLUX_NAMESPACE" get gitrepositories,kustomizations
}

case "${1:-}" in
    up)     cmd_up ;;
    sync)   cmd_sync ;;
    status) cmd_status ;;
    *)      die "Usage : $0 {up|sync|status}" ;;
esac
