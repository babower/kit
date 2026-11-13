"""Print the packages to test as a JSON list (used by CI).

    python3 scripts/packages.py               # every package under python/
    python3 scripts/packages.py origin/main   # only those changed since that ref

A change outside python/ (CI, scripts) tests everything.
"""

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ALL = sorted(p.name for p in (ROOT / "python").iterdir() if (p / "pyproject.toml").exists())


def changed(base: str) -> list[str]:
    files = subprocess.run(["git", "diff", "--name-only", f"{base}...HEAD"], cwd=ROOT,
                           capture_output=True, text=True, check=True).stdout.split()
    if any(not f.startswith("python/") and f.endswith((".py", ".yml", ".toml")) for f in files):
        return ALL
    return sorted({f.split("/")[1] for f in files if f.startswith("python/")} & set(ALL))


print(json.dumps(changed(sys.argv[1]) if len(sys.argv) > 1 else ALL))
