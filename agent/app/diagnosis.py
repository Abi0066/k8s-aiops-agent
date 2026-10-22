"""Diagnostic déterministe : décider ce qu'un `if` peut décider.

Pourquoi ce module existe
-------------------------
Pendant le développement, l'agent a conclu « OOMKilled » pour un crash
applicatif qui sortait avec le code 1. Les preuves envoyées au LLM
étaient pourtant correctes (`reason: Error`, `exitCode: 1`) : c'est le
modèle qui a recombiné la **limite mémoire** (une constante de
configuration, présente dans le prompt) avec la consigne « 137 = OOM »,
pour produire une histoire plausible mais fausse.

C'est un défaut classique des systèmes LLM : on leur demande un travail
qu'un code déterministe fait *exactement*, et ils se trompent de façon
spectaculaire mais crédible.

La règle de conception retenue :

    **Le code déterministe décide. Le LLM explique.**

Concrètement :
  - `diagnose()` produit un verdict fondé sur les champs structurés
    (raison d'arrêt, code de sortie) — pas sur du texte libre ;
  - le verdict et sa preuve sont joints au prompt ;
  - le LLM est chargé de REDIGER le diagnostic en français, de relier le
    verdict aux preuves, et de proposer une action — pas de deviner la
    cause.

Si le LLM contredit le verdict, le verdict fait foi. C'est ce qui rend
le système fiable : la partie qui peut halluciner est cantonnée à la
partie qui peut être fausse sans conséquence.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Dict, Optional

log = logging.getLogger(__name__)


@dataclass
class Verdict:
    """Le verdict déterministe sur un incident.

    `code` est une catégorie stable, utilisée par l'étape 4 pour choisir
    l'action corrective. `evidence` est le champ structuré qui a permis
    de décider — c'est la preuve que le verdict n'est pas une devinette.
    """

    # Catégorie de l'incident.
    code: str

    # Cause racine, formulée à partir des champs structurés.
    cause: str

    # Le champ qui a permis de décider (ex. "exitCode=1").
    evidence: str

    # Action corrective recommandée (sera appliquée en étape 4).
    recommended_action: Optional[str] = None

    # Le niveau de confiance du code, pas du LLM.
    confidence: str = "high"


# Exit codes Kubernetes : la convention est 128 + numéro de signal.
#
#   137 = 128 + 9  = killed par SIGKILL
#   143 = 128 + 15 = arrêté par SIGTERM (arrêt propre, demandé par Kubernetes)
#   1   = le process a quitté lui-même avec une erreur
#
# La règle « 137 = OOMKilled » est une CONVENTION, pas une garantie : un
# SIGKILL peut aussi venir d'un `kill -9` ou d'un éviction. On ne peut
# l'affirmer que si Kubernetes confirme `reason: OOMKilled`.
EXIT_CODE_SIGKILL = 137
EXIT_CODE_SIGTERM = 143


def diagnose(
    last_termination: Dict[str, Any],
    pod_status: Optional[Dict[str, Any]] = None,
) -> Optional[Verdict]:
    """Déduit la cause racine à partir des champs structurés.

    On regarde, dans cet ordre de confiance décroissante :

    1. **`reason`** — la raison annoncée par Kubernetes (`OOMKilled`,
       `Error`, `ContainerStatusUnknown`...). C'est l'autorité : c'est
       Kubernetes qui a écrit « OOMKilled ».
    2. **`exitCode`** — le code de sortie, qui confirme ou nuance la
       raison (`137` = SIGKILL, `1` = erreur applicative).
    3. **`state.reason`** — le motif d'attente du conteneur
       (`CrashLoopBackOff`, `ImagePullBackOff`...), qui explique le
       comportement courant plutôt que la cause.

    On ne retourne `None` que si on n'a vraiment aucune information : dans
    ce cas, l'agent laisse le LLM работа avec les preuves brutes, mais en
    lui signalant explicitement qu'aucun verdict déterministe n'a pu être
    posé.
    """
    if not last_termination:
        return None

    reason = last_termination.get("reason")
    exit_code = last_termination.get("exitCode")
    signal = last_termination.get("signal")
    restart_count = last_termination.get("restartCount")
    message = last_termination.get("message")

    # --- Cas 1 : OOMKilled --------------------------------------------
    if reason == "OOMKilled":
        return Verdict(
            code="OOMKilled",
            cause=(
                "Le kernel a tué le conteneur car il a dépassé sa limite "
                "de mémoire (OOMKilled)."
            ),
            evidence=f"reason=OOMKilled, exitCode={exit_code}, signal={signal}",
            recommended_action=(
                "Augmenter la limite de mémoire (resources.limits.memory) "
                "du conteneur, ou réduire la consommation de l'application."
            ),
            confidence="high",
        )

    # --- Cas 2 : SIGKILL sans confirmation d'OOM ----------------------
    # Kubernetes dit OOMKilled OU le conteneur a été tué par SIGKILL sans
    # que le kernel confirme un dépassement mémoire.
    if exit_code == EXIT_CODE_SIGKILL and reason != "Error":
        return Verdict(
            code="SIGKILL",
            cause=(
                "Le conteneur a été tué par SIGKILL (signal 9). C'est la "
                "signature d'un dépassement mémoire, mais Kubernetes n'a "
                "pas explicitement confirmé OOMKilled."
            ),
            evidence=f"exitCode={exit_code} (128+9=SIGKILL), reason={reason}",
            recommended_action=(
                "Vérifier la consommation mémoire (container_memory_working_set_bytes) "
                "puis augmenter la limite de mémoire si elle est proche du plafond."
            ),
            confidence="medium",
        )

    # --- Cas 3 : erreur applicative (exit code 1) --------------------
    # C'est le cas du scénario /crash : le process appelle os._exit(1).
    # Kubernetes le signale `Reason: Error, Exit Code: 1`.
    if reason == "Error" or (exit_code is not None and exit_code == 1):
        return Verdict(
            code="ApplicationError",
            cause=(
                "Le processus applicatif s'est terminé lui-même en erreur "
                "(le conteneur a quitté avec le code 1). Ce n'est PAS un "
                "dépassement mémoire : le kernel n'a pas tué le processus."
            ),
            evidence=f"reason={reason}, exitCode={exit_code}, signal={signal}",
            recommended_action=(
                "Analyser les logs de l'application pour identifier l'erreur "
                "qui a provoque la sortie du processus."
            ),
            confidence="high",
        )

    # --- Cas 4 : arrêt propre par SIGTERM ----------------------------
    if exit_code == EXIT_CODE_SIGTERM or reason == "Completed":
        return Verdict(
            code="GracefulStop",
            cause=(
                "Le conteneur a été arrêté proprement (SIGTERM / exit 0-143). "
                "Il s'agit d'un arrêt demandé, pas d'une panne."
            ),
            evidence=f"reason={reason}, exitCode={exit_code}",
            recommended_action=None,
            confidence="high",
        )

    # --- Cas 5 : le conteneur est en attente -------------------------
    # CrashLoopBackOff, ImagePullBackOff, ErrImagePull...
    waiting_reason = reason
    if pod_status:
        for container in pod_status.get("containers", []):
            state_reason = container.get("stateReason")
            if state_reason and state_reason != "Running":
                waiting_reason = state_reason
                break

    if waiting_reason in (
        "CrashLoopBackOff",
        "ImagePullBackOff",
        "ErrImagePull",
        "CreateContainerConfigError",
        "InvalidImageName",
    ):
        action = {
            "CrashLoopBackOff": (
                "Examiner les logs du conteneur précédent pour identifier "
                "l'erreur qui tue le processus à chaque démarrage."
            ),
            "ImagePullBackOff": (
                "Vérifier le nom et le tag de l'image, et les droits du "
                "Secret d'image (imagePullSecrets)."
            ),
        }.get(
            waiting_reason,
            "Examiner les événements du namespace pour comprendre la cause.",
        )
        return Verdict(
            code=waiting_reason,
            cause=(
                f"Le conteneur est en attente de redémarrage ({waiting_reason}). "
                f"Il s'est arrêté {restart_count} fois."
                if restart_count
                else f"Le conteneur est en attente de redémarrage ({waiting_reason})."
            ),
            evidence=f"state.reason={waiting_reason}, restartCount={restart_count}",
            recommended_action=action,
            confidence="medium",
        )

    # --- Cas 6 : on ne sait pas --------------------------------------
    log.info(
        "Aucun verdict déterministe pour reason=%s exitCode=%s",
        reason,
        exit_code,
    )
    return None


def format_verdict_for_prompt(verdict: Optional[Verdict]) -> str:
    """Présente le verdict au LLM, sous une forme contraignante.

    Le LLM reçoit le verdict ET l'interdiction de le contredire. Sans
    cette consigne, un modèle à 3 milliards de paramètres a une forte
    tendance à « améliorer » la réponse — et à se tromper.
    """
    if verdict is None:
        return (
            "VERDICT DETERMINISTE : aucun.\n"
            "Aucun verdict n'a pu être pose a partir des champs structurees "
            "(raison d'arret, code de sortie). Ne deduis PAS de cause a partir "
            "des limites de ressources : elles decrivent la CONFIGURATION "
            "voulue, pas le symptome observe. Si les preuves sont "
            "insuffisantes, dis-le explicitement."
        )

    action = verdict.recommended_action or (
        "Aucune action corrective automatique identifiee."
    )
    return (
        f"VERDICT DETERMINISTE : {verdict.code}\n"
        f"CAUSE RETENUE : {verdict.cause}\n"
        f"PREUVE DECISIVE : {verdict.evidence}\n"
        f"ACTION RECOMMANDEE : {action}\n"
        f"NIVEAU DE CONFIANCE : {verdict.confidence}\n"
        "\n"
        "CONTRAINTE IMPORTANTE : cette cause est etablie par les champs "
        "structures ci-dessus. Rediges-la en français, en 3 phrases "
        "(cause, preuve, action), sans la contredire et sans en inventer "
        "une autre. Ne deduis JAMAIS une cause a partir des limites de "
        "ressources : une limite de memoire de 128Mi est une valeur de "
        "configuration, elle ne prouve pas un depassement de memoire. "
        "Seul un code de sortie 137 ou une raison 'OOMKilled' le prouve."
    )
