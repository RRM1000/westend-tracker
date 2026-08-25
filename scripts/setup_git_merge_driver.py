"""Register the SQLite merge driver for this clone.

Merge drivers live in .git/config, which isn't version-controlled, so this
has to be run once per clone (including on a fresh CI checkout). Without it,
git falls back to "binary files differ" and a conflict has to be resolved by
hand — and resolving it the obvious way loses data.
"""

import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
DRIVER = REPO / "scripts" / "merge_sqlite_db.py"


def main() -> int:
    py = sys.executable or "python"
    cmds = [
        ["git", "config", "merge.sqlitedb.name",
         "Union-merge the collected price archive"],
        ["git", "config", "merge.sqlitedb.driver",
         f'"{py}" "{DRIVER}" %O %A %B'],
        # Keep the locally generated report rather than conflicting on it.
        ["git", "config", "merge.ours.driver", "true"],
    ]
    for c in cmds:
        subprocess.run(c, cwd=REPO, check=True)
    print("SQLite merge driver registered for this clone.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
