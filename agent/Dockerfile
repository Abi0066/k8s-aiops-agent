# Dockerfile de l'agent IA.
#
# Différences avec l'image de l'application de démo (app/Dockerfile) :
#   - le point d'entrée est uvicorn, pas python main.py ;
#   - on n'installe PAS les dépendances de l'application de démo ;
#   - le code est un paquet (app/), pas un fichier unique.

FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# ---------------------------------------------------------------------------
# Utilisateur non-root
# ---------------------------------------------------------------------------
# Créer un utilisateur dédié est une bonne pratique de sécurité : le
# conteneur n'exécute pas le processus en root. Si un attaquant exploite
# une faille dans une dépendance, il n'obtient pas root dans le conteneur.
#
# On fixe un UID explicite (1000) : le même que l'app de démo, pour la
# cohérence du projet. Un UID fixe rend aussi les droits prévisibles le jour
# où l'on monte un volume (fsGroup, propriétaire des fichiers).
RUN groupadd --gid 1000 appuser \
    && useradd --uid 1000 --gid 1000 --no-create-home --shell /usr/sbin/nologin appuser

WORKDIR /app

# ---------------------------------------------------------------------------
# Dépendances
# ---------------------------------------------------------------------------
# On copie d'abord les requirements.txt seul, puis le code. C'est le
# "layer caching" de Docker : tant que les dépendances ne changent pas,
# la couche pip est réutilisée au lieu d'être reconstruite. Sur un
# projet qui itère beaucoup sur le code, ça fait gagner beaucoup de temps.
COPY requirements.txt .

# Pas de `pip install --upgrade pip` : la version de pip ne serait plus
# figée, et deux builds du même commit pourraient différer.
RUN pip install --no-cache-dir -r requirements.txt

# ---------------------------------------------------------------------------
# Code
# ---------------------------------------------------------------------------
COPY app/ ./app/

# UID numérique : `runAsNonRoot: true` (voir manifests/aiops-agent.yaml)
# exige que le kubelet puisse vérifier que l'utilisateur n'est pas root.
USER 1000:1000

EXPOSE 8080

# ---------------------------------------------------------------------------
# Point d'entrée
# ---------------------------------------------------------------------------
# uvicorn est le serveur ASGI qui exécute l'application FastAPI.
#
# --host 0.0.0.0 est obligatoire : uvicorn n'écoute par défaut que sur
# 127.0.0.1, ce qui rendrait le service inaccessible depuis le cluster.
#
# Un seul processus uvicorn (le défaut, sans --workers) : l'anti-doublon des
# alertes vit en mémoire dans le process. Avec plusieurs workers, chacun
# aurait sa propre mémoire et la même alerte pourrait être analysée deux
# fois. Le modèle, lui, tourne dans Ollama : il n'est pas chargé par worker.
#
# Pas de --proxy-headers : un Service Kubernetes travaille au niveau TCP et
# n'ajoute aucun en-tête X-Forwarded-For. Il n'y a pas de proxy HTTP devant
# l'agent.
CMD ["uvicorn", "app.main:app", \
     "--host", "0.0.0.0", \
     "--port", "8080", \
     "--log-level", "info"]
