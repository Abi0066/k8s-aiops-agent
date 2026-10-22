# Dockerfile de l'agent IA.
#
# Différences avec l'image de l'application de démo (app/Dockerfile) :
#   - le point d'entrée est uvicorn, pas python main.py ;
#   - on n'installe PAS les dépendances de l'application de démo ;
#   - le user non-root est NamedUser (Kubernetes 1.25+ le crée
#     automatiquement si le UID n'existe pas dans /etc/passwd).

FROM python:3.12-slim

# ---------------------------------------------------------------------------
# Utilisateur non-root
# ---------------------------------------------------------------------------
# Créer un utilisateur dédié est une bonne pratique de sécurité : le
# conteneur n'exécute pas le processus en root. Si un attaquant exploite
# une faille dans une dépendance, il n'obtient pas root dans le conteneur.
#
# On fixe un UID explicite (1000) : le même que l'app de démo, pour la
# cohérence du projet. Kubernetes peut monter un PVC seulement si le
# processus a les bons droits sur le répertoire monté (problème classique
# avec les volumes NFS et les groupes supplementationnaires).
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

RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r requirements.txt

# ---------------------------------------------------------------------------
# Code
# ---------------------------------------------------------------------------
COPY app/ ./app/

USER appuser

EXPOSE 8080

# ---------------------------------------------------------------------------
# Point d'entrée
# ---------------------------------------------------------------------------
# uvicorn est le serveur ASGI qui exécute l'application FastAPI.
#
# --host 0.0.0.0 est obligatoire : uvicorn n'écoute par défaut que sur
# 127.0.0.1, ce qui rendrait le service inaccessible depuis le cluster.
#
# --proxy-headers : le Service Kubernetes place l'IP réelle du client dans
# l'en-tête X-Forwarded-For. Sans cette option, les logs de l'agent
# montraient l'IP du proxy au lieu de celle d'Alertmanager.
#
# --workers 1 : un seul processus uvicorn. C'est le bon choix ici car les
# requêtes de diagnostic sont longues (minutes) : plusieurs workers
# multiplieraient la consommation mémoire (un modèle LLM chargé par
# worker) sans gain réel. Pour une démo mono-utilisateur, un worker suffit.
CMD ["uvicorn", "app.main:app", \
     "--host", "0.0.0.0", \
     "--port", "8080", \
     "--proxy-headers", \
     "--log-level", "info"]
