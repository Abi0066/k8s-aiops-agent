#!/usr/bin/env bash
#
# validate.sh — Vérifie l'infrastructure du projet, sans cluster.
#
#   scripts shell              -> shellcheck
#   Dockerfiles                -> hadolint
#   Prometheus (config, règles d'alerte et leurs tests) -> promtool
#   Alertmanager               -> amtool
#   manifests Kubernetes       -> kubeconform (schéma de l'API, clés en double)
#   modules Terraform          -> terraform fmt + validate
#   workflows GitHub Actions   -> actionlint
#
# Chaque outil tourne dans un conteneur, à une version épinglée : rien à
# installer, et le même résultat en local (make validate) et dans la CI.
# Toutes les vérifications s'exécutent, puis le script échoue s'il y a eu au
# moins un échec : on voit tous les problèmes d'un coup.
#
set -euo pipefail
# shellcheck source=scripts/lib.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
cd "$REPO_ROOT"

detect_runtime

# promtool et amtool viennent des images déployées : on valide avec la
# version même qui lira ces fichiers dans le cluster.
PROMETHEUS_IMAGE="$(image_in monitoring/prometheus/prometheus.yaml)"
ALERTMANAGER_IMAGE="$(image_in monitoring/alertmanager/alertmanager-deployment.yaml)"
SHELLCHECK_IMAGE="docker.io/koalaman/shellcheck:v0.11.0"
HADOLINT_IMAGE="docker.io/hadolint/hadolint:v2.15.1"
KUBECONFORM_IMAGE="ghcr.io/yannh/kubeconform:v0.8.0"
TERRAFORM_IMAGE="docker.io/hashicorp/terraform:1.16.7"
ACTIONLINT_IMAGE="docker.io/rhysd/actionlint:1.7.12"

# Le dépôt est monté en lecture seule : aucun outil ne peut le modifier.
# label=disable : sous SELinux (Fedora), un conteneur n'a pas le droit de
# lire un répertoire personnel monté ; sans SELinux, l'option est ignorée.
run() {
    "$RUNTIME" run --rm --security-opt label=disable \
        -v "$REPO_ROOT:/src:ro" -w /src "$@"
}

FAILED=()
check() {
    local name="$1"
    shift
    log "--- $name"
    if "$@"; then
        log "OK : $name"
    else
        warn "ÉCHEC : $name"
        FAILED+=("$name")
    fi
}

# -x : suit le `source` de lib.sh, pour connaître ses variables et fonctions.
check "shellcheck" run "$SHELLCHECK_IMAGE" -x scripts/*.sh

check "hadolint" run "$HADOLINT_IMAGE" hadolint app/Dockerfile agent/Dockerfile

# --syntax-only : la configuration référence des fichiers qui n'existent que
# dans le pod (jeton et certificat du ServiceAccount).
check "promtool check config" run --entrypoint promtool "$PROMETHEUS_IMAGE" \
    check config --syntax-only monitoring/prometheus/config.yaml
check "promtool check rules" run --entrypoint promtool "$PROMETHEUS_IMAGE" \
    check rules monitoring/prometheus/rules/alerts.yaml
check "promtool test rules" run --entrypoint promtool "$PROMETHEUS_IMAGE" \
    test rules monitoring/prometheus/tests/alerts_test.yaml

check "amtool check-config" run --entrypoint amtool "$ALERTMANAGER_IMAGE" \
    check-config monitoring/alertmanager/alertmanager.yaml

# Les manifests Kubernetes sont les fichiers qui déclarent un `kind:`. Les
# autres YAML (configuration Prometheus, provisioning Grafana) n'en sont pas.
# -strict refuse les champs inconnus et les clés en double, qu'un
# `kubectl apply` ignorerait sans rien dire.
mapfile -t MANIFESTS < <(grep -rlE '^kind: ' manifests monitoring | sort)
check "kubeconform" run "$KUBECONFORM_IMAGE" -strict -summary "${MANIFESTS[@]}"

# terraform init écrit dans le répertoire (.terraform/) : on travaille sur
# une copie, à l'intérieur du conteneur. init télécharge le provider AWS
# (~100 Mo) ; -backend=false évite tout accès à un état distant.
check "terraform" run --entrypoint sh "$TERRAFORM_IMAGE" -c '
    set -e
    terraform fmt -check -recursive -diff terraform
    cp -r terraform /tmp/tf
    cd /tmp/tf/environments/dev
    terraform init -backend=false -input=false >/dev/null
    terraform validate'

# actionlint vérifie la syntaxe des workflows, les expressions ${{ }} et,
# avec le shellcheck embarqué dans l'image, les scripts des étapes `run:`.
check "actionlint" run "$ACTIONLINT_IMAGE" -no-color

echo
if [[ ${#FAILED[@]} -gt 0 ]]; then
    die "${#FAILED[@]} vérification(s) en échec : ${FAILED[*]}"
fi
log "Toutes les vérifications sont passées."
