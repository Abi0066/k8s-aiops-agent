"""Agent IA de diagnostic d'incidents Kubernetes.

Ce paquet contient :
  - `main.py`     : le service FastAPI (webhook + sonde de santé) ;
  - `models.py`   : les modèles Pydantic du payload Alertmanager (v4) ;
  - `collector.py` : la collecte des preuves (API K8s + Prometheus) ;
  - `llm.py`      : le prompt de diagnostic et l'appel à Ollama.
"""
