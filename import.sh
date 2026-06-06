#!/usr/bin/env bash
# One-time corpus import: re-embed the archived old store FROM TEXT through the new PCA head,
# scanning every row for secrets, landing rows status='pending' under the import-admin identity.
set -euo pipefail
OLD_DB="${1:?usage: import.sh <old_store.db> [new_db]}"
NEW_DB="${2:-/data/shared.db}"
[ -f "$OLD_DB" ] || { echo "import: old store not found: $OLD_DB" >&2; exit 1; }
exec python -m hive.ops.migration import-corpus \
  --old-db "$OLD_DB" \
  --new-db "$NEW_DB" \
  --import-admin "import-admin" \
  --scan-secrets       # refuse/redact via SecretScanner BEFORE stage [B4]; rows land pending
