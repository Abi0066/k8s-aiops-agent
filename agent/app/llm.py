"""Appel au modèle de langage local (Ollama).

Ce module contient deux choses :

  1. le **prompt** — la politique de diagnostic de l'agent ;
  2. l'appel HTTP à l'API `/api/chat` d'Ollama.

Séparer le prompt du code de collecte est un choix délibéré : la politique
de l'agent (« réponds en 3 phrases, cite ta preuve ») est un texte, pas de
la logique. On peut la relire, la discuter, la modifier sans toucher au
code de collecte des preuves.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Le prompt système : la politique de diagnostic
# ---------------------------------------------------------------------------
# Ce message système est la consigne permanente donnée au modèle. Il
# contient :
#   - le rôle du modèle (analyste d'incidents SRE) ;
#   - le format de réponse attendu (concision) ;
#   - la règle la plus importante : ne jamais inventer, s'appuyer sur les
#     preuves fournies ;
#   - une consigne explicite sur le non diagnostic d'alertes résolues.
SYSTEM_PROMPT = """\
Tu es un analyste d'incidents Kubernetes (SRE) experimenté. On te fournit des \
preuves brutes extraites d'un cluster Kubernetes : état du pod, logs, \
événements, métriques Prometheus.

Ta tâche : identifier la cause racine la plus probable de l'incident.

Règles impératives :
1. Appuie CHAQUE affirmation sur une preuve présente dans les données. Cite \
la preuve (raison d'arrêt, code de sortie, événement, métrique).
2. N'invente JAMAIS une cause qui n'apparaît pas dans les preuves. Si les \
preuves sont insuffisantes, dis-le explicitement.
3. Réponds en 3 phrases maximum : (a) la cause racine, (b) la preuve \
déterminante, (c) l'action corrective recommandée.
4. Utilise les termes Kubernetes exacts : OOMKilled, CrashLoopBackOff, \
ImagePullBackOff, exit code 137, etc.
5. Réponds en français.

Rappel sur les exit codes : 137 = 128 + 9 = killed par SIGKILL (typiquement \
OOMKill). 1 = erreur applicative. 143 = 128 + 15 = SIGTERM (arrêt propre).\
"""


def build_user_prompt(context: str, verdict_block: str) -> str:
    """Construit le message « user » : le verdict, puis les preuves.

    Le verdict déterministe vient EN PREMIER. C'est délibéré : le modèle
    lit le contexte de haut en bas, et placer la conclusion avant les
    données brutes rend l'ancrage nettement plus fiable.

    On sépare clairement l'instruction (message système) des données
    (message user). C'est ce qui permet de changer la politique sans
    toucher au code de collecte.
    """
    return (
        f"{verdict_block}\n\n"
        "Voici les preuves brutes qui ont servi a etablir ce verdict. "
        "Rediges le diagnostic final en francais, en 3 phrases maximum : "
        "cause racine, preuve determinante, action corrective.\n\n"
        f"{context}"
    )


# ---------------------------------------------------------------------------
# Appel à l'API Ollama
# ---------------------------------------------------------------------------
async def call_ollama(
    http_client,
    ollama_url: str,
    model: str,
    context: str,
    verdict_block: str,
    timeout: float = 300.0,
) -> Dict[str, Any]:
    """Appelle Ollama en mode `/api/chat` et renvoie la réponse.

    L'API `/api/chat` attend :

        {
          "model": "qwen2.5-coder:14b",
          "messages": [{"role": "system", ...}, {"role": "user", ...}],
          "stream": false,
          "options": {"temperature": 0.1, "num_ctx": 4096}
        }

    et renvoie :

        {
          "model": "qwen2.5-coder:14b",
          "message": {"role": "assistant", "content": "..."},
          "done": true,
          "total_duration": 12345678
        }

    Trois paramètres non-évidents :

    - **`stream: false`** — par défaut, Ollama diffuse la réponse token par
      token (Server-Sent Events). On veut la réponse complète d'un bloc.

    - **`temperature: 0.1`** — entre 0 (déterministe) et 2 (créatif). Pour
      un diagnostic, la reproductibilité est une qualité : la même alerte
      doit produire la même analyse. À 0.7 (défaut), deux exécutions sur
      les mêmes preuves peuvent donner des conclusions différentes — donc
      des-actions différentes en étape 4.

    - **`num_ctx`** — taille de la fenêtre de contexte. 4096 tokens est la
      valeur par défaut de qwen2.5-coder:14b. Les preuves sont déjà tronquées en
      amont (module `collector`) pour tenir dans cette limite.

    Le timeout est long (300 s) : un modèle 3B sur CPU peut prendre 30 à
    90 s pour la première inférence (chargement en mémoire), et le
    téléchargement du modèle au premier démarrage peut en prendre bien
    plus. Le `num_predict` limite la longueur générée.
    """
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": build_user_prompt(context, verdict_block)},
        ],
        "stream": False,
        "options": {
            "temperature": 0.1,
            "num_ctx": 4096,
            "num_predict": 300,
        },
    }

    try:
        response = await http_client.post(
            f"{ollama_url}/api/chat", json=payload, timeout=timeout
        )
        response.raise_for_status()
        data = response.json()
    except Exception as exc:  # noqa: BLE001
        log.error("Appel Ollama en échec : %s", exc)
        return {
            "ok": False,
            "error": str(exc),
            "analysis": None,
            "duration_s": None,
        }

    content = (data.get("message") or {}).get("content", "")

    # `total_duration` est en nanosecondes chez Ollama.
    total_duration = data.get("total_duration")
    duration_s = round(total_duration / 1_000_000_000, 2) if total_duration else None

    log.info(
        "Réponse Ollama reçue en %ss (%d caractères)",
        duration_s,
        len(content),
    )

    return {
        "ok": True,
        "error": None,
        "analysis": content.strip(),
        "duration_s": duration_s,
        "model": data.get("model", model),
        "eval_count": data.get("eval_count"),
    }


async def check_ollama_health(http_client, ollama_url: str) -> Dict[str, Any]:
    """Vérifie qu'Ollama répond et liste les modèles disponibles.

    L'API `/api/tags` renvoie la liste des modèles téléchargés. Cette
    information est DOUBLE : elle sert au `/health` de l'agent (l'agent
    est-il opérationnel ?) et elle permet de distinguer « Ollama est mort »
    de « le modèle n'est pas encore téléchargé » — deux pannes très
    différentes.
    """
    try:
        response = await http_client.get(f"{ollama_url}/api/tags", timeout=10.0)
        response.raise_for_status()
        data = response.json()
        models = [m.get("name") for m in data.get("models", [])]
        return {"ok": True, "models": models, "error": None}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "models": [], "error": str(exc)}
