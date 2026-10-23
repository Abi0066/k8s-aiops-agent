#!/usr/bin/env bash
#
# cluster-down.sh — Détruit le cluster kind et nettoie les ressources locales.
#
# L'objectif de ce script est qu'un `make cluster-down` laisse la machine
# dans l'état où l'on avait tout démarré : pas de conteneur, pas de réseau,
# pas de volume. Avec --images (make clean-images), les images construites
# pour le projet sont aussi supprimées.
#
set -euo pipefail
# shellcheck source=scripts/lib.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
cd "$REPO_ROOT"

detect_runtime

# ---------------------------------------------------------------------------
# 1. Suppression du cluster kind
# ---------------------------------------------------------------------------
if cluster_exists; then
    log "Suppression du cluster '$CLUSTER_NAME'..."
    kind delete cluster --name "$CLUSTER_NAME"
    log "Cluster supprimé."
else
    log "Le cluster '$CLUSTER_NAME' n'existe pas."
fi

# ---------------------------------------------------------------------------
# 2. Nettoyage du réseau
# ---------------------------------------------------------------------------
# kind crée un réseau dédié ("kind") et ne le supprime pas avec le cluster.
# S'il sert encore à un autre cluster kind, la suppression échoue et on le
# laisse en place.
if "$RUNTIME" network inspect kind >/dev/null 2>&1; then
    log "Suppression du réseau 'kind'..."
    "$RUNTIME" network rm kind >/dev/null 2>&1 \
        || warn "Réseau 'kind' non supprimé (utilisé par un autre cluster ?)."
fi

# ---------------------------------------------------------------------------
# 3. Nettoyage des images locales du projet
# ---------------------------------------------------------------------------
# Option --images (make clean-images) : supprime aussi les deux images
# construites par le projet. Elles sont régénérables avec `make build`, donc
# les supprimer est sans risque. Les images publiques (Prometheus, Ollama...)
# restent sur l'hôte : les retélécharger coûterait plusieurs Go.
if [[ "${1:-}" == "--images" ]]; then
    log "Suppression des images locales du projet..."
    for image in "$DEMO_APP_IMAGE" "$AGENT_IMAGE"; do
        if "$RUNTIME" image inspect "$image" >/dev/null 2>&1; then
            "$RUNTIME" rmi "$image" >/dev/null && log "  supprimée : $image"
        fi
    done
fi

echo
log "Nettoyage terminé."
log "Le cluster, le réseau et (optionnellement) les images du projet sont supprimés."
log "Le modèle d'Ollama vivait dans un volume du cluster : il est supprimé avec"
log "lui et sera retéléchargé (~9 Go) au prochain 'make cluster-up'."
