#!/usr/bin/env bash
# hive {up|down|logs|nuke} — liveness ONLY (handshake is hive_init, a separate concern).
# NOTE: spec §6.3 names this `./hive`, but the Python package dir `hive/` occupies that
# path; renamed to `hive.sh` to avoid the collision (P1.13/M12 compose references this).
set -euo pipefail
COMPOSE=(docker compose)
HEALTH_TIMEOUT="${HIVE_HEALTH_TIMEOUT:-180}"

_wait_healthy() {
  local cid elapsed=0
  cid="$("${COMPOSE[@]}" ps -q hive-server)"
  [ -n "$cid" ] || { echo "hive: server container not found" >&2; exit 1; }
  while :; do
    status="$(docker inspect -f '{{.State.Health.Status}}' "$cid" 2>/dev/null || echo starting)"
    case "$status" in
      healthy) echo "hive: healthy" >&2; return 0 ;;
      unhealthy) echo "hive: UNHEALTHY" >&2; "${COMPOSE[@]}" logs --tail=200 hive-server >&2; exit 1 ;;
    esac
    [ "$elapsed" -ge "$HEALTH_TIMEOUT" ] && {
      echo "hive: health-wait timeout after ${HEALTH_TIMEOUT}s" >&2
      "${COMPOSE[@]}" logs --tail=200 hive-server >&2; exit 1; }
    sleep 3; elapsed=$((elapsed + 3))
  done
}

case "${1:-}" in
  up)   "${COMPOSE[@]}" up -d --build hive-server; _wait_healthy ;;
  down) "${COMPOSE[@]}" down ;;            # PRESERVES the named volume
  logs) shift; "${COMPOSE[@]}" logs -f "$@" ;;
  nuke) "${COMPOSE[@]}" down -v ;;          # DESTROYS the volume (data loss)
  *)    echo "usage: hive.sh {up|down|logs|nuke}" >&2; exit 2 ;;
esac
