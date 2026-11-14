# Changelog: kit-asana

The import name is `kit_asana`.

## 0.1.0 (2026-11-14)

- First release: `Client` with create, update, complete, comment, get and paginated
  project reads; typed, safe errors; retries for 429 (all calls) and for 5xx and network
  errors (reads and updates only); `kit_asana.testing.FakeAsana` for apps' tests.
  First users: smart-home and ops-dashboard.
