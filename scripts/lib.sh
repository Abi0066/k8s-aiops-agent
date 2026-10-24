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

# ---------------------------------------------------------------------------
# Préparation et suivi du déploiement (cluster-up.sh et gitops.sh)
# ---------------------------------------------------------------------------
# Les deux modes de déploiement commencent de la même façon : un cluster, et
# nos images dans son nœud. Ils ne diffèrent que par ce qui applique les
# manifests ensuite : kubectl depuis le disque, ou Flux depuis Git.
#
# Crée le cluster s'il n'existe pas, pointe kubectl dessus et y charge les
# images.
prepare_cluster() {
    local image

    # demo-app et l'agent sont en imagePullPolicy: Never : sans leur image dans
    # le nœud, leurs pods resteraient en ErrImageNeverPull. On vérifie qu'elles
    # sont construites AVANT de créer le cluster, pour échouer tôt et proprement.
    for image in "$DEMO_APP_IMAGE" "$AGENT_IMAGE"; do
        "$RUNTIME" image inspect "$image" >/dev/null 2>&1 \
            || die "Image $image introuvable : lancez d'abord 'make build'."
    done

    if cluster_exists; then
        log "Le cluster '$CLUSTER_NAME' existe déjà."
    else
        log "Création du cluster '$CLUSTER_NAME'..."
        kind create cluster --name "$CLUSTER_NAME" \
            --config "$REPO_ROOT/kind-config.yaml" --wait 120s
    fi

    # Le contexte kubectl doit pointer sur notre cluster.
    kubectl config use-context "kind-${CLUSTER_NAME}" >/dev/null

    # Les images publiques que l'hôte n'a pas encore (make build ne les a pas
    # tirées) seront tirées par le kubelet.
    log "Chargement des images dans le nœud..."
    load_image "$DEMO_APP_IMAGE"
    load_image "$AGENT_IMAGE"
    while read -r image; do
        if "$RUNTIME" image inspect "$image" >/dev/null 2>&1; then
            load_image "$image"
        fi
    done < <(third_party_images)
}

# Attend que tous les pods du namespace soient Ready.
#
# Le délai est long (600 s) à cause de l'initContainer d'Ollama, qui télécharge
# le modèle (~9 Go) au premier démarrage. Un dépassement n'est pas une erreur :
# le déploiement continue, on affiche seulement où regarder.
wait_for_pods() {
    log "Attente que les pods soient prêts (timeout 600s)..."
    # status.phase!=Succeeded écarte les pods TERMINÉS (un Job, un test de
    # pull d'image). Un pod Completed n'est jamais Ready : sans ce filtre,
    # kubectl wait échoue et le déploiement annonce à tort un incident.
    if ! kubectl wait --for=condition=Ready pod \
            --namespace="$NAMESPACE" \
            --all \
            --field-selector=status.phase!=Succeeded \
            --timeout=600s 2>/dev/null; then
        warn "Tous les pods ne sont pas Ready. État actuel :"
        kubectl get pods --namespace="$NAMESPACE"
        warn "Au premier démarrage, Ollama télécharge encore son modèle :"
        warn "    kubectl -n $NAMESPACE logs deploy/ollama -c pull-model -f"
    fi
}

# Les adresses à retenir, affichées à la fin d'un déploiement.
print_access() {
    log "Application : http://localhost:30090/health"
    log "Prometheus  : http://localhost:30093"
    log "Grafana     : http://localhost:30094  (admin / admin)"
    log
    log "Agent IA        :  kubectl -n $NAMESPACE port-forward svc/aiops-agent 8080"
    log
    log "Tester un crash :  curl http://localhost:30090/crash"
    log "Tester un OOM   :  curl http://localhost:30090/oom"
    log "Voir les logs   :  kubectl -n $NAMESPACE logs -l app=demo-app"
    log "Logs de l'agent :  kubectl -n $NAMESPACE logs -l app=aiops-agent -f"
}
