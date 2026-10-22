#!/usr/bin/env bash
#
# demo.sh — Provoque un incident et affiche ce que Kubernetes en dit.
#
# Deux scénarios, correspondant aux deux endpoints de l'application :
#   crash : le process se termine (exit 1)      -> CrashLoopBackOff
#   oom   : le kernel tue le process (OOMKill)  -> OOMKilled
#
# Le script affiche ensuite l'etat du pod et ses derniers evenements, ce qui
# correspond exactement a ce que l'agent IA de l'etape 3 devra interroger.
#
set -euo pipefail

NAMESPACE="aiops"
APP_URL="http://localhost:30090"
SCENARIO="${1:-crash}"

log()  { printf '\033[0;36m[demo]\033[0m %s\n' "$*"; }
warn() { printf '\033[0;33m[demo]\033[0m %s\n' "$*"; }
die()  { printf '\033[0;31m[demo]\033[0m %s\n' "$*" >&2; exit 1; }

# Nombre de déclenchements pour le scénario crash.
#
# Pourquoi 5 et pas 1 ? La règle Prometheus DemoAppCrashLooping s'appuie sur
#   increase(kube_pod_container_status_restarts_total[5m]) > 3
# avec un "for: 1m" : il faut donc PLUS DE 3 redémarrages en 5 minutes, et la
# condition doit rester vraie 1 minute. Un seul /crash ne produit qu'un
# redémarrage et ne déclencherait jamais l'alerte.
#
# On espace les déclenchements de CRASH_INTERVAL secondes pour que les
# redémarrages tombent dans la fenêtre glissante de 5 minutes.
CRASH_COUNT="${CRASH_COUNT:-5}"
CRASH_INTERVAL="${CRASH_INTERVAL:-5}"

# Attend que le pod demo-app réponde de nouveau sur /health.
#
# Pourquoi c'est indispensable : après un crash, Kubernetes NE redémarre pas
# le conteneur tout de suite. Le kubelet applique un back-off exponentiel
# (10 s, puis 20 s, puis 40 s, puis 80 s...). Pendant ce back-off, le pod
# n'écoute pas : une requête /crash échoue avec "connection refused" SANS
# que le process ait jamais été tué. Le compteur de redémarrages ne bouge
# donc pas, et l'alerte ne se déclenche jamais.
#
# On attend donc que le pod soit de nouveau "Ready" avant de le tuer à
# nouveau : c'est le seul moyen de produire N redémarrages réalistes.
wait_for_app() {
    local waited=0
    while [[ $waited -lt 120 ]]; do
        if curl -sf -m 3 "$APP_URL/health" >/dev/null 2>&1; then
            return 0
        fi
        sleep 2
        waited=$((waited + 2))
    done
    return 1
}

case "$SCENARIO" in
    crash)
        log "Scénario : crash applicatif (le process se termine avec le code 1)."
        log "Déclenchement $CRASH_COUNT fois (il faut > 3 redémarrages en 5 min"
        log "pour que la règle Prometheus DemoAppCrashLooping se déclenche)."
        for i in $(seq 1 "$CRASH_COUNT"); do
            printf "  [%d/%d] attente du podReady..." "$i" "$CRASH_COUNT"
            if ! wait_for_app; then
                warn "  le pod ne redevient pas Ready — abandon."
                break
            fi
            printf " pret, crash... "
            # On ignore le code de retour : la connexion sera coupée, c'est
            # précisément ce qui prouve que le process est mort.
            curl -s -m 10 "$APP_URL/crash" >/dev/null 2>&1 || true
            echo "ok"
            # Petite pause pour que le kubelet enregistre l'arrêt avant le
            # tour suivant.
            sleep "$CRASH_INTERVAL"
        done
        ;;
    oom)
        log "Scénario : OOMKill (le kernel tue le process pour dépassement mémoire)."
        log "Attente de la réponse HTTP — elle sera coupée, c'est normal."
        curl -s -m 15 "$APP_URL/oom" || warn "  (connexion coupée : le process a bien été tué)"
        ;;
    *)
        die "Scénario inconnu : '$SCENARIO'. Utiliser 'crash' ou 'oom'."
        ;;
esac

echo
log "Attente de la fin du redémarrage (15 s)..."
sleep 15

echo
log "============================================================"
log " État du pod"
log "============================================================"
kubectl -n "$NAMESPACE" get pods -l app=demo-app -o wide

echo
log "============================================================"
log " Événements récents du pod"
log "============================================================"
# Les événements expliquent CE QUI S'EST PASSE : "Back-off restarting failed
# container" pour un crash, "OOMKilled" pour un dépassement mémoire.
kubectl -n "$NAMESPACE" get events --sort-by=.lastTimestamp 2>/dev/null | tail -15

echo
log "============================================================"
log " Description du pod (raison du dernier arrêt)"
log "============================================================"
POD=$(kubectl -n "$NAMESPACE" get pods -l app=demo-app -o jsonpath='{.items[0].metadata.name}' 2>/dev/null || true)
if [[ -n "$POD" ]]; then
    kubectl -n "$NAMESPACE" describe pod "$POD" 2>/dev/null | grep -A6 -E 'Last State|Terminated|Reason' | head -20
else
    warn "Aucun pod demo-app trouvé."
fi

echo
log "============================================================"
log " Derniers logs de l'application"
log "============================================================"
kubectl -n "$NAMESPACE" logs -l app=demo-app --tail=15 --previous 2>/dev/null || \
    kubectl -n "$NAMESPACE" logs -l app=demo-app --tail=15 2>/dev/null || \
    warn "  (pas de logs disponibles)"

echo
log "============================================================"
log " Alertes Prometheus déclenchées"
log "============================================================"
# On interroge l'API Prometheus pour lister les alertes en cours.
# Les règles DemoAppCrashLooping / DemoAppDown / DemoAppHighLatency
# apparaissent ici quand elles se déclenchent.
PROM_URL="http://localhost:30093"

# Note : on teste /-/ready, et NON /-/alerts. /-/alerts n'existe pas dans
# Prometheus (il renvoie un 404), ce qui faisait échouer la détection même
# quand Prometheus répondait parfaitement.
if curl -sf -m 10 "$PROM_URL/-/ready" >/dev/null 2>&1; then
    # On récupère la réponse JSON en entier avant de la traiter, pour pouvoir
    # distinguer trois cas : API en erreur, aucune alerte, ou des alertes.
    ALERTS_JSON=$(curl -s -m 10 "$PROM_URL/api/v1/alerts")
    if [[ -z "$ALERTS_JSON" ]]; then
        warn "  (réponse vide de l'API Prometheus)"
    elif ! echo "$ALERTS_JSON" | jq -e . >/dev/null 2>&1; then
        warn "  (réponse inattendue de l'API Prometheus) : ${ALERTS_JSON:0:200}"
    else
        COUNT=$(echo "$ALERTS_JSON" | jq '.data.alerts | length')
        if [[ "$COUNT" -eq 0 ]]; then
            # jq sur un tableau vide ne sort AUCUNE ligne (mais exit 0) : sans
            # ce test explicite, l'utilisateur ne verrait rien du tout et
            # pourrait croire que la commande a échoué.
            log "  Aucune alerte active pour le moment."
        else
            echo "$ALERTS_JSON" | jq -r '
                .data.alerts[] |
                "  [\(.state)] \(.labels.alertname)  —  \(.labels.severity // "-")"
            '
        fi
    fi
else
    warn "  Prometheus n'est pas joignable sur $PROM_URL (est-il déployé ?)"
fi
