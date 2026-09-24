"""Tests for the Slack bot. No network: urlopen is replaced throughout.

The cases that matter are the ones that bite in production — a refusal that
arrives as HTTP 200, a rate limit, and the guarantee that a notification
failure cannot propagate into the trading path.

    python -m pytest execution/test_slack.py -q
"""

from __future__ import annotations

import io
import json
import sys
import urllib.error
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from execution import slack as mod  # noqa: E402


@pytest.fixture(autouse=True)
def isolate_config(monkeypatch, tmp_path):
    """No test may see the real .env or a real SLACK_* environment variable.

    Without this, whether a test passes depends on the machine it runs on —
    a webhook in the local .env reroutes `post()` away from the Web API, and
    the failure looks like a code bug rather than a leaked config.
    """
    monkeypatch.setattr(mod, "REPO_ROOT", tmp_path)
    for name in ("SLACK_BOT_TOKEN", "SLACK_CHANNEL", "SLACK_WEBHOOK_URL"):
        monkeypatch.delenv(name, raising=False)


class FakeResponse(io.BytesIO):
    """Context-manager shaped like what urlopen returns."""

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def fake_urlopen(responses):
    """Serve `responses` in order; record every request that arrives."""
    calls = []

    def _open(req, timeout=None):
        calls.append(req)
        item = responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return FakeResponse(json.dumps(item).encode())

    _open.calls = calls
    return _open


@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
    monkeypatch.setenv("SLACK_CHANNEL", "#test")
    return mod.Slack()


@pytest.fixture
def hooked(monkeypatch, tmp_path):
    """Webhook only — no bot token at all."""
    monkeypatch.setattr(mod, "REPO_ROOT", tmp_path)
    monkeypatch.setenv("SLACK_WEBHOOK_URL", "https://hooks.slack.com/services/T/B/xyz")
    return mod.Slack()


def test_post_sends_token_and_channel(client, monkeypatch):
    opener = fake_urlopen([{"ok": True, "channel": "C1", "ts": "1.2"}])
    monkeypatch.setattr(mod.urllib.request, "urlopen", opener)

    result = client.post("hello")

    assert result["ts"] == "1.2"
    req = opener.calls[0]
    assert req.full_url == "https://slack.com/api/chat.postMessage"
    assert req.headers["Authorization"] == "Bearer xoxb-test"
    assert json.loads(req.data) == {"channel": "#test", "text": "hello"}


def test_ok_false_raises_despite_http_200(client, monkeypatch):
    """The trap this module exists to avoid: a refusal wearing a 200."""
    monkeypatch.setattr(mod.urllib.request, "urlopen",
                        fake_urlopen([{"ok": False, "error": "not_in_channel"}]))

    with pytest.raises(mod.SlackError) as exc:
        client.post("hello")

    assert "not_in_channel" in str(exc.value)
    assert "/invite" in str(exc.value), "the actionable hint should survive"


def test_missing_scope_error_says_reinstall(client, monkeypatch):
    monkeypatch.setattr(mod.urllib.request, "urlopen",
                        fake_urlopen([{"ok": False, "error": "missing_scope",
                                       "needed": "chat:write"}]))

    with pytest.raises(mod.SlackError, match="REINSTALL"):
        client.post("hello")


def test_rate_limit_retries_then_succeeds(client, monkeypatch):
    limited = urllib.error.HTTPError(
        "url", 429, "Too Many Requests", {"Retry-After": "0"}, io.BytesIO(b""))
    opener = fake_urlopen([limited, {"ok": True, "ts": "9.9"}])
    monkeypatch.setattr(mod.urllib.request, "urlopen", opener)
    monkeypatch.setattr(mod.time, "sleep", lambda _: None)

    assert client.post("hello")["ts"] == "9.9"
    assert len(opener.calls) == 2


def test_rate_limit_gives_up_rather_than_hanging(client, monkeypatch):
    limited = [urllib.error.HTTPError("url", 429, "", {"Retry-After": "0"}, io.BytesIO(b""))
               for _ in range(mod.MAX_RETRIES)]
    monkeypatch.setattr(mod.urllib.request, "urlopen", fake_urlopen(limited))
    monkeypatch.setattr(mod.time, "sleep", lambda _: None)

    with pytest.raises(mod.SlackError, match="429"):
        client.post("hello")


def test_no_token_is_a_clear_error(monkeypatch, tmp_path):
    monkeypatch.delenv("SLACK_BOT_TOKEN", raising=False)
    # Point the .env lookup at an empty directory so a real local .env, if the
    # developer has one, cannot make this pass by accident.
    monkeypatch.setattr(mod, "REPO_ROOT", tmp_path)

    with pytest.raises(mod.SlackError, match="SLACK_WEBHOOK_URL"):
        mod.Slack()


def test_no_channel_is_a_clear_error(monkeypatch, tmp_path):
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
    monkeypatch.delenv("SLACK_CHANNEL", raising=False)
    monkeypatch.setattr(mod, "REPO_ROOT", tmp_path)

    with pytest.raises(mod.SlackError, match="SLACK_CHANNEL"):
        mod.Slack().post("hello")


def test_environment_beats_dotenv(monkeypatch, tmp_path):
    (tmp_path / ".env").write_text("SLACK_BOT_TOKEN=xoxb-from-file\n")
    monkeypatch.setattr(mod, "REPO_ROOT", tmp_path)
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-from-env")

    assert mod.Slack(channel="#c")._token == "xoxb-from-env"


def test_dotenv_used_when_environment_is_empty(monkeypatch, tmp_path):
    (tmp_path / ".env").write_text("SLACK_BOT_TOKEN=xoxb-from-file\n")
    monkeypatch.setattr(mod, "REPO_ROOT", tmp_path)
    monkeypatch.delenv("SLACK_BOT_TOKEN", raising=False)

    assert mod.Slack(channel="#c")._token == "xoxb-from-file"


def test_notify_swallows_every_failure(monkeypatch, capsys):
    """The whole point: a dead Slack must not reach the caller."""
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
    monkeypatch.setenv("SLACK_CHANNEL", "#test")
    monkeypatch.setattr(mod.urllib.request, "urlopen",
                        fake_urlopen([urllib.error.URLError("network is down")]))

    assert mod.notify("FAILED", "the run did not finish") is False
    assert "not sent" in capsys.readouterr().err


def test_notify_reports_success(monkeypatch):
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
    monkeypatch.setenv("SLACK_CHANNEL", "#test")
    opener = fake_urlopen([{"ok": True, "ts": "1"}])
    monkeypatch.setattr(mod.urllib.request, "urlopen", opener)

    assert mod.notify("DONE", "backtest finished") is True
    assert json.loads(opener.calls[0].data)["text"] == "*DONE* — backtest finished"


def test_upload_rejects_an_empty_file(client, tmp_path):
    empty = tmp_path / "nothing.csv"
    empty.write_text("")

    with pytest.raises(mod.SlackError, match="empty"):
        client.upload(empty)


def test_upload_walks_the_three_step_flow(client, monkeypatch, tmp_path):
    report = tmp_path / "report.html"
    report.write_text("<html>ok</html>")
    opener = fake_urlopen([
        {"ok": True, "upload_url": "https://files.slack.com/up/1", "file_id": "F1"},
        {},                                   # the bytes PUT, which returns no JSON body
        {"ok": True, "files": [{"id": "F1"}]},
    ])
    monkeypatch.setattr(mod.urllib.request, "urlopen", opener)

    result = client.upload(report, title="Backtest report", channel="C123")

    assert result["files"][0]["id"] == "F1"
    assert opener.calls[1].full_url == "https://files.slack.com/up/1"
    assert opener.calls[1].data == b"<html>ok</html>"
    complete = json.loads(opener.calls[2].data)
    assert complete["channel_id"] == "C123"
    assert complete["files"] == [{"id": "F1", "title": "Backtest report"}]


def test_channel_lookup_encodes_booleans_as_slack_expects(client, monkeypatch):
    """urlencode turns Python True into "True", which Slack ignores."""
    opener = fake_urlopen([{"ok": True, "channels": [{"name": "test", "id": "C7"}]}])
    monkeypatch.setattr(mod.urllib.request, "urlopen", opener)

    assert client.channel_id("#test") == "C7"
    assert b"exclude_archived=true" in opener.calls[0].data


def test_channel_lookup_pages_until_found(client, monkeypatch):
    opener = fake_urlopen([
        {"ok": True, "channels": [{"name": "other", "id": "C1"}],
         "response_metadata": {"next_cursor": "page2"}},
        {"ok": True, "channels": [{"name": "test", "id": "C2"}]},
    ])
    monkeypatch.setattr(mod.urllib.request, "urlopen", opener)

    assert client.channel_id("#test") == "C2"
    assert b"cursor=page2" in opener.calls[1].data


def test_channel_id_passes_through_a_raw_id(client, monkeypatch):
    """A C… id needs no lookup, so no scope and no call."""
    monkeypatch.setattr(mod.urllib.request, "urlopen", fake_urlopen([]))
    assert client.channel_id("C123") == "C123"


# -- webhook transport ------------------------------------------------------

class FakeTextResponse(io.BytesIO):
    """A webhook answers plain text, not JSON."""

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def fake_text_urlopen(bodies):
    calls = []

    def _open(req, timeout=None):
        calls.append(req)
        item = bodies.pop(0)
        if isinstance(item, Exception):
            raise item
        return FakeTextResponse(item)

    _open.calls = calls
    return _open


def test_webhook_posts_plain_text_body(hooked, monkeypatch):
    opener = fake_text_urlopen([b"ok"])
    monkeypatch.setattr(mod.urllib.request, "urlopen", opener)

    assert hooked.post("hello")["via"] == "webhook"
    req = opener.calls[0]
    assert req.full_url == "https://hooks.slack.com/services/T/B/xyz"
    assert json.loads(req.data) == {"text": "hello"}
    assert "Authorization" not in req.headers, "a webhook carries no bearer token"


def test_webhook_non_ok_body_raises(hooked, monkeypatch):
    """Success is the literal text 'ok'; anything else is a failure."""
    monkeypatch.setattr(mod.urllib.request, "urlopen", fake_text_urlopen([b"no_service"]))

    with pytest.raises(mod.SlackError, match="no_service"):
        hooked.post("hello")


def test_webhook_http_error_explains_no_service(hooked, monkeypatch):
    dead = urllib.error.HTTPError("url", 404, "Not Found", {}, io.BytesIO(b"no_service"))
    monkeypatch.setattr(mod.urllib.request, "urlopen", fake_text_urlopen([dead]))

    with pytest.raises(mod.SlackError, match="no longer exists"):
        hooked.post("hello")


def test_webhook_needs_no_scopes_so_no_token_is_fine(hooked):
    assert hooked._token is None
    assert hooked.webhook


def test_webhook_cannot_target_another_channel(hooked):
    with pytest.raises(mod.SlackError, match="fixed to its own channel"):
        hooked.post("hello", channel="#elsewhere")


def test_webhook_cannot_thread(hooked):
    with pytest.raises(mod.SlackError, match="bot token"):
        hooked.post("hello", thread_ts="1.2")


def test_webhook_cannot_upload(hooked, tmp_path):
    f = tmp_path / "a.csv"
    f.write_text("x")
    with pytest.raises(mod.SlackError, match="needs a bot token"):
        hooked.upload(f)


def test_webhook_wins_over_token_for_a_plain_post(monkeypatch, tmp_path):
    """Both configured: the plain path takes the one that cannot fail on scope."""
    monkeypatch.setattr(mod, "REPO_ROOT", tmp_path)
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
    monkeypatch.setenv("SLACK_CHANNEL", "#test")
    monkeypatch.setenv("SLACK_WEBHOOK_URL", "https://hooks.slack.com/services/T/B/xyz")
    opener = fake_text_urlopen([b"ok"])
    monkeypatch.setattr(mod.urllib.request, "urlopen", opener)

    assert mod.Slack().post("hello")["via"] == "webhook"
    assert "hooks.slack.com" in opener.calls[0].full_url


def test_token_is_used_when_the_post_names_a_channel(monkeypatch, tmp_path):
    """A channel argument is something only the Web API can honour."""
    monkeypatch.setattr(mod, "REPO_ROOT", tmp_path)
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
    monkeypatch.setenv("SLACK_WEBHOOK_URL", "https://hooks.slack.com/services/T/B/xyz")
    opener = fake_urlopen([{"ok": True, "ts": "1"}])
    monkeypatch.setattr(mod.urllib.request, "urlopen", opener)

    mod.Slack().post("hello", channel="#other")
    assert opener.calls[0].full_url.endswith("chat.postMessage")


def test_notify_works_through_a_webhook(hooked, monkeypatch):
    monkeypatch.setattr(mod.urllib.request, "urlopen", fake_text_urlopen([b"ok"]))
    assert mod.notify("DONE", "nightly refresh finished") is True


def test_cli_reports_a_webhook_post_without_none_fields(hooked, monkeypatch, capsys):
    """A webhook answers only 'ok', so there is no channel or ts to print."""
    monkeypatch.setattr(mod.urllib.request, "urlopen", fake_text_urlopen([b"ok"]))
    monkeypatch.setattr(sys, "argv", ["slack.py", "hello"])

    assert mod.main() == 0
    out = capsys.readouterr().out
    assert "posted via webhook" in out
    assert "None" not in out
