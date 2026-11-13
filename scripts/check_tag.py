"""Check a release tag such as signin-v0.1.0 against python/signin/pyproject.toml."""

import re
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

tag = sys.argv[1]
m = re.fullmatch(r"([a-z0-9]+)-v(\d+\.\d+\.\d+)", tag)
if not m:
    sys.exit(f"{tag}: release tags look like <package>-vX.Y.Z")
package, version = m.groups()
pyproject = ROOT / "python" / package / "pyproject.toml"
if not pyproject.exists():
    sys.exit(f"{tag}: there is no package python/{package}")
declared = tomllib.loads(pyproject.read_text())["project"]["version"]
if declared != version:
    sys.exit(f"{tag}: python/{package}/pyproject.toml says {declared}")
changelog = (ROOT / "python" / package / "CHANGELOG.md").read_text()
if f"## {version}" not in changelog:
    sys.exit(f"{tag}: python/{package}/CHANGELOG.md has no '## {version}' entry")
print(f"{tag}: ok")
