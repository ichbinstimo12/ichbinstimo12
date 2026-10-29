#!/usr/bin/env bash
# Bedienung der Firma auf dem Server (auch bequem per SSH-App vom Handy).
#   ./scripts/firmctl.sh start | stop | restart | status | logs | halt | resume | notify-test | backup | update
#   ./scripts/firmctl.sh approve-live [Sekunden]      (nur nach deiner bewussten Entscheidung!)
set -euo pipefail
cd "$(dirname "$0")/.."

PROFILES=()
[[ -n "${FIRM_PROFILES:-}" ]] && for p in ${FIRM_PROFILES//,/ }; do PROFILES+=(--profile "$p"); done
dc() { docker compose "${PROFILES[@]}" "$@"; }
firm() { dc exec -T firm python -m src.firm "$@"; }

prepare() {
  [[ -f .env ]] || { echo ".env fehlt: cp .env.example .env und ausfuellen (siehe docs/deploy_vps.md)"; exit 1; }
  if grep -qE '^(CAPITAL_API_KEY|FIRM_API_TOKEN)=HIER_EINFUEGEN' .env; then
    echo ".env enthaelt noch Platzhalter (HIER_EINFUEGEN)."; exit 1
  fi
  chmod 600 .env
  mkdir -p data
  # Der Container laeuft als uid 1000 (ohne root-Rechte) und braucht Schreibrecht auf data/
  if [[ "$(stat -c %u data)" != "1000" ]]; then sudo chown -R 1000:1000 data; fi
}

case "${1:-status}" in
  start)       prepare; dc up -d --build; dc ps ;;
  stop)        dc stop -t 90 ;;                      # Positionen bleiben offen (Server-Stops beim Broker)
  restart)     prepare; dc up -d --build --force-recreate; dc ps ;;
  status)      dc ps; echo; curl -s http://127.0.0.1:8000/health/deep; echo; firm status || true ;;
  logs)        dc logs -f --tail 200 firm ;;
  halt)        firm halt ;;                           # Kill-Switch: alles glattstellen, dann warten
  resume)      firm resume ;;                         # Firma startet danach von selbst wieder
  notify-test) firm notify-test ;;
  backup)      dc exec -T firm python -c \
                 "import os; from src.firm.ops.backup import backup_now; print(backup_now(os.environ['FIRM_DB_PATH'], os.environ['FIRM_BACKUP_DIR']))" ;;
  update)      git pull --ff-only; prepare; dc up -d --build; dc ps; docker image prune -f >/dev/null ;;
  approve-live)
               read -r -p "LIVE-Freigabe erteilen? Echtes Geld. Tippe JA: " ok
               [[ "$ok" == "JA" ]] || { echo "Abgebrochen."; exit 1; }
               firm approve-live --duration "${2:-3600}" ;;
  *)           sed -n '2,5p' "$0"; exit 1 ;;
esac
