"""Tests du verdict déterministe.

Ces tests figent la règle « le code décide » : pour chaque combinaison
(reason, exitCode) observée en pratique, un seul verdict est acceptable.
"""

import pytest

from app.diagnosis import diagnose, format_verdict_for_prompt


def term(reason=None, exit_code=None, **extra):
    """Construit un `last_termination` comme le fait le collecteur."""
    return {"reason": reason, "exitCode": exit_code, "restartCount": 4, **extra}


@pytest.mark.parametrize(
    ("last_termination", "expected"),
    [
        # Scénario /oom : le kernel confirme.
        (term("OOMKilled", 137), "OOMKilled"),
        # SIGKILL sans confirmation d'OOM. containerd rapporte souvent
        # `reason: Error` : ce n'est PAS une erreur applicative.
        (term(None, 137), "SIGKILL"),
        (term("Error", 137), "SIGKILL"),
        # Scénario /crash.
        (term("Error", 1), "ApplicationError"),
        # Autres codes applicatifs : 2 (usage), 127 (commande introuvable).
        (term("Error", 2), "ApplicationError"),
        (term("Error", 127), "ApplicationError"),
        # Arrêts propres, y compris un SIGTERM rapporté comme `Error`.
        (term("Completed", 0), "GracefulStop"),
        (term("Error", 143), "GracefulStop"),
    ],
)
def test_verdict_from_termination(last_termination, expected):
    verdict = diagnose(last_termination)
    assert verdict is not None
    assert verdict.code == expected


def test_application_error_reports_real_exit_code():
    verdict = diagnose(term("Error", 127))
    assert "code 127" in verdict.cause
    assert "code 1)" not in verdict.cause


def test_waiting_state_without_exit_code():
    # Ce que renvoie extract_last_termination quand aucun arrêt n'est
    # encore enregistré mais que le conteneur attend.
    verdict = diagnose({"reason": "ImagePullBackOff", "restartCount": 0, "state": "en attente"})
    assert verdict.code == "ImagePullBackOff"
    assert "imagePullSecrets" in verdict.recommended_action


def test_waiting_reason_read_from_pod_status():
    pod_status = {"containers": [{"stateReason": "CrashLoopBackOff"}]}
    verdict = diagnose({"reason": None, "restartCount": 7}, pod_status)
    assert verdict.code == "CrashLoopBackOff"
    assert "7 fois" in verdict.cause


@pytest.mark.parametrize("last_termination", [{}, None])
def test_no_information_gives_no_verdict(last_termination):
    assert diagnose(last_termination) is None


def test_prompt_block_without_verdict_warns_llm():
    block = format_verdict_for_prompt(None)
    assert "aucun" in block
    assert "limites de ressources" in block


def test_prompt_block_carries_verdict():
    block = format_verdict_for_prompt(diagnose(term("OOMKilled", 137)))
    assert "VERDICT DETERMINISTE : OOMKilled" in block
    assert "resources.limits.memory" in block
