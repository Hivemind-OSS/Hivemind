#!/usr/bin/env bash
# Decommission the old AgentCortex deployment. Reversible: archive (mv), not delete (rm).
set -euo pipefail
HOME_DIR="${HOME:?}"
ARCHIVE="${HOME_DIR}/cortex.archived-$(date +%Y%m%d-%H%M%S-%N)"   # %N: sub-second, no same-second collision
SETTINGS="${HOME_DIR}/.claude/settings.json"
CORTEX_UNITS=(cortex-consolidate cortex-report cortex-backup cortex-maintain cortex-label)
DRY=0; RESTORE=0
for a in "$@"; do case "$a" in --dry-run) DRY=1;; --restore) RESTORE=1;; esac; done
run() { if [ "$DRY" = 1 ]; then echo "DRY: $*"; else "$@"; fi; }

strip_cortex_hooks() {
  # Remove ONLY cortex-* hook commands; preserve groundcheck, git-ai, every non-cortex hook.
  # Anchor on a path-component boundary ((^|/)cortex[_-]) so a cortex_*/cortex-* script or
  # unit is stripped but a BENIGN command that merely contains "cortex" mid-path (e.g.
  # /home/u/data-cortex/hook.py) is preserved — the broad substring test over-matched.
  [ -f "$SETTINGS" ] || return 0
  local tmp; tmp="$(mktemp)"
  jq '(.hooks // {}) |= with_entries(
        .value |= map(select(
          (.hooks // []) | all(.command // "" | test("(^|/)cortex[_-]"; "i") | not)
        ))
      )' "$SETTINGS" > "$tmp"
  if [ "$DRY" = 1 ]; then echo "DRY: would write stripped $SETTINGS"; diff "$SETTINGS" "$tmp" || true; rm -f "$tmp"
  else mv "$tmp" "$SETTINGS"; echo "stripped cortex hooks from $SETTINGS"; fi
}

if [ "$RESTORE" = 1 ]; then
  latest="$(ls -1d "${HOME_DIR}"/cortex.archived-* 2>/dev/null | sort | tail -1 || true)"
  [ -n "$latest" ] || { echo "no archive to restore" >&2; exit 1; }
  run mv "$latest" "${HOME_DIR}/cortex"
  for u in "${CORTEX_UNITS[@]}"; do run systemctl --user enable --now "${u}.timer" 2>/dev/null || true; done
  echo "restored ${latest} -> ${HOME_DIR}/cortex"; exit 0
fi

# 1) stop + remove the systemd units (idempotent: || true on absent units)
for u in "${CORTEX_UNITS[@]}"; do
  run systemctl --user disable --now "${u}.timer" 2>/dev/null || true
  run systemctl --user disable --now "${u}.service" 2>/dev/null || true
  run rm -f "${HOME_DIR}/.config/systemd/user/${u}.timer" "${HOME_DIR}/.config/systemd/user/${u}.service" 2>/dev/null || true
done
run systemctl --user daemon-reload 2>/dev/null || true
# 2) strip ONLY cortex hooks
strip_cortex_hooks
# 3) archive (mv, NOT rm) the cortex tree
[ -d "${HOME_DIR}/cortex" ] && run mv "${HOME_DIR}/cortex" "$ARCHIVE" && echo "archived ${HOME_DIR}/cortex -> ${ARCHIVE}"
echo "teardown complete (reversible: ./teardown.sh --restore)"
