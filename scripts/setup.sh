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
# shellcheck source=scripts/lib.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

INSTALL_DIR="${INSTALL_DIR:-$HOME/.local/bin}"

# Versions épinglées. kind v0.33.0 embarque par défaut kindest/node:v1.37.0 ;
# kubectl v1.37.1 est donc à une version mineure du serveur API (supported skew).
KIND_VERSION="v0.33.0"
KUBECTL_VERSION="v1.37.1"

# kind et kubectl sont publiés pour amd64 et arm64 (ex. une VM Linux sur un
# Mac Apple Silicon) : on prend le binaire de l'architecture de la machine.
case "$(uname -m)" in
    x86_64)        ARCH="amd64" ;;
    aarch64|arm64) ARCH="arm64" ;;
    *)             die "Architecture non supportée : $(uname -m)" ;;
esac

# Télécharge un binaire, vérifie son empreinte SHA-256 publiée, puis
# l'installe. Le fichier n'arrive dans $INSTALL_DIR qu'une fois vérifié : un
# téléchargement interrompu ou altéré ne remplace jamais un binaire valide.
#
# Usage : install_binary <nom> <url du binaire> <url de l'empreinte>
install_binary() {
    local name="$1" url="$2" sha_url="$3" tmp expected
    tmp="$(mktemp)"
    curl -fsSL --retry 3 -o "$tmp" "$url"
    # Le fichier d'empreinte contient le hash, suivi du nom du fichier chez
    # kind, seul chez kubectl : on ne garde que le premier champ.
    expected="$(curl -fsSL --retry 3 "$sha_url" | awk '{ print $1 }')"
    if ! echo "${expected}  ${tmp}" | sha256sum --check --status; then
        rm -f -- "$tmp"
        die "Empreinte SHA-256 invalide pour $name ($url)."
    fi
    install -m 0755 "$tmp" "$INSTALL_DIR/$name"
    rm -f -- "$tmp"
}

install_kind() {
    if [[ -x "$INSTALL_DIR/kind" ]] && "$INSTALL_DIR/kind" version | grep -q "$KIND_VERSION"; then
        log "kind $KIND_VERSION déjà installé."
        return
    fi
    log "Téléchargement de kind $KIND_VERSION ($ARCH)..."
    local url="https://kind.sigs.k8s.io/dl/${KIND_VERSION}/kind-linux-${ARCH}"
    install_binary kind "$url" "${url}.sha256sum"
    log "kind $KIND_VERSION installé dans $INSTALL_DIR"
}

install_kubectl() {
    if [[ -x "$INSTALL_DIR/kubectl" ]] && "$INSTALL_DIR/kubectl" version --client=true 2>/dev/null | grep -q "$KUBECTL_VERSION"; then
        log "kubectl $KUBECTL_VERSION déjà installé."
        return
    fi
    log "Téléchargement de kubectl $KUBECTL_VERSION ($ARCH)..."
    local url="https://dl.k8s.io/release/${KUBECTL_VERSION}/bin/linux/${ARCH}/kubectl"
    install_binary kubectl "$url" "${url}.sha256"
    log "kubectl $KUBECTL_VERSION installé dans $INSTALL_DIR"
}

main() {
    mkdir -p "$INSTALL_DIR"
    install_kind
    install_kubectl

    # kind 0.33 exige Podman >= 3.0 ou Docker >= 20.10.
    detect_runtime
    log "Runtime détecté : $("$RUNTIME" --version)"

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

    # Vérifie la limite d'instances inotify. containerd en ouvre une par
    # conteneur pour être prévenu des OOMKill, sur un compteur partagé avec
    # les autres programmes du même utilisateur. À 128 (valeur par défaut du
    # noyau), il n'y arrive plus : un pod tué par manque de mémoire apparaît
    # « Error » au lieu de « OOMKilled », et l'auto-guérison ne se déclenche pas.
    local inotify_instances
    inotify_instances=$(cat /proc/sys/fs/inotify/max_user_instances 2>/dev/null || echo 0)
    if (( inotify_instances < 512 )); then
        warn "fs.inotify.max_user_instances vaut ${inotify_instances} — les OOMKill seront vus comme « Error »."
        warn "À exécuter avec les privilèges root :"
        warn "    sudo sysctl -w fs.inotify.max_user_instances=512"
        warn "    echo fs.inotify.max_user_instances=512 | sudo tee /etc/sysctl.d/99-kind.conf"
    else
        log "Limite inotify suffisante (${inotify_instances} instances)."
    fi

    log "Setup terminé."
}

main "$@"
