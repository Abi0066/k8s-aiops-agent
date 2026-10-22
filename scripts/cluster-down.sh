#!/usr/bin/env bash
#
# cluster-down.sh — Détruit le cluster kind et nettoie les ressources locales.
#
# L'objectif de ce script est qu'un `make cluster-down` laisse la machine
# dans l'état où l'on avait tout démarré : pas de conteneur, pas de réseau
# Podman, pas de volume, pas d'image construite pour le projet.
#
set -euo pipefail

CLUSTER_NAME="k8s-aiops"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

log()  { printf '\033[0;36m[cluster-down]\033[0m %s\n' "$*"; }
warn() { printf '\033[0;33m[cluster-down]\033[0m %s\n' "$*"; }

if command -v podman >/dev/null 2>&1; then
    export KIND_EXPERIMENTAL_PROVIDER=podman
fi

# ---------------------------------------------------------------------------
# 1. Suppression du cluster kind
# ---------------------------------------------------------------------------
if kind get clusters 2>/dev/null | grep -qx "$CLUSTER_NAME"; then
    log "Suppression du cluster '$CLUSTER_NAME'..."
    kind delete cluster --name "$CLUSTER_NAME"
    log "Cluster supprimé."
else
    log "Le cluster '$CLUSTER_NAME' n'existe pas."
fi

# ---------------------------------------------------------------------------
# 2. Nettoyage des ressources Podman
# ---------------------------------------------------------------------------
# kind utilise un réseau dédié ("kind") et des volumes pour le stockage du
# nœud. Ils sont normalement supprimés avec le cluster, mais une creation
# interrompue peut en laisser.
if command -v podman >/dev/null 2>&1; then
    if podman network exists kind 2>/dev/null; then
        log "Suppression du réseau Podman 'kind'..."
        podman network rm kind 2>/dev/null || warn "Réseau 'kind' non supprimé (peut-être utilisé)."
    fi
fi

# ---------------------------------------------------------------------------
# 3. Nettoyage des images locales du projet
# ---------------------------------------------------------------------------
# Option --images : supprime aussi les images Docker/Podman du projet.
# Elles sont régénérables avec `make build`, donc les supprimer est sans risque.
if [[ "${1:-}" == "--images" ]] && command -v podman >/dev/null 2>&1; then
    log "Suppression des images locales du projet..."
    for image in localhost/demo-app:1.0.3 localhost/aiops-agent:1.0.0; do
        if podman image exists "$image" 2>/dev/null; then
            podman rmi "$image" 2>/dev/null && log "  supprimée : $image"
        fi
    done
fi

echo
log "Nettoyage terminé."
log "Le cluster, le réseau et (optionnellement) les images du projet sont supprimés."
log "Ollama et ses modèles ne sont PAS touchés : ils servent aussi à l'agent IA."
