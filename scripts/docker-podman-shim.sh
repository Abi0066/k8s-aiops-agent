#!/usr/bin/env bash
#
# docker-podman-shim — Adaptateur minimal `docker` -> `podman`.
#
# POURQUOI CE SCRIPT EXISTE
# -------------------------
# `kind` supporte Podman comme runtime de conteneurs, mais UNIQUEMENT pour
# `kind create cluster` (via KIND_EXPERIMENTAL_PROVIDER=podman). Le chemin
# `kind load docker-image` reste câblé en dur sur l'API Docker : dans le code
# source de kind v0.33.0, pkg/cmd/kind/load/docker-image/docker-image.go :
#
#     func save(images []string, dest string) error {
#         commandArgs := append([]string{"save", "-o", dest}, images...)
#         return exec.Command("docker", commandArgs...).Run()
#     }
#
#     func imageID(containerNameOrID string) (string, error) {
#         cmd := exec.Command("docker", "image", "inspect", "-f", "{{ .Id }}", ...)
#
# Le chargement dans le nœud, lui, passe par nodeutils.LoadImageArchive() qui
# est agnostique : c'est le provider qui choisit Docker ou Podman.
#
# Autrement dit, il ne manque que les deux commandes shell ci-dessus. Ce shim
# fournit un exécutable nommé `docker` qui les délègue à `podman`, dont la
# syntaxe est compatible à ces deux endroits :
#   - `podman image inspect -f '{{ .Id }}' <img>`  <=>  `docker image inspect ...`
#   - `podman save -o <fichier> <img>`              <=>  `docker save -o ...`
#
# INSTALLATION
# ------------
# Ce fichier est installé dans ~/.local/bin/docker par scripts/setup.sh.
# Il n'agit QUE sur un sous-ensemble précis de commandes ; toute commande
# docker non reconnue est transmise telle quelle à podman, qui de toute façon
# refuse ce qu'il ne sait pas faire.
#
set -euo pipefail

if ! command -v podman >/dev/null 2>&1; then
    echo "docker-podman-shim: podman introuvable dans le PATH" >&2
    exit 127
fi

# On ne traite que les sous-commandes que kind invoque réellement.
case "${1:-}" in
    image)
        # kind appelle : docker image inspect -f '{{ .Id }}' <image>
        # podman accepte exactement la même syntaxe.
        shift
        exec podman image "$@"
        ;;
    save)
        # kind appelle : docker save -o <dest> <image...>
        # podman accepte exactement la même syntaxe.
        shift
        exec podman save "$@"
        ;;
    *)
        # Tout le reste est délégué tel quel à podman.
        exec podman "$@"
        ;;
esac
