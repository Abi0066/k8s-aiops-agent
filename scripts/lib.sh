# shellcheck shell=bash
# Les variables définies ici sont lues par les scripts qui chargent ce
# fichier : analysé seul, shellcheck les croirait inutilisées (SC2034).
# shellcheck disable=SC2034
#
# lib.sh — Réglages et fonctions communs aux scripts du projet.
#
# Chaque script le charge avec :
#   source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
#
# Le nom du cluster, le namespace et la détection du runtime n'y sont écrits
# qu'une fois. Les noms d'images, eux, sont lus dans les manifests : c'est là
# qu'on change une version, et les scripts suivent sans rien modifier.

CLUSTER_NAME="k8s-aiops"
NAMESPACE="aiops"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# Le préfixe des messages est le nom du script : [cluster-up], [setup]...
LOG_PREFIX="$(basename "$0" .sh)"
log()  { printf '\033[0;36m[%s]\033[0m %s\n' "$LOG_PREFIX" "$*"; }
warn() { printf '\033[0;33m[%s]\033[0m %s\n' "$LOG_PREFIX" "$*"; }
die()  { printf '\033[0;31m[%s]\033[0m %s\n' "$LOG_PREFIX" "$*" >&2; exit 1; }

# ---------------------------------------------------------------------------
# Runtime de conteneurs
# ---------------------------------------------------------------------------
# podman en priorité, docker en secours. CONTAINER_RUNTIME force le choix (la
# CI l'utilise pour prendre docker). kind doit savoir quel runtime piloter :
# KIND_EXPERIMENTAL_PROVIDER le lui indique.
detect_runtime() {
    RUNTIME="${CONTAINER_RUNTIME:-}"
    if [[ -z "$RUNTIME" ]]; then
        if command -v podman >/dev/null 2>&1; then
            RUNTIME="podman"
        elif command -v docker >/dev/null 2>&1; then
            RUNTIME="docker"
        else
            die "Aucun runtime de conteneurs (podman ou docker) trouvé."
        fi
    fi
    if [[ "$RUNTIME" == "podman" ]]; then
        export KIND_EXPERIMENTAL_PROVIDER=podman
    fi
}

# ---------------------------------------------------------------------------
# Images
# ---------------------------------------------------------------------------
# Image du premier conteneur d'un manifest.
image_in() {
    awk '$1 == "image:" { print $2; exit }' "$REPO_ROOT/$1"
}

# Nos deux images, construites localement (registre localhost/).
DEMO_APP_IMAGE="$(image_in manifests/demo-app.yaml)"
AGENT_IMAGE="$(image_in manifests/aiops-agent.yaml)"

# Images publiques du cluster : toutes celles des manifests, sauf les nôtres.
third_party_images() {
    find "$REPO_ROOT/manifests" "$REPO_ROOT/monitoring" -name '*.yaml' -exec \
        awk '$1 == "image:" && $2 !~ /^localhost\// { print $2 }' {} + | sort -u
}

# ---------------------------------------------------------------------------
# Cluster
# ---------------------------------------------------------------------------
cluster_exists() {
    kind get clusters 2>/dev/null | grep -qx "$CLUSTER_NAME"
}

# Le cluster n'a qu'un nœud (kind-config.yaml), nommé <cluster>-control-plane.
node_has_image() {
    "$RUNTIME" exec "${CLUSTER_NAME}-control-plane" crictl inspecti "$1" >/dev/null 2>&1
}

# Copie une image de l'hôte dans le nœud kind.
#
# `kind load docker-image` appelle la commande `docker` en dur, même quand kind
# pilote podman. On passe donc par une archive : `$RUNTIME save` l'écrit et
# `kind load image-archive` l'importe dans le containerd du nœud. Les deux
# runtimes suivent le même chemin, sans adaptateur.
#
# Une image déjà présente dans le nœud n'est pas rechargée : un tag épinglé
# désigne toujours la même image. --force recharge quand même ; c'est le cas
# de nos images, reconstruites sous le même tag.
#
# Le corps entre parenthèses s'exécute dans un sous-shell : le trap qui
# supprime l'archive ne concerne que cette fonction.
load_image() (
    image="$1"
    if [[ "${2:-}" != "--force" ]] && node_has_image "$image"; then
        log "Déjà dans le nœud : $image"
        exit 0
    fi
    # /var/tmp est sur disque, alors que /tmp est souvent en mémoire (tmpfs) :
    # l'archive d'Ollama pèse plusieurs Go.
    dir="$(mktemp -d "${TMPDIR:-/var/tmp}/aiops-image.XXXXXX")"
    trap 'rm -r -- "$dir"' EXIT
    log "Chargement de $image dans le nœud..."
    "$RUNTIME" save -o "$dir/image.tar" "$image"
    kind load image-archive "$dir/image.tar" --name "$CLUSTER_NAME"
)
