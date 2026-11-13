# kit-opsreport

Report usage, runs, outcomes and production stages to the ops dashboard (babower's
private dashboard for Claude-powered projects) from any Python project, **without ever
breaking the project's own run**. Standard library only. Import name: `opsreport`.

```python
from opsreport import Reporter

ops = Reporter("my-project")                        # reads OPS_URL and OPS_TOKEN

ops.usage(provider="anthropic", op="write", model="claude-opus-5-5",
          input_tokens=900, output_tokens=300, cost_usd=0.0096, item_id="2026-11-10-dynamic",
          event_id="2026-11-10-dynamic:write:1")
ops.run(status="ok", duration_seconds=41.2, mode="dynamic", item_id="2026-11-10-dynamic")
ops.outcome(item_id="2026-11-10-dynamic", horizon_hours=72, metrics={"views": 1200},
            reward=1.4, event_id="2026-11-10-dynamic:72h")
ops.metric(series="mood.share", value=0.31, labels={"emotion": "anger"})
ops.stage(item_id="2026-11-10-dynamic", stage="published", state="done",
          labels={"platform": "instagram"}, event_id="2026-11-10-dynamic:published:instagram")

ops.flush()          # send what is buffered; or use `with Reporter(...) as ops:`
```

The six calls are `usage`, `run`, `outcome`, `budget`, `metric` and `stage` (0.2.0+). Their
fields follow the dashboard's event format, ops-events v1 (`docs/ops-events.md` in the
dashboard's repository); each call's docstring lists them too.

## Install

```bash
pip install "kit-opsreport @ git+https://github.com/babower/kit@opsreport-v0.2.1#subdirectory=python/opsreport"
```

Pin the tag; upgrade by changing it. Needs Python 3.11 or newer and nothing else.

## Setup

| Environment variable | Meaning |
| --- | --- |
| `OPS_URL` | The dashboard, for example `https://ops.example.com`. Must be https (plain http only for localhost, so a token never crosses the network unencrypted) |
| `OPS_TOKEN` | The project's token (`opsdash token create <project>` on the dashboard). Keep it in a secret store, never in a repo |
| `OPS_BUFFER_DIR` | Where events wait before they are sent. Default `.opsreport` in the current directory |
| `OPS_FLUSH` | Set to `off` to keep buffering but never send (a dry run) |
| `OPS_PROJECT` | The project slug, if you don't pass it to `Reporter` (used by the command line) |

**If `OPS_URL` or `OPS_TOKEN` is not set, the library does nothing at all**: no files, no
network. So a project can call it everywhere and only report where it is configured.

## What it guarantees

1. **It never raises into your code.** Every public method catches everything. A dashboard
   outage, a full disk or an odd value (NaN, an object that isn't JSON) is logged (type only,
   never values or the token) and skipped; the return value says what happened.
2. **It never holds you up for long.** Sending has a time budget (`flush_timeout`, 20 s) and a
   per-request timeout (5 s). If the dashboard says "retry in 10 minutes", it stops at once
   and keeps the events for next time instead of waiting.
3. **Nothing is lost.** Events are written to a local buffer first and removed only after the
   dashboard confirms them. A crash in the middle of a flush can only cause a repeat.
4. **Repeats are harmless.** The dashboard ignores an event it already has (same `project` and
   `event_id`), so retries are free. For a backfill or a re-run to be recognised, pass an
   `event_id` made from the data, for example `f"{item_id}:{hours}h"`; if you don't, a random
   one is used, which is fine for live reporting.

## How sending works

- `flush()` sends the buffer in batches of up to 500 events (and under about 1.5 MB), and
  returns a `FlushResult(sent, duplicates, pending, rejected, error)`.
- Network trouble, HTTP 5xx and 429 are retried with growing, jittered waits (and `Retry-After`
  is honoured up to 30 s). Anything still unsent stays buffered.
- 401 or 403 (bad or wrong-project token) and 404 or 405 (wrong URL) stop the flush and keep
  everything: fix the setting and flush again.
- If the dashboard refuses a batch for good (422, 413), the client splits it until it finds the
  event(s) responsible, delivers the rest, and moves the bad ones to
  `<project>.rejected.jsonl` for you to look at.
- `autoflush_every=N` flushes every N events; `flush_at_exit=True` flushes when Python exits.

## Command line

```bash
python -m opsreport --project social-media-prayer-flags status   # is it on? what is waiting?
python -m opsreport --project social-media-prayer-flags flush    # send the buffer (exit 1 if not all sent)
```

Useful as a final step of a scheduled job, or from cron to catch up after an outage.

## Tests

```bash
pytest client/tests          # the client against a scripted mock server: no database needed
pytest tests/test_client_integration.py   # against the real ingest API (needs Postgres)
```
