# Makefile — Point d'entrée unique du projet K8s AI Ops Agent.
#
# Le Makefile liste les actions possibles ; la logique vit dans scripts/*.sh.
# Ce découpage a deux avantages :
#   - le Makefile reste lisible d'un coup d'œil (c'est le "menu" du projet) ;
#   - les scripts sont réutilisables tels quels, par un recruteur qui n'a pas
#     `make` installé, ou depuis un README.
#
# Usage : make help

SHELL := /bin/bash
.DEFAULT_GOAL := help

CLUSTER_NAME ?= k8s-aiops
NAMESPACE    ?= aiops

.PHONY: help setup cluster-up cluster-down build demo-crash demo-oom \
        status logs ports clean-images agent-logs agent-health test lint validate \
        gitops-up gitops-sync gitops-status

## help : affiche cette aide
help:
	@echo ""
	@echo "  K8s AI Ops Agent — cibles disponibles"
	@echo ""
	@grep -E '^## ' $(MAKEFILE_LIST) | sed 's/^## /  /' | awk -F' : ' '{printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}'
	@echo ""
	@echo "  Démarrage rapide :"
	@echo "    make setup        # installe kind + kubectl"
	@echo "    make build        # construit les images"
	@echo "    make cluster-up   # crée le cluster, y charge les images, déploie tout"
	@echo "      ou make gitops-up  # idem, mais c'est Flux qui déploie depuis Git"
	@echo "    make demo-crash   # provoque un incident de démonstration"
	@echo ""

## setup : installe kind et kubectl dans ~/.local/bin (sans root)
setup:
	@./scripts/setup.sh

## cluster-up : crée le cluster kind et déploie les étapes 1 à 4
cluster-up:
	@./scripts/cluster-up.sh

## gitops-up : comme cluster-up, mais Flux déploie la stack depuis Git
# Pour suivre un fork ou une branche :
#   make gitops-up GITOPS_REPO_URL=https://github.com/<vous>/k8s-aiops-agent \
#                  GITOPS_BRANCH=ma-branche
# make transmet ces variables au script par l'environnement.
gitops-up:
	@./scripts/gitops.sh up

## gitops-sync : force Flux à appliquer le dernier commit, sans attendre
gitops-sync:
	@./scripts/gitops.sh sync

## gitops-status : état de la synchronisation Flux (révision appliquée)
gitops-status:
	@./scripts/gitops.sh status

## cluster-down : détruit le cluster et nettoie les ressources
cluster-down:
	@./scripts/cluster-down.sh

## build : construit les images (et les recharge si le cluster existe)
build:
	@./scripts/build-images.sh

## demo-crash : provoque un crash applicatif et affiche le diagnostic
demo-crash:
	@./scripts/demo.sh crash

## demo-oom : provoque un OOMKill et affiche le diagnostic
demo-oom:
	@./scripts/demo.sh oom

## status : état des pods et services du cluster
status:
	@echo "--- Nodes ---"
	@kubectl get nodes
	@echo ""
	@echo "--- Pods (namespace $(NAMESPACE)) ---"
	@kubectl -n $(NAMESPACE) get pods -o wide
	@echo ""
	@echo "--- Services ---"
	@kubectl -n $(NAMESPACE) get svc

## logs : logs de tous les pods du namespace
logs:
	@kubectl -n $(NAMESPACE) logs -l app=demo-app --tail=50 --all-containers --prefix

## agent-logs : logs de l'agent IA (suivi en continu)
agent-logs:
	@kubectl -n $(NAMESPACE) logs -l app=aiops-agent -f --prefix

## agent-health : état de l'agent et de ses dépendances (Ollama, Prometheus)
# /health répond 503 quand une dépendance est en panne : c'est justement là
# qu'on veut lire le détail. http.client renvoie le corps quel que soit le
# code, là où urllib.urlopen lèverait une exception sans l'afficher.
agent-health:
	@kubectl -n $(NAMESPACE) exec deploy/aiops-agent -- \
		python3 -c "import http.client,sys; \
		c = http.client.HTTPConnection('localhost', 8080, timeout=10); \
		c.request('GET', '/health'); r = c.getresponse(); \
		print(r.status, r.read().decode()); sys.exit(r.status != 200)"

## ports : affiche les URL d'accès aux services
ports:
	@echo "  Application : http://localhost:30090/health"
	@echo "  Prometheus  : http://localhost:30093"
	@echo "  Grafana     : http://localhost:30094  (admin / admin)"
	@echo ""
	@echo "  L'agent IA n'est PAS exposé sur l'hôte : il est interne au cluster."
	@echo "  Pour l'atteindre :"
	@echo "    kubectl -n $(NAMESPACE) port-forward svc/aiops-agent 8080:8080"
	@echo "    puis http://localhost:8080/docs (documentation interactive)"
	@echo ""

## test : lance les tests unitaires de l'agent (sans cluster)
test:
	@cd agent && python3 -m pytest -q

## lint : vérifie le code Python (agent et application) avec ruff
# Lancé depuis agent/ : l'application n'a pas de configuration ruff propre,
# elle hérite donc de agent/pyproject.toml.
lint:
	@cd agent && python3 -m ruff check . ../app

## validate : vérifie scripts, Dockerfiles, Prometheus, Alertmanager, manifests et Terraform
validate:
	@./scripts/validate.sh

## clean-images : comme cluster-down, et supprime aussi les images du projet
clean-images:
	@./scripts/cluster-down.sh --images
