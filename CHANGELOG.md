# Changelog

Les changements notables du projet, du plus récent au plus ancien. Le projet
n'a pas de numéro de version : chaque entrée correspond à une pull request
fusionnée dans `main`. Le format s'inspire de
[Keep a Changelog](https://keepachangelog.com/fr/1.1.0/).

## 2026-10-24 : GIF de démonstration et dernier diagnostic

Poussé directement sur `main`, sans pull request.

### Ajouté
- `docs/screenshots/demo.gif` : quatre crashes, l'état du pod, puis le verdict
  de l'agent. Session réelle enregistrée avec `asciinema`, accélérée et
  aplatie sur le fond du thème.
- `scripts/last-analysis.sh` et `make last-analysis` : extraient le dernier
  `ANALYSIS_RESULT` et n'affichent que le verdict, la remédiation et la durée.

## 2026-10-24 : captures d'écran et lisibilité du dashboard

Poussé directement sur `main`, sans pull request.

### Ajouté
- `docs/screenshots/` : tableau de bord Grafana, alertes Prometheus et extrait
  d'`ANALYSIS_RESULT` en dry-run, rassemblés dans une section « Aperçu » du
  README.

### Corrigé
- Grafana : le panneau « Mémoire du pod » affichait `27 MiB` en rouge faute de
  paliers ; il a désormais des seuils vert/orange/rouge à 100 et 128 MiB.
- Grafana : la mémoire du panneau CPU/mémoire passe sur l'axe de droite en
  unités lisibles (MiB) au lieu d'octets bruts.
- Grafana : trois titres raccourcis pour ne plus être tronqués à 1024 px de
  large.

## 2026-10-24 : revue du README

Poussé directement sur `main`, sans pull request.

### Ajouté
- Sommaire et badges des trois workflows.
- Section « Configuration de l'agent » : toutes les variables, leurs défauts
  et le comportement en cas de valeur invalide.
- Section « Dépannage » : symptôme → cause probable → action, avec les pièges
  réellement rencontrés (image privée, inotify, alerte déjà `firing`).
- Licence MIT (`LICENSE`).

### Corrigé
- `make build` n'est plus présenté comme obligatoire : les images viennent de
  GHCR et `make build` est le repli hors ligne.
- Le schéma de séquence déclarait implicitement le participant `K8s` ; il est
  maintenant explicite.
- Prérequis : accès réseau à GHCR et espace disque ajoutés.
- Fork : les noms d'images GHCR sont codés en dur et doivent être remplacés.
- Limites : Terraform renvoyé à une étape 7, l'étape 6 étant terminée.
- `CHANGELOG` : les entrées ne correspondent plus uniquement à des PR.

## 2026-10-24 : CI/CD des images (étape 6)

Poussé directement sur `main`, sans pull request.

### Ajouté
- `.github/workflows/images-release.yml` : construit demo-app et l'agent,
  publie dans GHCR sous un tag immuable `sha-<commit>`, puis commite les
  nouveaux tags dans `manifests/`. Flux applique ce commit et le kubelet tire
  les images du registre.
- Packages publics `ghcr.io/abi0066/k8s-aiops-demo-app` et
  `ghcr.io/abi0066/k8s-aiops-agent`.
- `/health` renvoie le nom du pod : un rollout devient visible de l'extérieur.

### Modifié
- Manifests : noms GHCR et `imagePullPolicy: IfNotPresent` ; `make build`
  devient un repli hors ligne au lieu d'être obligatoire.
- `scripts/lib.sh` : les images du projet ne sont plus comptées comme tierces,
  et leur absence en local n'est plus bloquante.
- README : section CI/CD, justification du commit de bump, limites actualisées.

### Corrigé
- `wait_for_pods` attendait les pods `Completed` : un pod terminé n'étant
  jamais `Ready`, chaque déploiement affichait un faux avertissement.

## 2026-10-24 : documentation des étapes 3 à 5

Poussé directement sur `main`, sans pull request.

### Modifié
- README : en-tête à jour, nuance sur inotify, mesures GitOps (dérive corrigée
  en 311 s, `prune` prouvé) et table des limites connues.
- « étapes 1 à 3 » corrigé en « 1 à 4 » dans le README, le Makefile,
  `scripts/cluster-up.sh` et `deploy/kustomization.yaml`.

## 2026-10-23 : auto-guérison des OOMKilled (étape 4) — [#5](https://github.com/Abi0066/k8s-aiops-agent/pull/5)

### Ajouté
- `agent/app/remediation.py` : quand le verdict est un OOMKilled certain,
  l'agent augmente la memory limit du Deployment propriétaire du pod. Le code
  décide, le LLM ne fait que raconter l'action.
- Garde-fous : désactivé par défaut (`AUTOHEAL_ENABLED`), dry-run par défaut
  (`AUTOHEAL_DRY_RUN`, patch validé par le serveur sans être enregistré),
  plafond `AUTOHEAL_MEMORY_MAX` (512Mi), une action par Deployment et par
  heure (`AUTOHEAL_COOLDOWN_S`).
- RBAC `get`/`patch` limité au seul Deployment `demo-app`.
- Alerte `DemoAppOOMKilled` : un seul OOMKill suffit à la déclencher.
- Clé `remediation` dans `ANALYSIS_RESULT` (`applied`, `dry-run`, `skipped`,
  `disabled` ou `failed`, avec la raison).
- 22 tests sans cluster (`agent/tests/test_remediation.py`).

### Modifié
- Le manifest de l'agent active l'auto-guérison en dry-run ; passer en réel
  reste une décision humaine.
- `/oom` alloue 120 Mio : le pod est tué à 128Mi et répond 200 à 192Mi.
- Images : agent 1.1.0, app de démo 1.0.4.
- README : section sur l'auto-guérison et limite inotify requise sur kind.

## 2026-10-23 : GitOps avec Flux (étape 5) — [#4](https://github.com/Abi0066/k8s-aiops-agent/pull/4)

### Ajouté
- Déploiement de toute la stack par Flux v2.9.6, en installation minimale
  (`source-controller` et `kustomize-controller`).
- `deploy/kustomization.yaml` : agrège `manifests/` et `monitoring/`. Les
  ConfigMaps sont générées depuis Git (`configMapGenerator`) ; un changement
  de configuration redéploie le pod concerné.
- `deploy/flux-sync.yaml` : GitRepository et Kustomization (prune, correction
  de la dérive toutes les 5 minutes).
- `make gitops-up`, `make gitops-sync`, `make gitops-status` ;
  `GITOPS_REPO_URL` et `GITOPS_BRANCH` pour suivre un fork ou une branche.

### Modifié
- `cluster-up.sh` applique `kubectl apply -k deploy` et prévient si Flux gère
  déjà le cluster.
- `make validate` valide le rendu `kustomize build deploy` et les objets Flux.

## 2026-10-23 : collecte des ressources des conteneurs — [#6](https://github.com/Abi0066/k8s-aiops-agent/pull/6)

### Corrigé
- Le collecteur plantait quand `containerStatuses[].resources` était absent
  (Kubernetes avant 1.33, conteneur pas encore démarré) : l'analyse
  s'interrompait sans diagnostic. Il se replie maintenant sur la spec du pod.
- Les requests et limits étaient toujours vides dans les preuves : le client
  Kubernetes les renvoie sous forme de dict, lu par erreur avec `getattr`.

## 2026-10-23 : audit qualité — [#3](https://github.com/Abi0066/k8s-aiops-agent/pull/3)

### Corrigé
- Les requêtes PromQL et les événements ciblent le pod de l'alerte, et non
  plus tout le namespace. Le namespace et le nom du pod sont échappés dans
  PromQL.
- L'app de démo s'arrête proprement sur SIGTERM (0,3 s, code 0). Avant : 10 s
  puis SIGKILL et code 137, celui d'un OOM.
- Terraform : outputs déclarés deux fois, `terraform validate` échouait.

### Ajouté
- Collecte en parallèle et timeouts sur chaque appel à l'API Kubernetes et à
  Prometheus.
- Anti-doublon : une alerte déjà analysée dans `DEDUP_WINDOW_S` (10 minutes)
  ne l'est pas une seconde fois.
- Preuve `memory_working_set_max_15m` (pic mémoire réel).
- `make validate` (shellcheck, hadolint, promtool, amtool, kubeconform,
  terraform, actionlint, chacun dans un conteneur épinglé) et workflow
  `infra-ci` ; tests unitaires des règles d'alerte.

### Sécurité
- Tous les pods en Pod Security Standards « restricted », sauf deux
  exceptions documentées (Grafana, Ollama).
- Prometheus vérifie le certificat de l'API server et ne tourne plus en root.
- RBAC de kube-state-metrics réduit aux pods.

### Modifié
- `scripts/lib.sh` commun ; images chargées dans kind par archive, sous
  podman comme sous docker (fin de `docker-podman-shim.sh`).
- `setup.sh` vérifie le SHA-256 de kind et kubectl et prend en charge arm64.
- Ollama épinglé en 0.40.8, modèle défini une seule fois dans un ConfigMap.
- README : tests et vérifications, conteneurs sans privilèges, corrections.

## 2026-10-22 : corrections du diagnostic, tests et CI — [#2](https://github.com/Abi0066/k8s-aiops-agent/pull/2)

### Corrigé
- `diagnosis.py` donnait un verdict erroné pour certains couples
  `reason` / `exitCode`.
- Hors cluster, la configuration Kubernetes n'était jamais chargée
  (`load_kube_config()` non awaitée).
- Plantage sur un pod sans label.
- `ANALYSIS_RESULT` loguait le namespace de l'alerte (souvent vide) au lieu
  du namespace analysé.

### Ajouté
- Tests pytest sans cluster (`agent/tests/`) et workflow `agent-ci`
  (ruff + pytest) ; `make test` et `make lint`.

### Modifié
- `@app.on_event`, déprécié, remplacé par `lifespan` ; le client Kubernetes
  est fermé à l'arrêt.
- Documentation alignée sur le modèle réellement déployé,
  `qwen2.5-coder:14b`.

## 2026-10-22 : version initiale (étapes 1 à 3)

Cluster kind, application de démonstration, Prometheus, Alertmanager,
Grafana et agent de diagnostic avec Ollama.
