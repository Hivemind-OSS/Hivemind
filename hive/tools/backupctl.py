"""In-container entry for `hive backup`: snapshot the warm store + prune to
`retention.backup_keep`, then print the snapshot's dest path on stdout.

A status/healthcheck analog — the host `hive backup` verb execs
`python -m hive.tools.backupctl` inside the container and forwards this one line. Manual:
there is no scheduler, so it keeps the `backup_keep` most-recent snapshots YOU take.
"""
from __future__ import annotations

import os
import sys
from typing import Mapping, Optional

from hive.app.config import Config
from hive.ops.backup import run_daily_backup

_DEFAULT_DB_PATH = "/data/shared.db"   # mirrors entrypoint/healthcheck


def main(argv: Optional[list[str]] = None, *, env: Optional[Mapping[str, str]] = None) -> int:
    env = os.environ if env is None else env
    db_path = (env.get("HIVE_STORE__DB_PATH") or "").strip() or _DEFAULT_DB_PATH
    cfg = Config.load(db_path=db_path, env=env)
    rec = run_daily_backup(cfg)
    print(rec.dest_path)                 # the snapshot path, forwarded verbatim by the verb
    return 0


if __name__ == "__main__":  # pragma: no cover — module entry (`python -m hive.tools.backupctl`)
    raise SystemExit(main(sys.argv[1:]))
