"""Slack bot for this repo — posts run verdicts and daily results to a channel.

    from execution.slack import notify, Slack

    notify("DONE", "nightly refresh finished")      # never raises
    Slack().post("hello")                            # raises on failure

    python execution/slack.py --check                # verify token + scopes
    python execution/slack.py "deploy finished"      # post from the shell
    python execution/slack.py --file out/report.html --title "Backtest report"

Two transports
--------------
**Incoming webhook** (`SLACK_WEBHOOK_URL`) needs no scopes and no install
dance, but it is welded to the one channel it was created for and cannot
upload files or thread replies. **Bot token** (`SLACK_BOT_TOKEN`) can do all of
that, but needs `chat:write` and an invite.

`post()` prefers the webhook when one is set and the call does not name a
channel or a thread, because that is the path that cannot fail on a scope.
Anything the webhook cannot do falls through to the token, and says so plainly
if the token is missing.

Setup, once
-----------
1. api.slack.com/apps -> your app -> OAuth & Permissions.
2. Under *Bot Token Scopes* add `chat:write`. Add `files:write` only if you
   want `upload()`, and `channels:read` only if you want to name a channel with
   `#hash` instead of its `C…` id when uploading.
3. Install to the workspace and copy the **Bot** User OAuth Token (`xoxb-…`).
   The User token (`xoxp-…`) also works but posts as you, not as the bot.
4. Put it in the repo's `.env` (gitignored) as SLACK_BOT_TOKEN, and set
   SLACK_CHANNEL to the destination.
5. Invite the bot: type `/invite @your-bot-name` in the channel. Skipping this
   is the single most common failure — the token is valid, the scope is right,
   and the post still fails with `not_in_channel`.

Why stdlib instead of slack_sdk
-------------------------------
The same reason research/quantdata/client.py is stdlib: one more pinned
dependency to carry for what is a single JSON POST. `urllib` is enough.

Why failures are swallowed at the notify() seam
-----------------------------------------------
A Slack outage, an expired token, or a renamed channel must never be able to
take down the job doing the actual work. A missed message is an annoyance; a
crashed unattended run is a real problem, and the more so the closer it sits to
money. So `notify()` returns False and prints; only the explicit `Slack`
methods raise, and those are for interactive and test use.

The 200-that-means-no
---------------------
Slack answers a rejected call with HTTP 200 and `{"ok": false, "error": …}`.
Treating that as success is the classic bug here, so `_api` checks `ok` rather
than the status code. Credentials resolve the way backtest/config.py resolves
Alpaca's: process environment first, then the repo's `.env`.
"""

from __future__ import annotations

import argparse
import json
import mimetypes
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from dotenv import dotenv_values

REPO_ROOT = Path(__file__).resolve().parent.parent
API = "https://slack.com/api"

#: Slack's own guidance is roughly one message per second per channel. Bursts
#: are tolerated briefly, so a short bounded retry covers the realistic case
#: (a loop posting one line per symbol) without turning an outage into a hang.
MAX_RETRIES = 3

#: Errors worth explaining, because the API's one-word answer does not say what
#: to actually do about it.
HINTS = {
    "not_in_channel": "the bot is not a member — run `/invite @your-bot` in that channel",
    "channel_not_found": "no such channel, or the bot cannot see it. Private channels need an invite first",
    "invalid_auth": "token rejected — it may have been revoked or regenerated",
    "account_inactive": "the token's app was uninstalled from the workspace",
    "missing_scope": "the token lacks a required scope. Add it under OAuth & Permissions, then REINSTALL — scope changes need a reinstall to take effect",
    "not_allowed_token_type": "this looks like a user token where a bot token is needed (or vice versa)",
    "is_archived": "the channel is archived",
    "msg_too_long": "over Slack's 40,000-character limit for one message",
    "no_service": "this webhook no longer exists — it was deleted, or its app was uninstalled",
    "no_text": "the message body was empty",
    "invalid_payload": "Slack could not parse the message JSON",
    "ratelimited": "rate limited beyond the retry budget",
}


class SlackError(RuntimeError):
    """Slack accepted the request and refused it, or the transport failed."""


def _resolve(name: str, default: str | None = None) -> str | None:
    """Process environment first, then the repo's .env. Never the repo itself."""
    value = os.environ.get(name)
    if value and value.strip():
        return value.strip()
    env_file = REPO_ROOT / ".env"
    if env_file.is_file():
        value = dotenv_values(env_file).get(name)
        if value and str(value).strip():
            return str(value).strip()
    return default


class Slack:
    """Thin Slack Web API wrapper. Raises on every failure."""

    def __init__(self, token: str | None = None, channel: str | None = None,
                 webhook: str | None = None, timeout: int = 15) -> None:
        self._token = token or _resolve("SLACK_BOT_TOKEN")
        self.webhook = webhook or _resolve("SLACK_WEBHOOK_URL")
        if not self._token and not self.webhook:
            raise SlackError(
                "No Slack credentials. Set SLACK_WEBHOOK_URL (simplest — no "
                "scopes needed) or SLACK_BOT_TOKEN in the environment or in "
                f"{REPO_ROOT / '.env'}. The bot token is the Bot User OAuth "
                "Token (xoxb-…) from your app's OAuth & Permissions page."
            )
        self.channel = channel or _resolve("SLACK_CHANNEL")
        self._timeout = timeout

    # -- transport ------------------------------------------------------------
    def _api(self, method: str, payload: dict, form: bool = False) -> dict:
        """POST to a Web API method and return the result, or raise.

        `form` picks x-www-form-urlencoded, which a few endpoints
        (files.getUploadURLExternal among them) require instead of JSON.
        """
        if not self._token:
            raise SlackError(
                f"{method} needs a bot token (SLACK_BOT_TOKEN). A webhook can "
                "only post text to its own channel."
            )
        url = f"{API}/{method}"
        if form:
            body = urllib.parse.urlencode(payload).encode()
            content_type = "application/x-www-form-urlencoded"
        else:
            body = json.dumps(payload).encode()
            content_type = "application/json; charset=utf-8"

        for attempt in range(MAX_RETRIES):
            req = urllib.request.Request(
                url,
                data=body,
                headers={
                    "Authorization": f"Bearer {self._token}",
                    "Content-Type": content_type,
                },
            )
            try:
                with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                    data = json.loads(resp.read())
            except urllib.error.HTTPError as exc:
                # 429 is the only status Slack uses for flow control, and it
                # always carries Retry-After. Anything else is a real failure.
                if exc.code == 429 and attempt < MAX_RETRIES - 1:
                    time.sleep(int(exc.headers.get("Retry-After", 1)))
                    continue
                raise SlackError(f"{method}: HTTP {exc.code} {exc.read()[:200]!r}") from exc
            except urllib.error.URLError as exc:
                raise SlackError(f"{method}: {exc.reason}") from exc

            if data.get("ok"):
                return data
            error = data.get("error", "unknown_error")
            if error == "ratelimited" and attempt < MAX_RETRIES - 1:
                time.sleep(1)
                continue
            hint = HINTS.get(error)
            detail = data.get("needed") or data.get("response_metadata", {})
            raise SlackError(
                f"{method}: {error}" + (f" — {hint}" if hint else "")
                + (f" [{detail}]" if detail else "")
            )
        raise SlackError(f"{method}: gave up after {MAX_RETRIES} attempts")

    def _target(self, channel: str | None) -> str:
        target = channel or self.channel
        if not target:
            raise SlackError(
                "No channel. Pass channel=… or set SLACK_CHANNEL in .env "
                "(either '#name' or a 'C…' id)."
            )
        return target

    # -- verbs ----------------------------------------------------------------
    def auth_test(self) -> dict:
        """Who this token is. Cheapest possible proof it works."""
        return self._api("auth.test", {})

    def _post_webhook(self, text: str, blocks: list | None = None) -> dict:
        """Post through an incoming webhook.

        Unlike the Web API this answers `ok` as **plain text**, not JSON, and
        signals failure with a non-200 whose body is the one-word reason. So
        neither the `ok`-field check nor the JSON decode from `_api` applies.
        """
        payload: dict = {"text": text}
        if blocks:
            payload["blocks"] = blocks
        body = json.dumps(payload).encode()

        for attempt in range(MAX_RETRIES):
            req = urllib.request.Request(
                self.webhook, data=body,
                headers={"Content-Type": "application/json"})
            try:
                with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                    answer = resp.read().decode(errors="replace").strip()
            except urllib.error.HTTPError as exc:
                reason = exc.read().decode(errors="replace").strip()
                if exc.code == 429 and attempt < MAX_RETRIES - 1:
                    time.sleep(int(exc.headers.get("Retry-After", 1)))
                    continue
                hint = HINTS.get(reason)
                raise SlackError(f"webhook: HTTP {exc.code} {reason[:120]}"
                                 + (f" — {hint}" if hint else "")) from exc
            except urllib.error.URLError as exc:
                raise SlackError(f"webhook: {exc.reason}") from exc

            if answer != "ok":
                raise SlackError(f"webhook: {answer[:200]}")
            return {"ok": True, "via": "webhook"}
        raise SlackError(f"webhook: gave up after {MAX_RETRIES} attempts")

    def post(self, text: str, channel: str | None = None,
             thread_ts: str | None = None, blocks: list | None = None) -> dict:
        """Post a message. Returns the response, whose `ts` threads replies.

        Routes to the webhook when one is configured and neither a channel nor
        a thread is named — a webhook can serve neither.
        """
        if self.webhook and not channel and not thread_ts:
            return self._post_webhook(text, blocks)
        if not self._token:
            need = "a different channel" if channel else "a threaded reply"
            raise SlackError(
                f"Posting to {need} needs a bot token; only a webhook is "
                "configured, and a webhook is fixed to its own channel. Set "
                "SLACK_BOT_TOKEN, or drop the channel/thread_ts argument."
            )
        payload: dict = {"channel": self._target(channel), "text": text}
        if thread_ts:
            payload["thread_ts"] = thread_ts
        if blocks:
            # `text` stays as the notification fallback even when blocks render.
            payload["blocks"] = blocks
        return self._api("chat.postMessage", payload)

    def channel_id(self, name: str) -> str:
        """Resolve '#name' to a 'C…' id. Needs the channels:read scope.

        Only uploads need this — chat.postMessage takes '#name' directly.
        """
        if not name.startswith("#"):
            return name
        wanted, cursor = name.lstrip("#"), ""
        while True:
            page = self._api("conversations.list", {
                "limit": 1000, "cursor": cursor,
                "types": "public_channel,private_channel",
                # Lowercase string, not a Python bool: urlencode would render
                # True as "True", which Slack reads as absent.
                "exclude_archived": "true",
            }, form=True)
            for channel in page.get("channels", []):
                if channel.get("name") == wanted:
                    return channel["id"]
            cursor = page.get("response_metadata", {}).get("next_cursor", "")
            if not cursor:
                raise SlackError(
                    f"channel {name} not found. Private channels are invisible "
                    "to the bot until it is invited."
                )

    def upload(self, path: str | Path, title: str | None = None,
               comment: str | None = None, channel: str | None = None) -> dict:
        """Share a file. Needs files:write (and channels:read for a '#name').

        Three steps, because Slack retired the one-shot files.upload in 2025:
        reserve a URL, PUT the bytes at it, then tell Slack to publish it.
        """
        path = Path(path)
        if not path.is_file():
            raise SlackError(f"no such file: {path}")
        raw = path.read_bytes()
        if not raw:
            raise SlackError(f"{path} is empty — Slack rejects zero-length uploads")

        reserved = self._api("files.getUploadURLExternal",
                             {"filename": path.name, "length": len(raw)}, form=True)

        request = urllib.request.Request(
            reserved["upload_url"], data=raw,
            headers={"Content-Type": mimetypes.guess_type(path.name)[0]
                     or "application/octet-stream"},
        )
        try:
            with urllib.request.urlopen(request, timeout=self._timeout) as resp:
                resp.read()
        except urllib.error.URLError as exc:
            raise SlackError(f"upload of {path.name} failed: {exc}") from exc

        payload: dict = {
            "files": [{"id": reserved["file_id"], "title": title or path.name}],
            "channel_id": self.channel_id(self._target(channel)),
        }
        if comment:
            payload["initial_comment"] = comment
        return self._api("files.completeUploadExternal", payload)


def notify(title: str, msg: str, channel: str | None = None) -> bool:
    """Post '*title* — msg'. Returns success; never raises.

    Use this anywhere a failed post must not become a failed run. Prefer the
    Slack methods themselves wherever an error is worth seeing.
    """
    try:
        Slack().post(f"*{title}* — {msg}", channel=channel)
        return True
    except Exception as exc:  # noqa: BLE001 — deliberate; see module docstring
        print(f"  slack: not sent ({exc})", file=sys.stderr)
        return False


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("text", nargs="*", help="message to post")
    parser.add_argument("--channel", help="overrides SLACK_CHANNEL")
    parser.add_argument("--check", action="store_true",
                        help="verify the token and exit without posting")
    parser.add_argument("--file", help="upload this file instead of posting text")
    parser.add_argument("--title", help="title for --file")
    args = parser.parse_args()

    try:
        slack = Slack(channel=args.channel)
    except SlackError as exc:
        print(exc, file=sys.stderr)
        return 1

    try:
        if args.check:
            if slack.webhook:
                # A webhook has no introspection endpoint: the only way to
                # learn whether it works is to post through it. Say so rather
                # than implying a check happened.
                tail = slack.webhook.rsplit("/", 1)[-1][:6]
                print(f"webhook configured (…{tail}) — posts go to the channel "
                      "it was created for")
                print("  not verifiable without posting: send a real message to prove it")
            if slack._token:
                who = slack.auth_test()
                print(f"bot token ok: {who.get('user')} in {who.get('team')}")
                print(f"  channel: {slack.channel or '(SLACK_CHANNEL unset)'}")
                print("  posting is NOT proven until the bot is invited to it")
            return 0

        if args.file:
            result = slack.upload(args.file, title=args.title,
                                  comment=" ".join(args.text) or None)
            print(f"uploaded {args.file} -> {result.get('files', [{}])[0].get('id', '')}")
            return 0

        if not args.text:
            parser.error("nothing to post — give a message, --file, or --check")
        result = slack.post(" ".join(args.text))
        if result.get("via") == "webhook":
            # A webhook echoes only "ok" — there is no channel or ts to report.
            print("posted via webhook")
        else:
            print(f"posted to {result.get('channel')} at {result.get('ts')}")
        return 0
    except SlackError as exc:
        print(f"FAILED: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
