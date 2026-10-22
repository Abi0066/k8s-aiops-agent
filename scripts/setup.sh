#!/usr/bin/env bash
#
# setup.sh — Installe les binaires clients nécessaires au projet.
#
# Ce script est idempotent : le relancer ne réinstalle rien si les binaires
# sont déjà à la bonne version.
#
# Binaires installés dans $INSTALL_DIR (par défaut ~/.local/bin, déjà dans le PATH).
# Aucun privilège root requis — c'est un choix délibéré pour que le projet
# soit utilisable sur une machine Fedora comme sur une Ubuntu, sans sudo.
#
set -euo pipefail

INSTALL_DIR="${INSTALL_DIR:-$HOME/.local/bin}"

# Versions épinglées. kind v0.33.0 embarque par défaut kindest/node:v1.37.0 ;
# kubectl v1.37.1 est donc à une version mineure du serveur API (supported skew).
KIND_VERSION="v0.33.0"
KUBECTL_VERSION="v1.37.1"

log()  { printf '\033[0;36m[setup]\033[0m %s\n' "$*"; }
warn() { printf '\033[0;33m[setup]\033[0m %s\n' "$*"; }
die()  { printf '\033[0;31m[setup]\033[0m %s\n' "$*" >&2; exit 1; }

install_kind() {
    if [[ -x "$INSTALL_DIR/kind" ]] && "$INSTALL_DIR/kind" version | grep -q "$KIND_VERSION"; then
        log "kind $KIND_VERSION déjà installé."
        return
    fi
    log "Téléchargement de kind $KIND_VERSION..."
    curl -fsSL --retry 3 -o "$INSTALL_DIR/kind" \
        "https://kind.sigs.k8s.io/dl/${KIND_VERSION}/kind-linux-amd64"
    chmod +x "$INSTALL_DIR/kind"
    log "kind $KIND_VERSION installé dans $INSTALL_DIR"
}

install_kubectl() {
    if [[ -x "$INSTALL_DIR/kubectl" ]] && "$INSTALL_DIR/kubectl" version --client=true 2>/dev/null | grep -q "$KUBECTL_VERSION"; then
        log "kubectl $KUBECTL_VERSION déjà installé."
        return
    fi
    log "Téléchargement de kubectl $KUBECTL_VERSION..."
    curl -fsSL --retry 3 -o "$INSTALL_DIR/kubectl" \
        "https://dl.k8s.io/release/${KUBECTL_VERSION}/bin/linux/amd64/kubectl"
    chmod +x "$INSTALL_DIR/kubectl"
    log "kubectl $KUBECTL_VERSION installé dans $INSTALL_DIR"
}

# kind 0.33 exige Podman >= 3.0 ou Docker >= 20.10. On détecte ce qui est présent
# et on exporte KIND_EXPERIMENTAL_PROVIDER pour que kind pilote le bon runtime.
detect_container_runtime() {
    if command -v podman >/dev/null 2>&1; then
        export KIND_EXPERIMENTAL_PROVIDER=podman
        log "Runtime détecté : podman ($(podman --version)). KIND_EXPERIMENTAL_PROVIDER=podman"
    elif command -v docker >/dev/null 2>&1; then
        log "Runtime détecté : docker."
    else
        die "Aucun runtime de conteneurs trouvé (podman ou docker requis pour kind)."
    fi
}

main() {
    mkdir -p "$INSTALL_DIR"
    install_kind
    install_kubectl
    detect_container_runtime

    # Vérifie que br_netfilter est chargé : sans lui, le réseau interne de
    # Kubernetes (DNS interne, routage des Service) ne fonctionne pas.
    if [[ ! -d /sys/module/br_netfilter ]]; then
        warn "br_netfilter n'est PAS chargé — le réseau Kubernetes sera cassé."
        warn "À exécuter avec les privilèges root :"
        warn "    sudo modprobe br_netfilter"
        warn "    echo br_netfilter | sudo tee /etc/modules-load.d/k8s.conf"
    else
        log "br_netfilter chargé."
    fi

    log "Setup terminé."
}

main "$@"
