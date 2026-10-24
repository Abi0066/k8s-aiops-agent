#!/usr/bin/env bash
#
# last-analysis.sh — Affiche le dernier diagnostic de l'agent, formaté.
#
# L'agent journalise chaque analyse sur une ligne « ANALYSIS_RESULT <json> ».
# Ce script extrait la dernière et n'en garde que les champs utiles : le
# verdict déterministe, la remédiation et la durée. Sans lui, il faut
# découper la ligne à la main avec grep/sed/jq.
#
# Usage : ./scripts/last-analysis.sh [fenêtre]
#   fenêtre : durée remontée dans les logs (défaut 30m, syntaxe kubectl)
set -euo pipefail
# shellcheck source=scripts/lib.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

WINDOW="${1:-30m}"
LINE=$(kubectl -n "$NAMESPACE" logs deploy/aiops-agent --since="$WINDOW" 2>/dev/null \
    | grep ANALYSIS_RESULT | tail -1 || true)

if [[ -z "$LINE" ]]; then
    die "Aucune analyse dans les $WINDOW dernières minutes."
fi

echo "$LINE" | sed 's/^.*ANALYSIS_RESULT //' | jq -C '{
    alertname,
    verdict: .verdict.code,
    confidence: .verdict.confidence,
    remediation: (if .remediation then
        {status: .remediation.status, from: .remediation.from, to: .remediation.to}
      else null end),
    llm_ok,
    duration_s
}'
