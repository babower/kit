# kit

Shared, versioned libraries used by more than one of babower's projects. Each package
lives in its own folder, has its own version and is released with its own tag.

Projects **install a pinned release; they never copy code in.** The registry of
everything shared between projects (libraries, services, starters and conventions) is in
Notion: Dev Knowledge Base → Shared → Components.

## Packages

| Package | What it does | Install (pin the latest tag) |
| --- | --- | --- |
| [charts](python/charts) | Dependency-free SVG charts, the validated palette and light/dark tokens for server-rendered dashboards; CSP-safe | `kit-charts @ git+https://github.com/babower/kit@charts-v0.1.0#subdirectory=python/charts` |
| [opsreport](python/opsreport) | Report usage, runs, outcomes and production stages to the ops dashboard; never breaks the caller; standard library only | `kit-opsreport @ git+https://github.com/babower/kit@opsreport-v0.2.1#subdirectory=python/opsreport` |
| [signin](python/signin) | Auth0 / OpenID Connect sign-in for FastAPI: sessions, access rules checked every request, CSRF, security headers, a fake provider for tests | `kit-signin @ git+https://github.com/babower/kit@signin-v0.1.0#subdirectory=python/signin` |

The install line for a package looks like this (the `kit-` name, the tag, the folder):

```bash
pip install "kit-<package> @ git+https://github.com/babower/kit@<package>-vX.Y.Z#subdirectory=python/<package>"
```

or, in a `pyproject.toml`:

```toml
dependencies = [
  "kit-<package> @ git+https://github.com/babower/kit@<package>-vX.Y.Z#subdirectory=python/<package>",
]
```

## Layout

```
python/<package>/      one folder per Python package
  pyproject.toml       distribution name kit-<package>, its own version
  src/                 the code
  tests/               its own tests
  README.md            what it does and how to use it
  CHANGELOG.md         one entry per release
scripts/               CI helpers
```

JavaScript or Unity packages get `js/` or `unity/` folders when a second project in that
language needs the same thing.

## Versions and releases

- [Semantic versioning](https://semver.org) per package. Within a major version, changes
  are additive only, so a project on an older release keeps working.
- To release: bump `version` in the package's `pyproject.toml`, add a `## X.Y.Z` entry to
  its `CHANGELOG.md`, merge, then tag the merge commit `<package>-vX.Y.Z` and push the tag.
  CI checks the tag matches the package's version and changelog.
- Projects move to a new release through an upgrade pull request (Renovate, planned).

## Develop

```bash
cd python/<package>
python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'
.venv/bin/pytest -q && .venv/bin/ruff check .
```

CI runs each package's tests on Python 3.11 and 3.13, only for the packages a pull
request touches.
