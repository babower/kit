# CLAUDE.md

Shared libraries for babower's projects. Project context lives in Notion: Dev Knowledge
Base → **dev-kit**; the registry of shared components is Dev Knowledge Base → Shared →
**Components**. Ticket workflow: Dev Knowledge Base → Shared → **Asana Ticket
Convention**. Asana project: dev-kit. Work tickets on branches named
`claude/<task-gid>-<short-slug>`, one PR per ticket.

## Commands (per package)

- Install: `cd python/<package> && python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'`
- Test: `.venv/bin/pytest -q` · Lint: `.venv/bin/ruff check .`

## Rules

- **This repo is public.** Never add secrets, tokens, hostnames of private services,
  personal data or anything project-specific (prompts, guardrails, business rules).
  Addresses, keys and allowed users always come from the using app's settings.
- Only generic code that at least two projects need belongs here. Each package has its own
  folder, `pyproject.toml` (distribution `kit-<package>`), tests, README and CHANGELOG.
- Semantic versioning per package; within a major version, changes are additive only.
  Every release bumps the version, adds a CHANGELOG entry and is tagged
  `<package>-vX.Y.Z` on the merge commit.
- Projects install pinned tags; never tell a project to copy code from here.
- Libraries never raise into their caller where the README promises they won't, never log
  tokens, secrets or personal data, and fail closed on security decisions.
- Python 3.11+. Keep dependencies few; prefer the standard library.
