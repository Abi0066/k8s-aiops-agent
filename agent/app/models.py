"""Modèles du payload Alertmanager (format v4).

Pourquoi ce fichier existe
--------------------------
Alertmanager envoie un JSON dont la structure est définie par un contrat
public : le format « v4 ». La bibliothèque `alertmanager` de la communauté
en donne la spécification. Reproduire ces modèles en Pydantic permet :

  1. de **valider** le payload — un champ manquant ou mal typé est rejeté
     proprement (HTTP 400) au lieu de faire planter l'agent au milieu du
     traitement ;
  2. de **documenter** le contrat dans le code, avec des annotations ;
  3. de ne dépendre d'aucune librairie tierce.

Deux différences par rapport à la spécification Alertmanager :
    - On n'est pas *strict* sur les champs inconnus (`extra="ignore"`) :
      Alertmanager peut ajouter des champs à l'avenir, on ne veut pas
      casser la réception d'un webhook à cause d'un champ supplémentaire.
    - Les timestamps sont gardés comme chaînes brutes : l'agent n'a pas
      besoin de raisonner sur les dates, seulement de les transmettre au
      LLM comme preuves.
"""

from __future__ import annotations

from typing import Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field


class Alert(BaseModel):
    """Une alerte individuelle dans le tableau `alerts[]`."""

    # `extra="ignore"` : on tolère les champs que nous ne connaissons pas.
    model_config = ConfigDict(extra="ignore")

    # "firing" (alerte active) ou "resolved" (alerte résolue).
    # On ne diagnostique que les "firing" : une alerte résolue est un
    # historique, pas un incident en cours.
    status: str = Field(default="firing")

    # Les labels : paires clé/valeur indexées (alertname, severity,
    # namespace, component...). C'est ici qu'on lit le type d'incident.
    labels: Dict[str, str] = Field(default_factory=dict)

    # Les annotations : texte libre (summary, description...). C'est là que
    # nos règles d'alerte déposent le nom du pod (`pod_name`).
    annotations: Dict[str, str] = Field(default_factory=dict)

    # Horodatages RFC3339 en UTC. Conservés tels quels.
    startsAt: Optional[str] = None
    endsAt: Optional[str] = None

    # Empreinte stable de l'alerte, utilisée par Alertmanager pour le
    # dédoublonnage. Elle change quand un label change.
    fingerprint: Optional[str] = None

    @property
    def alertname(self) -> str:
        """Le nom de l'alerte, ex. "DemoAppCrashLooping"."""
        return self.labels.get("alertname", "UnknownAlert")

    @property
    def severity(self) -> str:
        """La sévérité, ex. "critical" ou "warning"."""
        return self.labels.get("severity", "unknown")

    @property
    def namespace(self) -> Optional[str]:
        """Le namespace Kubernetes concerné, ou None s'il est inconnu.

        On lit d'abord le label `namespace` (c'est la convention Prometheus),
        puis on retombe sur l'annotation `namespace` que nos règles
        d'alerte remplissent.

        **On ne devine PAS.** Si aucun des deux n'est présent, on renvoie
        None et l'appelant décide du fallback (le namespace surveillé par
        l'agent). C'est un cas réel : la règle `up{job="demo-app"} == 0`
        produit une alerte qui n'a aucun label `namespace`, parce que la
        métrique `up` n'en porte pas.

        Le fallback naïf `"default"` était un bug : l'agent analysait
        l'incident dans le mauvais namespace, et le RBAC — volontairement
        limité à `aiops` — rejetait la lecture avec un 403.
        """
        return (
            self.labels.get("namespace")
            or self.annotations.get("namespace")
            or None
        )

    @property
    def pod_name(self) -> Optional[str]:
        """Le nom du pod si l'alerte le précise.

        Convention du projet : nos règles d'alerte copient `{{ $labels.pod }}`
        dans l'annotation `pod_name`. On lit cette annotation en priorité,
        puis on tente le label `pod` (convention kube-state-metrics).

        Peut renvoyer None : c'est le cas normal pour une alerte qui ne
        concerne pas un pod précis (latence agrégée par service, par
        exemple). Dans ce cas l'agent déduira le pod autrement.
        """
        return (
            self.annotations.get("pod_name")
            or self.labels.get("pod")
            or None
        )


class AlertGroup(BaseModel):
    """Le payload complet envoyé par Alertmanager à un receiver webhook.

    Rappel important : Alertmanager REGROUPE les alertes similaires et
    n'envoie qu'un seul HTTP POST contenant TOUTES les alertes du groupe
    dans `alerts[]`. Il faut donc toujours boucler sur ce tableau.
    """

    model_config = ConfigDict(extra="ignore")

    # "4" : la version du contrat. On la vérifie pour détecter un
    # changement de format inattendu plutôt que de mal interpréter le JSON.
    version: str = Field(default="4")

    # Clé de regroupement : encode les labels de group_by.
    groupKey: Optional[str] = None

    # Statut du GROUPE ("firing" ou "resolved").
    # ATTENTION : c'est un résumé. Si une alerte du groupe est résolue et
    # les autres non, le groupe vaut "firing". Il faut donc regarder
    # `alert.status` pour chaque alerte, pas `group.status`.
    status: str = Field(default="firing")

    # Nom du receiver configuré dans Alertmanager ("aiops-agent").
    receiver: Optional[str] = None

    # Labels communs à toutes les alertes du groupe.
    groupLabels: Dict[str, str] = Field(default_factory=dict)
    commonLabels: Dict[str, str] = Field(default_factory=dict)
    commonAnnotations: Dict[str, str] = Field(default_factory=dict)

    # URL de l'Alertmanager dans l'UI (utile pour le contexte).
    externalURL: Optional[str] = None

    # Les alertes du groupe.
    alerts: List[Alert] = Field(default_factory=list)

    @property
    def firing_alerts(self) -> List[Alert]:
        """Les seules alertes actives.

        Le webhook est configuré avec `send_resolved: true` : on reçoit
        aussi la fin d'incident. Diagnostiquer une alerte résolue n'a pas
        de sens — l'incident est terminé.
        """
        return [a for a in self.alerts if a.status == "firing"]
