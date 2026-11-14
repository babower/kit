"""kit-asana against FakeAsana: real HTTP on localhost."""

from __future__ import annotations

import logging
from datetime import UTC, date, datetime

import pytest

from kit_asana import AsanaAuthError, AsanaError, AsanaNotFound, Client
from kit_asana.testing import FakeAsana


@pytest.fixture
def fake():
    f = FakeAsana()
    yield f
    f.close()


@pytest.fixture
def waits():
    return []


@pytest.fixture
def client(fake, waits):
    return Client(fake.token, base_url=fake.base_url, sleep=waits.append, backoff=0.01)


@pytest.fixture
def project(fake):
    return fake.add_project("Home")


def test_create_read_update_complete(client, fake, project):
    inbox = fake.last_section
    t = client.create_task(project=project, section=inbox, name="Furnace: filter",
                           notes="line 1\nsmart-home: x", due_on=date(2026, 12, 1))
    assert t["name"] == "Furnace: filter" and t["due_on"] == "2026-12-01"
    assert t["memberships"][0]["section"]["gid"] == inbox
    got = client.get_task(t["gid"], fields=["name", "completed"])
    assert got["gid"] == t["gid"] and got["completed"] is False
    client.update_task(t["gid"], due_on="2026-12-15")
    assert fake.tasks[t["gid"]]["due_on"] == "2026-12-15"
    done = client.complete_task(t["gid"])
    assert done["completed"] is True and done["completed_at"]
    client.add_comment(t["gid"], "Logged in Smart Home")
    assert fake.stories[t["gid"]] == ["Logged in Smart Home"]


def test_create_without_section(client, project):
    t = client.create_task(project=project, name="x")
    assert t["projects"] == [{"gid": project}]


def test_tasks_in_project_paginates_and_filters(client, fake, project):
    fake.page_size = 2
    gids = [client.create_task(project=project, name=f"t{i}")["gid"] for i in range(5)]
    assert [t["gid"] for t in client.tasks_in_project(project)] == gids
    fake.complete(gids[1])
    open_ = [t["gid"] for t in client.tasks_in_project(project, completed_since="now")]
    assert gids[1] not in open_ and len(open_) == 4
    later = datetime.now(UTC)
    fake.complete(gids[3])
    changed = [t["gid"] for t in client.tasks_in_project(project, modified_since=later)]
    assert changed == [gids[3]]
    assert ("GET", "/api/1.0/tasks") in fake.requests


def test_a_deleted_task_is_not_found(client, fake, project):
    gid = client.create_task(project=project, name="x")["gid"]
    fake.delete(gid)
    with pytest.raises(AsanaNotFound):
        client.get_task(gid)


def test_bad_token_is_an_auth_error_without_the_token(fake, project):
    c = Client("wrong-token-123", base_url=fake.base_url, sleep=lambda s: None)
    with pytest.raises(AsanaAuthError) as e:
        c.get_task("1")
    assert e.value.status == 401 and "wrong-token" not in str(e.value)
    assert "wrong-token" not in repr(c)


def test_rate_limit_honours_retry_after(client, fake, project, waits):
    fake.fail_next(429, times=2, retry_after="2")
    client.create_task(project=project, name="x")
    assert waits == [2.0, 2.0]


def test_long_retry_after_gives_up_at_once(client, fake, project, waits):
    fake.fail_next(429, retry_after="600")
    with pytest.raises(AsanaError) as e:
        client.get_task("1")
    assert e.value.status == 429 and waits == []


def test_reads_and_updates_retry_server_errors(client, fake, project, waits):
    gid = client.create_task(project=project, name="x")["gid"]
    fake.fail_next(503, times=2)
    assert client.update_task(gid, name="y")["name"] == "y"
    assert len(waits) == 2


def test_creating_does_not_retry_server_errors(client, fake, project, waits):
    """The first attempt may have worked; a blind retry could create a duplicate."""
    fake.fail_next(503)
    with pytest.raises(AsanaError) as e:
        client.create_task(project=project, name="x")
    assert e.value.status == 503 and waits == [] and not fake.tasks


def test_gives_up_after_max_attempts(client, fake, project, waits):
    fake.fail_next(500, times=10)
    with pytest.raises(AsanaError) as e:
        client.get_task("1")
    assert e.value.status == 500 and len(waits) == 3  # 4 attempts


def test_network_error_is_typed_and_safe():
    c = Client("tok", base_url="http://127.0.0.1:9/api/1.0", sleep=lambda s: None,
               max_attempts=2, timeout=1)
    with pytest.raises(AsanaError) as e:
        c.get_task("1")
    assert e.value.status == 0 and "unreachable" in str(e.value)


def test_error_messages_never_carry_task_text(client, fake, project):
    fake.fail_next(400)
    with pytest.raises(AsanaError) as e:
        client.create_task(project=project, name="SECRET-NAME", notes="SECRET-NOTES")
    assert "SECRET" not in str(e.value)


@pytest.mark.parametrize("bad", ["", "  ", "tok en", "tok\nen"])
def test_unusable_tokens_are_refused(bad):
    with pytest.raises(ValueError) as e:
        Client(bad)
    assert bad.strip() == "" or bad not in str(e.value)


@pytest.mark.parametrize("url", ["http://example.com/api/1.0", "ftp://x", "nonsense"])
def test_plain_http_only_to_localhost(url):
    with pytest.raises(ValueError):
        Client("tok", base_url=url)


@pytest.mark.parametrize("gid", ["../projects", "1/stories", "", "abc", None])
def test_gids_are_validated(client, gid):
    with pytest.raises(ValueError):
        client.get_task(gid)


def test_nothing_is_logged(client, fake, project, caplog):
    caplog.set_level(logging.DEBUG)
    client.create_task(project=project, name="private name")
    assert "private name" not in caplog.text and fake.token not in caplog.text


def test_fake_rejects_unknown_projects_and_sections(client, fake, project):
    with pytest.raises(AsanaError):
        client.create_task(project="999", name="x")
    other = fake.add_project("Other")
    with pytest.raises(AsanaError):
        client.create_task(project=project, section=fake.last_section, name="x")
    assert other
