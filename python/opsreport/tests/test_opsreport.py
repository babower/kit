import json
import threading
from datetime import UTC, date, datetime

import pytest
from mockserver import MockDashboard, ok

from opsreport import Reporter
from opsreport.transport import events_url

TOKEN = "opsd_test-token"


def reporter(tmp_path, url, **kw):
    sleeps: list[float] = []
    kw.setdefault("sleep", sleeps.append)
    r = Reporter("demo-project", url=url, token=TOKEN, buffer_dir=tmp_path, **kw)
    r.sleeps = sleeps
    return r


def usage(r, n=1, **kw):
    for i in range(n):
        r.usage(provider="anthropic", op="write", event_id=kw.pop("event_id", f"u{i}"),
                input_tokens=10, **kw)


def buffered(tmp_path):
    return sum(len(f.read_text().splitlines()) for f in tmp_path.glob("demo-project.*jsonl")
               if "rejected" not in f.name)


# --- off by default ----------------------------------------------------------------

def test_does_nothing_without_url_or_token(tmp_path, monkeypatch):
    monkeypatch.delenv("OPS_URL", raising=False)
    monkeypatch.delenv("OPS_TOKEN", raising=False)
    r = Reporter("demo-project", buffer_dir=tmp_path)
    assert not r.enabled and r.usage(provider="a", op="b") is None
    assert r.flush().error == "disabled"
    assert list(tmp_path.iterdir()) == []


def test_reads_the_environment(tmp_path, monkeypatch):
    with MockDashboard() as srv:
        monkeypatch.setenv("OPS_URL", srv.url)
        monkeypatch.setenv("OPS_TOKEN", TOKEN)
        monkeypatch.setenv("OPS_BUFFER_DIR", str(tmp_path))
        r = Reporter("demo-project")
        assert r.enabled
        r.metric(series="mood.share", value=0.2, event_id="m1")
        assert r.flush().sent == 1


@pytest.mark.parametrize("bad", ["http://example.com", "ftp://x.com", "not a url", "https://"])
def test_refuses_a_url_that_would_leak_the_token(tmp_path, bad):
    r = Reporter("demo-project", url=bad, token=TOKEN, buffer_dir=tmp_path)
    assert not r.enabled and r.usage(provider="a", op="b") is None


@pytest.mark.parametrize("pasted", [f"  {TOKEN}\n", f"\t{TOKEN} "])
def test_surrounding_whitespace_in_the_token_is_ignored(tmp_path, pasted):
    with MockDashboard() as srv:
        r = Reporter("demo-project", url=srv.url, token=pasted, buffer_dir=tmp_path)
        assert r.enabled and r.token == TOKEN
        r.metric(series="x", value=1, event_id="m1")
        assert r.flush().sent == 1


@pytest.mark.parametrize("broken", [f"{TOKEN[:12]}\n{TOKEN}", f"{TOKEN} {TOKEN}"])
def test_a_token_with_inner_spaces_or_line_breaks_switches_reporting_off(tmp_path, broken,
                                                                          caplog):
    r = Reporter("demo-project", url="http://localhost:9", token=broken, buffer_dir=tmp_path)
    assert not r.enabled and r.usage(provider="a", op="b") is None
    assert "spaces or line breaks" in caplog.text and TOKEN not in caplog.text


def test_a_request_that_cannot_be_built_is_a_config_problem(tmp_path, monkeypatch):
    from opsreport import transport

    def refuse(*a, **k):
        raise ValueError("Invalid header value")

    monkeypatch.setattr(transport, "post", refuse)
    r = Reporter("demo-project", url="http://localhost:9", token=TOKEN, buffer_dir=tmp_path)
    r.metric(series="x", value=1, event_id="m1")
    res = r.flush()
    assert (res.error, res.pending) == ("config", 1)


def test_stage_events(tmp_path):
    with MockDashboard() as srv:
        r = reporter(tmp_path, srv.url)
        eid = r.stage(item_id="post-1", stage="published", state="done",
                      labels={"platform": "instagram"}, event_id="post-1:published:instagram")
        assert eid == "post-1:published:instagram" and r.flush().sent == 1
        sent = srv.requests[0]["body"]["events"][0]
        assert (sent["type"], sent["stage"], sent["state"], sent["labels"]) == (
            "stage", "published", "done", {"platform": "instagram"})


def test_url_forms():
    assert events_url("https://h.example") == "https://h.example/v1/events"
    assert events_url("https://h.example/") == "https://h.example/v1/events"
    assert events_url("https://h.example/v1/events") == "https://h.example/v1/events"
    assert events_url("http://localhost:8000") == "http://localhost:8000/v1/events"


# --- sending ---------------------------------------------------------------------------

def test_sends_events_with_the_token_and_clears_the_buffer(tmp_path):
    with MockDashboard() as srv:
        r = reporter(tmp_path, srv.url)
        usage(r, 3)
        assert buffered(tmp_path) == 3
        res = r.flush()
        assert (res.sent, res.duplicates, res.pending, res.error) == (3, 0, 0, None)
        req = srv.requests[0]
        assert req["path"] == "/v1/events"
        assert req["headers"]["authorization"] == f"Bearer {TOKEN}"
        assert req["body"]["schema_version"] == 1
        assert req["body"]["events"][0]["project"] == "demo-project"
        assert buffered(tmp_path) == 0


def test_every_event_type_has_the_documented_shape(tmp_path):
    with MockDashboard() as srv:
        r = reporter(tmp_path, srv.url)
        when = datetime(2026, 11, 10, 7, 30, tzinfo=UTC)
        r.usage(provider="elevenlabs", op="voice", units=812, unit_name="characters",
                cost_usd=0.24, item_id="p1", ts=when, event_id="u")
        r.run(status="ok", duration_seconds=12.5, errors={"gdelt": "timeout"}, ts=when,
              event_id="r")
        r.outcome(item_id="p1", horizon_hours=72, metrics={"views": 1200}, reward=1.2,
                  event_id="p1:72h")
        r.budget(effective_from=date(2026, 11, 1), monthly_budget_usd=60,
                 fixed_monthly_usd={"blotato": 29}, event_id="b")
        r.metric(series="mood.share", value=0.3, labels={"emotion": "anger"}, event_id="m")
        r.flush()
    events = {e["type"]: e for e in srv.requests[0]["body"]["events"]}
    assert set(events) == {"usage", "run", "outcome", "budget", "metric"}
    assert events["usage"]["ts"] == "2026-11-10T07:30:00+00:00"
    assert events["budget"]["effective_from"] == "2026-11-01T00:00:00+00:00"
    assert "item_id" not in events["run"] and "reward" in events["outcome"]


def test_naive_datetimes_are_taken_as_utc(tmp_path):
    with MockDashboard() as srv:
        r = reporter(tmp_path, srv.url)
        r.metric(series="x", value=1, ts=datetime(2026, 1, 1, 12, 0), event_id="m")
        r.flush()
    assert srv.requests[0]["body"]["events"][0]["ts"].endswith("+00:00")


def test_large_buffers_go_out_in_batches_of_500(tmp_path):
    with MockDashboard() as srv:
        r = reporter(tmp_path, srv.url)
        for i in range(1200):
            r.metric(series="x", value=i, event_id=f"m{i}")
        res = r.flush()
    assert res.sent == 1200
    assert [len(q["body"]["events"]) for q in srv.requests] == [500, 500, 200]


def test_big_payloads_are_split_by_size(tmp_path):
    with MockDashboard() as srv:
        r = reporter(tmp_path, srv.url)
        for i in range(10):  # ~250 KB each: more than 1.5 MB in total
            r.run(status="ok", details={"blob": "x" * 250_000}, event_id=f"r{i}")
        assert r.flush().sent == 10
    assert len(srv.requests) > 1


def test_autoflush(tmp_path):
    with MockDashboard() as srv:
        r = reporter(tmp_path, srv.url, autoflush_every=3)
        for i in range(7):
            r.metric(series="x", value=i, event_id=f"m{i}")
        assert len(srv.event_ids) == 6 and buffered(tmp_path) == 1


def test_context_manager_flushes_on_exit(tmp_path):
    with MockDashboard() as srv:
        with reporter(tmp_path, srv.url) as r:
            usage(r, 2)
        assert len(srv.event_ids) == 2


def test_flushing_can_be_turned_off(tmp_path, monkeypatch):
    with MockDashboard() as srv:
        r = reporter(tmp_path, srv.url, flush_enabled=False)
        usage(r, 2)
        res = r.flush()
        assert (res.error, res.pending) == ("flush-disabled", 2) and not srv.requests
        monkeypatch.setenv("OPS_FLUSH", "off")
        r2 = Reporter("demo-project", url=srv.url, token=TOKEN, buffer_dir=tmp_path)
        assert not r2.flush_enabled and not srv.requests
        assert buffered(tmp_path) == 2  # still buffered, ready for a later flush


# --- outages, retries and duplicates ---------------------------------------------------------

def test_an_outage_keeps_events_buffered_and_never_raises(tmp_path):
    with MockDashboard() as srv:
        dead_url = srv.url  # the server is shut down when the block exits
    r = reporter(tmp_path, dead_url, max_attempts=2)
    usage(r, 3)
    res = r.flush()
    assert res.error == "unreachable" and (res.sent, res.pending) == (0, 3)
    assert len(r.sleeps) == 1  # backed off once between the two attempts

    with MockDashboard() as srv:  # the dashboard comes back
        r.url = events_url(srv.url)
        res = r.flush()
        assert (res.sent, res.pending, res.error) == (3, 0, None)
        assert sorted(srv.event_ids) == ["u0", "u1", "u2"]


def test_retries_5xx_with_growing_backoff_then_succeeds(tmp_path):
    def handler(n, body, headers):
        return (503, {}, {}) if n < 3 else ok(len(body["events"]))
    with MockDashboard(handler) as srv:
        r = reporter(tmp_path, srv.url, max_attempts=4, backoff=1.0)
        usage(r, 2)
        res = r.flush()
        assert res.sent == 2 and len(srv.requests) == 3
    # backoff * 2**attempt, times a random jitter in [0.5, 1.5): the first wait is in
    # [0.5, 1.5) and the second in [1, 3), so the second can be shorter than the first.
    assert len(r.sleeps) == 2
    assert 0.5 <= r.sleeps[0] < 1.5 and 1.0 <= r.sleeps[1] < 3.0


def test_gives_up_after_max_attempts_and_keeps_the_events(tmp_path):
    with MockDashboard(lambda n, b, h: (500, {}, {})) as srv:
        r = reporter(tmp_path, srv.url, max_attempts=3)
        usage(r, 2)
        res = r.flush()
        assert res.error == "unreachable" and res.pending == 2 and len(srv.requests) == 3


def test_429_honours_retry_after(tmp_path):
    replies = iter([(429, {"Retry-After": "2"}, {}), (429, {"Retry-After": "25"}, {})])

    def handler(n, body, headers):
        return next(replies, None) or ok(len(body["events"]))
    with MockDashboard(handler) as srv:
        r = reporter(tmp_path, srv.url, max_attempts=3, flush_timeout=100)
        usage(r)
        assert r.flush().sent == 1
    assert r.sleeps == [2.0, 25.0]


def test_retry_after_is_capped_at_30_seconds(tmp_path):
    replies = iter([(429, {"Retry-After": "9999"}, {})])

    def handler(n, body, headers):
        return next(replies, None) or ok(len(body["events"]))
    with MockDashboard(handler) as srv:
        r = reporter(tmp_path, srv.url, flush_timeout=1000)
        usage(r)
        assert r.flush().sent == 1
    assert r.sleeps == [30.0]


def test_a_long_retry_after_gives_up_at_once_instead_of_blocking(tmp_path):
    with MockDashboard(lambda n, b, h: (429, {"Retry-After": "600"}, {})) as srv:
        r = reporter(tmp_path, srv.url, flush_timeout=20)
        usage(r, 2)
        res = r.flush()
        assert res.error == "unreachable" and res.pending == 2
        assert r.sleeps == [] and len(srv.requests) == 1  # no sleeping, no hammering


def test_a_lost_reply_is_safe_because_resending_is_idempotent(tmp_path):
    """The dashboard stored the events but the client never heard back."""
    with MockDashboard() as srv:
        r = reporter(tmp_path, srv.url)
        usage(r, 3)
        first_try = r.buffer.claim()  # the client claims, sends, and "crashes" before cleanup
        events = first_try[1]
        srv.handler(1, {"events": events}, {})  # the server stored them (and remembers ids)
        for f in first_try[0]:  # the claimed file is left behind, as after a crash
            f.touch()
        import os
        import time
        old = time.time() - 120
        for f in first_try[0]:
            os.utime(f, (old, old))
        res = r.flush()  # picks the abandoned file up and resends
        assert (res.sent, res.duplicates, res.pending) == (0, 3, 0)
        assert sorted(srv.event_ids) == ["u0", "u1", "u2"]  # each id sent exactly... once stored


def test_recent_claims_by_other_flushes_are_left_alone(tmp_path):
    with MockDashboard() as srv:
        r = reporter(tmp_path, srv.url)
        usage(r, 2)
        files, _ = r.buffer.claim()  # claimed seconds ago: someone is sending it
        assert r.flush().sent == 0 and not srv.requests
        assert files[0].exists()


# --- problems that retrying cannot fix --------------------------------------------------------

def test_a_bad_token_keeps_events_and_stops(tmp_path):
    with MockDashboard(lambda n, b, h: (401, {}, {})) as srv:
        r = reporter(tmp_path, srv.url)
        usage(r, 2)
        res = r.flush()
        assert (res.error, res.pending, len(srv.requests)) == ("auth", 2, 1)


def test_a_wrong_url_is_reported_as_a_config_problem(tmp_path):
    with MockDashboard(lambda n, b, h: (404, {}, {})) as srv:
        r = reporter(tmp_path, srv.url)
        usage(r)
        assert r.flush().error == "config"


def test_one_bad_event_is_isolated_and_the_rest_are_delivered(tmp_path):
    def handler(n, body, headers):
        if any(e["event_id"] == "bad" for e in body["events"]):
            return 422, {}, {"detail": []}
        return ok(len(body["events"]))
    with MockDashboard(handler) as srv:
        r = reporter(tmp_path, srv.url)
        for i in range(9):
            r.usage(provider="a", op="b", event_id=f"u{i}")
        r.usage(provider="a", op="b", event_id="bad")
        res = r.flush()
        assert (res.sent, res.rejected, res.pending, res.error) == (9, 1, 0, None)
    dead = [json.loads(line) for line in
            (tmp_path / "demo-project.rejected.jsonl").read_text().splitlines()]
    assert [e["event_id"] for e in dead] == ["bad"]
    assert r.buffer.rejected() == 1


def test_a_413_is_split_until_it_fits(tmp_path):
    def handler(n, body, headers):
        return ok(len(body["events"])) if len(body["events"]) <= 4 else (413, {}, {})
    with MockDashboard(handler) as srv:
        r = reporter(tmp_path, srv.url)
        usage(r, 10)
        assert r.flush().sent == 10


# --- never raises into the caller -----------------------------------------------------------------

def test_bad_values_do_not_raise_and_do_not_poison_the_buffer(tmp_path):
    with MockDashboard() as srv:
        r = reporter(tmp_path, srv.url)
        assert r.metric(series="x", value=float("nan")) is None       # not valid JSON
        assert r.metric(series="x", value=float("inf")) is None
        assert r.usage(provider="a", op="b", item_id=object()) is None  # not serialisable
        assert r.metric(series="ok", value=1, event_id="good") == "good"
        assert r.flush().sent == 1


def test_an_unwritable_buffer_does_not_raise(tmp_path):
    blocker = tmp_path / "file"
    blocker.write_text("x")  # a file where the buffer directory should be
    r = Reporter("demo-project", url="http://127.0.0.1:9", token=TOKEN, buffer_dir=blocker / "d")
    assert r.metric(series="x", value=1) is None
    assert r.flush().ok in (True, False)  # returns a result, no exception


def test_a_corrupt_buffer_line_is_skipped(tmp_path):
    with MockDashboard() as srv:
        r = reporter(tmp_path, srv.url)
        usage(r, 2)
        with (tmp_path / "demo-project.jsonl").open("a") as f:
            f.write('{"torn": \n')
        usage(r, 1, event_id="u9")
        assert r.flush().sent == 3


def test_the_flush_deadline_is_respected(tmp_path):
    now = [0.0]
    with MockDashboard(lambda n, b, h: (503, {}, {})) as srv:
        r = reporter(tmp_path, srv.url, max_attempts=50, clock=lambda: now[0],
                     sleep=lambda s: now.__setitem__(0, now[0] + s))
        usage(r, 2)
        res = r.flush(timeout=3.0)
        assert res.error == "unreachable" and res.pending == 2
        assert now[0] <= 3.0 + 1e-6 and len(srv.requests) < 50


def test_concurrent_writers_do_not_corrupt_the_buffer(tmp_path):
    with MockDashboard() as srv:
        r = reporter(tmp_path, srv.url)

        def work(t):
            for i in range(50):
                r.metric(series="x", value=i, event_id=f"t{t}-{i}")
        threads = [threading.Thread(target=work, args=(t,)) for t in range(8)]
        [t.start() for t in threads]
        [t.join() for t in threads]
        assert buffered(tmp_path) == 400
        assert r.flush().sent == 400 and len(set(srv.event_ids)) == 400


def test_the_token_is_never_logged(tmp_path, caplog):
    caplog.set_level("DEBUG", logger="opsreport")
    with MockDashboard(lambda n, b, h: (401, {}, {})) as srv:
        r = reporter(tmp_path, srv.url)
        usage(r)
        r.flush()
    assert TOKEN not in caplog.text and "u0" not in caplog.text
