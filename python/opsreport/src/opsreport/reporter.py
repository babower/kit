"""Reporter: the one object a project uses to report to the ops dashboard.

Guarantees, in this order of importance:

1. It never raises into the caller and never blocks the caller for long. Reporting is
   optional; a dashboard outage, a full disk or a bad value must not break a real run.
2. Nothing is lost on the way: events go to a local buffer first, are sent in batches, and
   stay buffered until the dashboard confirms them.
3. Sending twice is harmless: the dashboard ignores events it already has, so the client
   can retry freely.
4. If OPS_URL or OPS_TOKEN is not set, it does nothing at all (no files, no network).
"""

from __future__ import annotations

import atexit
import functools
import json
import logging
import os
import random
import threading
import time
import uuid
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from . import transport
from .buffer import Buffer

log = logging.getLogger("opsreport")

SCHEMA_VERSION = 1
MAX_BATCH = 500  # the dashboard's limit per request
MAX_BATCH_BYTES = 1_500_000  # under the dashboard's 2 MiB body limit
RETRY_AFTER_CAP = 30.0
FALSE_WORDS = {"0", "false", "off", "no"}


@dataclass
class FlushResult:
    sent: int = 0  # events the dashboard accepted as new
    duplicates: int = 0  # events it already had (fine)
    pending: int = 0  # still buffered, to be sent later
    rejected: int = 0  # refused for good and moved to the .rejected file
    error: str | None = None  # None, or why sending stopped early

    @property
    def ok(self) -> bool:
        return self.error is None


@dataclass
class _Outcome:
    kind: str  # ok | permanent | auth | config | transient
    accepted: int = 0
    duplicates: int = 0


def _safe(default: Callable[[], Any] = lambda: None):
    """Decorator: never let an exception escape a public method."""
    def wrap(fn):
        @functools.wraps(fn)
        def inner(self, *args, **kwargs):
            try:
                return fn(self, *args, **kwargs)
            except Exception as e:
                log.warning("opsreport: %s failed (%s)", fn.__name__, type(e).__name__)
                return default()
        return inner
    return wrap


def _iso(value: datetime | date | str | None) -> str:
    """An ISO 8601 timestamp with a timezone. Naive datetimes are taken as UTC."""
    if value is None:
        return datetime.now(UTC).isoformat()
    if isinstance(value, str):
        return value
    if not isinstance(value, datetime):  # a date
        value = datetime(value.year, value.month, value.day)
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat()


class Reporter:
    def __init__(self, project: str | None = None, *, url: str | None = None,
                 token: str | None = None, buffer_dir: str | os.PathLike | None = None,
                 timeout: float = 5.0, flush_timeout: float = 20.0, max_attempts: int = 3,
                 backoff: float = 0.5, autoflush_every: int = 0, flush_at_exit: bool = False,
                 flush_enabled: bool | None = None,
                 sleep: Callable[[float], None] = time.sleep,
                 clock: Callable[[], float] = time.monotonic):
        self.project = project or os.environ.get("OPS_PROJECT") or ""
        self.token = (token if token is not None else os.environ.get("OPS_TOKEN", "")).strip()
        if not self.token.isprintable() or " " in self.token:
            log.warning("opsreport: OPS_TOKEN contains spaces or line breaks (a paste "
                        "mistake?); reporting is off")
            self.token = ""
        raw_url = url if url is not None else os.environ.get("OPS_URL", "")
        self.url = transport.events_url(raw_url) if raw_url else None
        if raw_url and not self.url:
            log.warning("opsreport: OPS_URL is not usable (it must be https, or http to "
                        "localhost); reporting is off")
        if flush_enabled is None:
            flush_enabled = os.environ.get("OPS_FLUSH", "1").strip().lower() not in FALSE_WORDS
        self.flush_enabled = flush_enabled
        self.timeout = timeout
        self.flush_timeout = flush_timeout
        self.max_attempts = max(1, max_attempts)
        self.backoff = backoff
        self.autoflush_every = autoflush_every
        self._sleep = sleep
        self._clock = clock
        self._since_flush = 0
        self._flush_lock = threading.Lock()
        self.buffer = Buffer(Path(buffer_dir or os.environ.get("OPS_BUFFER_DIR")
                                  or ".opsreport"), self.project or "unnamed")
        if self.enabled and flush_at_exit:
            atexit.register(self.flush)

    # --- state --------------------------------------------------------------------
    @property
    def enabled(self) -> bool:
        """True if there is somewhere to report to: a project, a usable URL and a token."""
        return bool(self.project and self.url and self.token)

    def __enter__(self) -> Reporter:
        return self

    def __exit__(self, *exc) -> None:
        self.flush()

    # --- reporting ------------------------------------------------------------------
    @_safe()
    def usage(self, *, provider: str, op: str, model: str = "", input_tokens: int = 0,
              output_tokens: int = 0, cache_read_tokens: int = 0, units: float = 0.0,
              unit_name: str = "", cost_usd: float = 0.0, item_id: str | None = None,
              simulated: bool = False, event_id: str | None = None,
              ts: datetime | str | None = None) -> str | None:
        """Tokens or units used and their cost. One per Claude call or provider job."""
        return self._add("usage", event_id, ts, provider=provider, op=op, model=model,
                         input_tokens=input_tokens, output_tokens=output_tokens,
                         cache_read_tokens=cache_read_tokens, units=units, unit_name=unit_name,
                         cost_usd=cost_usd, item_id=item_id, simulated=simulated)

    @_safe()
    def run(self, *, status: str, duration_seconds: float = 0.0, mode: str = "",
            item_id: str | None = None, items: int = 0, errors: dict[str, str] | None = None,
            notes: list[str] | None = None, details: dict[str, Any] | None = None,
            simulated: bool = False, event_id: str | None = None,
            ts: datetime | str | None = None) -> str | None:
        """One execution of a pipeline. `ts` is when it started. status: ok, partial,
        failed or held."""
        return self._add("run", event_id, ts, status=status, duration_seconds=duration_seconds,
                         mode=mode, item_id=item_id, items=items, errors=errors or {},
                         notes=notes or [], details=details or {}, simulated=simulated)

    @_safe()
    def outcome(self, *, item_id: str, horizon_hours: int, metrics: dict[str, float],
                reward: float | None = None, simulated: bool = False,
                event_id: str | None = None, ts: datetime | str | None = None) -> str | None:
        """Engagement for one output at one horizon. Use event_id=f"{item_id}:{h}h" so a
        repeat is recognised."""
        return self._add("outcome", event_id, ts, item_id=item_id, horizon_hours=horizon_hours,
                         metrics=metrics, reward=reward, simulated=simulated)

    @_safe()
    def budget(self, *, effective_from: datetime | date | str, monthly_budget_usd: float,
               fixed_monthly_usd: dict[str, float] | None = None, event_id: str | None = None,
               ts: datetime | str | None = None) -> str | None:
        """A monthly budget and fixed costs. The latest effective_from wins."""
        return self._add("budget", event_id, ts, effective_from=_iso(effective_from),
                         monthly_budget_usd=monthly_budget_usd,
                         fixed_monthly_usd=fixed_monthly_usd or {})

    @_safe()
    def metric(self, *, series: str, value: float, labels: dict[str, str] | None = None,
               simulated: bool = False, event_id: str | None = None,
               ts: datetime | str | None = None) -> str | None:
        """A named number for project-specific charts, e.g. series="mood.share",
        labels={"emotion": "anger"}."""
        return self._add("metric", event_id, ts, series=series, value=value,
                         labels=labels or {}, simulated=simulated)

    @_safe()
    def stage(self, *, item_id: str, stage: str, state: str = "entered",
              labels: dict[str, str] | None = None, simulated: bool = False,
              event_id: str | None = None, ts: datetime | str | None = None) -> str | None:
        """One output moving through a production step: stage="render", state="done";
        stage="published", state="done", labels={"platform": "instagram"}. state is
        entered, done, failed or skipped. Use an event_id derived from the item, stage,
        state and platform so a repeat is recognised."""
        return self._add("stage", event_id, ts, item_id=item_id, stage=stage, state=state,
                         labels=labels or {}, simulated=simulated)

    def _add(self, kind: str, event_id: str | None, ts: Any, **fields: Any) -> str | None:
        if not self.enabled:
            return None
        event = {"type": kind, "project": self.project,
                 "event_id": event_id or f"{kind}-{uuid.uuid4().hex}", "ts": _iso(ts)}
        event.update({k: v for k, v in fields.items() if v is not None})
        self.buffer.append(event)  # raises on NaN or unserialisable values; _safe catches it
        self._since_flush += 1
        if self.autoflush_every and self._since_flush >= self.autoflush_every:
            self.flush()
        return event["event_id"]

    # --- sending --------------------------------------------------------------------
    @_safe(default=lambda: FlushResult(error="internal"))
    def flush(self, timeout: float | None = None) -> FlushResult:
        """Send everything buffered. Never raises; the result says what happened.

        Gives up after `timeout` seconds (default flush_timeout); whatever is not sent
        stays buffered for the next flush.
        """
        if not self.enabled:
            return FlushResult(error="disabled")
        if not self.flush_enabled:
            return FlushResult(pending=self.buffer.pending(), error="flush-disabled")
        if not self._flush_lock.acquire(blocking=False):
            return FlushResult(pending=self.buffer.pending(), error="busy")
        try:
            self._since_flush = 0
            return self._flush(self._clock() + (timeout if timeout is not None
                                                else self.flush_timeout))
        finally:
            self._flush_lock.release()

    def _flush(self, deadline: float) -> FlushResult:
        files, events = self.buffer.claim()
        result = FlushResult()
        work: deque[list[dict]] = deque(self._chunks(events))
        leftover: list[dict] = []
        rejected: list[dict] = []
        while work:
            batch = work.popleft()
            out = self._send(batch, deadline)
            if out.kind == "ok":
                result.sent += out.accepted
                result.duplicates += out.duplicates
            elif out.kind == "permanent":
                if len(batch) > 1:  # find the bad event(s) without losing the good ones
                    half = len(batch) // 2
                    work.appendleft(batch[half:])
                    work.appendleft(batch[:half])
                else:
                    rejected.append(batch[0])
            else:  # transient, auth or config: stop and keep everything not yet sent
                leftover = batch + [e for rest in work for e in rest]
                result.error = {"transient": "unreachable"}.get(out.kind, out.kind)
                break
        # write the leftovers back BEFORE deleting the claimed files, so a crash in
        # between duplicates events (harmless) instead of losing them
        self.buffer.append_many(rejected, self.buffer.rejected_path)
        self.buffer.append_many(leftover)
        self.buffer.discard(files)
        result.rejected = len(rejected)
        result.pending = self.buffer.pending()
        if rejected:
            log.warning("opsreport: %d event(s) were refused by the dashboard and moved to %s",
                        len(rejected), self.buffer.rejected_path)
        return result

    @staticmethod
    def _chunks(events: list[dict]) -> list[list[dict]]:
        chunks: list[list[dict]] = []
        current: list[dict] = []
        size = 0
        for e in events:
            n = len(json.dumps(e, separators=(",", ":"), allow_nan=False)) + 1
            if current and (len(current) >= MAX_BATCH or size + n > MAX_BATCH_BYTES):
                chunks.append(current)
                current, size = [], 0
            current.append(e)
            size += n
        if current:
            chunks.append(current)
        return chunks

    def _send(self, batch: list[dict], deadline: float) -> _Outcome:
        payload = json.dumps({"schema_version": SCHEMA_VERSION, "events": batch},
                             separators=(",", ":"), allow_nan=False).encode()
        for attempt in range(self.max_attempts):
            remaining = deadline - self._clock()
            if remaining <= 0:
                return _Outcome("transient")  # out of time: keep it for next time
            wait = self.backoff * (2 ** attempt) * (0.5 + random.random())
            try:
                resp = transport.post(self.url, self.token, payload,
                                      timeout=min(self.timeout, remaining))
            except OSError as e:
                log.info("opsreport: network problem (%s), attempt %d", type(e).__name__,
                         attempt + 1)
            except ValueError:  # e.g. a header value http.client refuses: not worth retrying
                log.warning("opsreport: the request could not be built; check OPS_URL and "
                            "OPS_TOKEN")
                return _Outcome("config")
            else:
                status = resp.status
                if status == 200:
                    data = resp.json()
                    return _Outcome("ok", _int(data.get("accepted"), len(batch)),
                                    _int(data.get("duplicates"), 0))
                if status in (401, 403):
                    log.warning("opsreport: the dashboard refused the token (HTTP %d)", status)
                    return _Outcome("auth")
                if status in (404, 405, 410):
                    log.warning("opsreport: OPS_URL does not look like the dashboard (HTTP %d)",
                                status)
                    return _Outcome("config")
                if status != 429 and status < 500:
                    return _Outcome("permanent")  # 400, 413, 422, ...: retrying won't help
                retry_after = resp.headers.get("retry-after", "")
                if retry_after.isdigit():
                    wait = min(float(retry_after), RETRY_AFTER_CAP)
                log.info("opsreport: HTTP %d, attempt %d", status, attempt + 1)
            if attempt + 1 < self.max_attempts:
                if wait >= deadline - self._clock():
                    return _Outcome("transient")  # waiting would use up the budget: stop now
                self._sleep(wait)
        return _Outcome("transient")


def _int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default
