# kit-asana

A small, safe Asana REST client for babower's projects: create, update, complete and
comment on tasks, and read a project's tasks page by page. Standard library only.
Import name: `kit_asana`.

```bash
pip install "kit-asana @ git+https://github.com/babower/kit@asana-v0.1.0#subdirectory=python/asana"
```

## Use it

```python
from kit_asana import AsanaError, AsanaNotFound, Client

asana = Client(token)              # a personal access token, from the app's settings

task = asana.create_task(project=PROJECT, section=INBOX, name="Furnace: filter swap",
                         notes="...", due_on="2026-12-01")
asana.update_task(task["gid"], due_on="2026-12-15")
asana.complete_task(task["gid"])
asana.add_comment(task["gid"], "Logged in the app")

for t in asana.tasks_in_project(PROJECT, modified_since=last_sync,
                                fields=["name", "notes", "completed", "completed_at"]):
    ...
```

The token, project and section gids always come from the app's settings, never from code.

## What it promises

- **Never logs** the token, request bodies or Asana's responses. Error messages carry only
  an HTTP status or an error type, so they are safe to log and show. `repr(client)` hides
  the token.
- **Typed errors:** `AsanaAuthError` (401/403), `AsanaNotFound` (404, for example a task
  deleted in Asana), `AsanaError` for everything else (`.status`, 0 for network trouble).
  Unlike kit-opsreport, it **does raise**: the caller decides what a failed call means.
- **Retries:** 429 is retried for every call, honouring `Retry-After` up to 30 s (a longer
  wait gives up at once). Reads and updates also retry 5xx and network errors with growing,
  jittered waits. Creating a task or a comment does **not** retry those, because the first
  attempt may have worked: keep your own record of created gids and stay idempotent.
  Limits: `timeout` per request (10 s), `budget` for all attempts of a call (60 s),
  `max_attempts` (4).
- **Checks inputs:** gids must be digits (so nothing can be smuggled into a URL path); the
  token must be one printable word; `base_url` must be https (plain http only to localhost,
  for tests).

## Test your app with FakeAsana

```python
from kit_asana import Client
from kit_asana.testing import FakeAsana

fake = FakeAsana()                          # a real local server; token "test-token"
project = fake.add_project("Home")          # fake.last_section is its first section
client = Client(fake.token, base_url=fake.base_url, sleep=lambda s: None)

fake.add_task(project, "Added by a person") # tasks the app did not create
fake.complete(gid); fake.reopen(gid); fake.delete(gid)   # what a person does in Asana
fake.fail_next(503, times=2)                # or fail_next(429, retry_after="1")
fake.requests, fake.bodies, fake.stories    # what the app sent
fake.close()
```

## Not included (yet)

Webhooks, OAuth, attachments, and custom fields beyond passing them through `**fields`.
