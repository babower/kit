# Changelog: kit-opsreport

The import name is `opsreport`. Versions 0.1.0 to 0.2.0 were released inside the
ops-dashboard repository (`client/`); from 0.2.1 the package lives here.

## 0.2.1 (2026-11-13)

- Moved to babower/kit as `kit-opsreport` (public, so installing needs no credentials).
- The User-Agent now carries the real version (`opsreport/0.2.1`; it said `opsreport/0.1`),
  so the dashboard can show which version each project runs.
- Fixed a flaky test: retry backoff has random jitter, so the second wait can be shorter
  than the first; the test now checks each wait's range.

## 0.2.0 (2026-11-12)

- `stage()`: one output moving through a production step (render, review, published...).

## 0.1.1 (2026-11-12)

- A token pasted with surrounding whitespace is cleaned; one with inner spaces or line
  breaks switches reporting off with a clear warning (never logging the token). A request
  that can't be built counts as a configuration problem; events stay buffered.

## 0.1.0 (2026-11-11)

- First release: usage, run, outcome, budget and metric events; local buffer, batching,
  retries with backoff, idempotent event ids; never raises into the caller.
