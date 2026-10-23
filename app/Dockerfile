# Dockerfile de l'application de démonstration.
#
# Image de base slim : plus petite à tirer et à scanner (Trivy, étape 5).
# python:3.12-slim suffit pour ce projet — pas besoin de compilateur.

FROM python:3.12-slim

# PYTHONUNBUFFERED : les logs sortent immédiatement, sans attendre qu'un
# tampon se remplisse — indispensable pour `kubectl logs` en direct.
# PYTHONDONTWRITEBYTECODE : pas de fichiers .pyc ; /app n'est de toute façon
# pas modifiable par l'utilisateur qui exécute le process.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# On ne lance pas le process en root : le runtime Kubernetes peut le refuser
# via PodSecurityContext, et un process non-root réduit la surface d'attaque.
RUN useradd --no-create-home --uid 1000 --shell /usr/sbin/nologin appuser

WORKDIR /app

# Les dépendances sont installées dans une couche séparée du code source.
# Docker réutilise cette couche tant que requirements.txt ne change pas :
# reconstruire l'image après une modif de main.py est donc instantané.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY main.py .

# Le code reste la propriété de root : appuser peut le lire et l'exécuter,
# mais pas le modifier. Un `chown -R` ici dupliquerait en plus tous les
# fichiers dans une nouvelle couche de l'image.
#
# L'UID est numérique (et non `USER appuser`) : avec `runAsNonRoot: true`,
# le kubelet refuse de démarrer un conteneur dont il ne peut pas vérifier
# que l'utilisateur n'est pas root, ce qu'un nom ne permet pas.
USER 1000:1000

EXPOSE 8000

# Serveur HTTP : waitress, et c'est un choix de SCÉNARIO, pas de performance.
#
# Dans un conteneur, le PID 1 est le process que le kubelet surveille : quand
# il meurt, le conteneur s'arrête et Kubernetes redémarre le pod.
#
# gunicorn est un superviseur : le PID 1 est le master, et les workers sont des
# enfants. Or notre endpoint /crash appelle os._exit(1) pour simuler un crash
# applicatif. Avec gunicorn, cela ne tuerait QUE le worker ayant servi la
# requête ; le master constaterait sa mort et en démarrerait un autre. Le
# conteneur ne mourrait jamais, le pod ne redémarrerait pas, et l'incident
# serait totalement invisible -- c'est exactement ce qui a été observé pendant
# le développement de ce projet.
#
# waitress est mono-processus : le process Python EST le PID 1. os._exit(1)
# tue donc directement le PID 1, le conteneur s'arrête avec le code de sortie 1,
# et Kubernetes le redémarre en CrashLoopBackOff avec :
#     Last State:  Terminated
#       Reason:    Error
#       Exit Code: 1
#
# Note : os.kill(1, signal.SIGKILL) ne conviendrait PAS. Le noyau Linux ignore
# les signaux sans handler envoyés au PID 1 d'un namespace, et SIGKILL ne peut
# pas être intercepté : le signal serait silencieusement perdu.
#
# Sur un vrai déploiement, on ferait l'inverse (plusieurs workers, pour qu'un
# bad request ne tue pas le serveur). Ici on veut au contraire que la panne
# soit visible au niveau du pod, puisque c'est le pod que l'agent IA doit
# diagnostiquer.
CMD ["python", "main.py"]
