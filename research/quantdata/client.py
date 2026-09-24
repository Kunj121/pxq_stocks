"""Quant Data client — speaks MCP over HTTPS as plain JSON-RPC.

The hosted server at api.quantdata.us/mcp is *stateless*: there is no session
handshake and no `Mcp-Session-Id` to carry, so `tools/call` works from a bare
POST. That is what makes this importable from a script or a notebook instead of
only from Claude's MCP tool layer.

The documented REST surface (`/v1/options/tool/<name>`) 404s as of 2026-09-22, so
this is the only programmatic path to the feed.

Credentials resolve the way `backtest/config.py` resolves Alpaca's: the
environment first, then the file the MCP server itself reads.
"""
from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

URL = "https://api.quantdata.us/mcp"

#: The server allows 240 requests a minute with a burst of 20 a second. A
#: paginated pull of several streams crosses that easily, and the refusal comes
#: back as HTTP 200 with a 429 quoted in the body — so it must be recognised by
#: text, not status. Retrying is the right response: the limit is a pace, not a
#: rejection.
RATE_LIMIT_RETRIES = 5
_RETRY_AFTER = re.compile(r'"retryAfterSeconds"\s*:\s*([0-9.]+)')
PROJECT = "/Users/kunjshah/Downloads/pxq_stocks"
CLAUDE_CONFIG = Path("~/.claude.json").expanduser()


class QuantDataError(RuntimeError):
    """The server accepted the request and refused it, or the transport failed."""


def resolve_key() -> str:
    """QUANTDATA_API_KEY, else the bearer token in the MCP server registration."""
    key = os.environ.get("QUANTDATA_API_KEY")
    if key:
        return key
    try:
        cfg = json.loads(CLAUDE_CONFIG.read_text())
        header = cfg["projects"][PROJECT]["mcpServers"]["quantdata"]["headers"]["Authorization"]
        return header.split()[1]
    except (OSError, KeyError, IndexError) as exc:
        raise QuantDataError(
            "No Quant Data key. Set QUANTDATA_API_KEY or register the `quantdata` "
            f"MCP server for {PROJECT}."
        ) from exc


class QuantData:
    """Thin JSON-RPC wrapper. One method per MCP verb, plus `call` for tools."""

    def __init__(self, key: str | None = None, timeout: int = 60) -> None:
        self._key = key or resolve_key()
        self._timeout = timeout
        self._id = 0

    def _rpc(self, method: str, params: dict | None = None) -> dict:
        self._id += 1
        body = json.dumps(
            {"jsonrpc": "2.0", "id": self._id, "method": method, "params": params or {}}
        ).encode()
        req = urllib.request.Request(
            URL,
            data=body,
            headers={
                "Authorization": f"Bearer {self._key}",
                "Content-Type": "application/json",
                # The server content-negotiates; it answers JSON but advertises SSE.
                "Accept": "application/json, text/event-stream",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                payload = json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            raise QuantDataError(f"HTTP {exc.code}: {exc.read()[:400]!r}") from exc
        if "error" in payload:
            raise QuantDataError(payload["error"])
        return payload["result"]

    # -- verbs ----------------------------------------------------------------
    def ping(self) -> str:
        return self.call("qd_ping")

    def tools(self) -> list[dict]:
        return self._rpc("tools/list")["tools"]

    def call(self, tool: str, **arguments: Any) -> str:
        """Raw tool output, retrying while the server is only asking us to slow down.

        Input-validation failures come back as a 200 with the complaint in the
        text body, not as a JSON-RPC error, so they are raised here instead of
        being returned as data that looks like a result.
        """
        for attempt in range(RATE_LIMIT_RETRIES):
            text = self._text(tool, arguments)
            # Two distinct refusals both arrive as HTTP 200 with the complaint in
            # the text body: the MCP layer's own schema check, and a /v1 error
            # relayed verbatim. Returning either as data yields a "DataFrame" of
            # one error string, which fails much later and misleadingly.
            head = text[:400]
            if "input validation failed" not in head and "failed with status" not in head:
                return text
            if "status 429" not in head:
                raise QuantDataError(f"{tool}: {text[:400]}")
            if attempt == RATE_LIMIT_RETRIES - 1:
                # Say it is pacing, not a malformed request. The raw body buries
                # that under a wall of JSON, and the two want different fixes.
                raise QuantDataError(
                    f"{tool}: rate limited after {RATE_LIMIT_RETRIES} attempts — "
                    "the server allows 240 requests a minute, 20 a second. "
                    "Pull fewer streams at once, or raise a premium floor.")
            found = _RETRY_AFTER.search(head)
            # Back off a little harder each time. The server's own
            # retryAfterSeconds is usually 1, which is too optimistic when
            # several paginated pulls are racing each other.
            time.sleep(float(found.group(1)) if found else 1.0 + attempt)
        raise QuantDataError(f"{tool}: exhausted retries")  # unreachable

    def _text(self, tool: str, arguments: dict) -> str:
        """One tools/call, decoded to text. Most tools double-encode."""
        result = self._rpc("tools/call", {"name": tool, "arguments": arguments})
        text = result["content"][0]["text"]
        try:
            decoded = json.loads(text)
            text = decoded if isinstance(decoded, str) else json.dumps(decoded)
        except json.JSONDecodeError:
            pass
        return text

    # -- shaping --------------------------------------------------------------
    def frame(self, tool: str, **arguments: Any):
        """`call`, parsed into a DataFrame plus the auxiliary header dict.

        Tool output is CSV, optionally preceded by `key=value` lines and a blank
        line — that block carries `nextSearchAfter` (the pagination cursor) and
        the opt-in `stats.*` aggregate. All-empty columns are dropped: the record
        is wide and most tools populate only a slice of it, so keeping them makes
        every frame look the same and hides which fields a tool actually serves.
        """
        import io

        import pandas as pd

        text = self.call(tool, **arguments)
        meta: dict[str, str] = {}
        if "\n\n" in text:
            head, _, text = text.partition("\n\n")
            for line in head.splitlines():
                key, _, value = line.partition("=")
                if value:
                    meta[key.strip()] = value.strip()
        frame = pd.read_csv(io.StringIO(text))
        frame = frame.dropna(axis="columns", how="all")
        if "tradeTime" in frame:
            frame.insert(0, "ts", _ny(frame["tradeTime"]))
        elif "timestamp" in frame:
            frame.insert(0, "ts", _ny(frame["timestamp"]))
        return frame, meta



    def pages(self, tool: str, page_size: int = 100, max_pages: int = 200, **arguments):
        """Follow `nextSearchAfter` to the end and return one concatenated frame.

        The consolidated feed caps `size` at 100, so any real query is multi-page.
        Ordering must be stable for the cursor to mean anything; the server's
        default TRADE_TIME sort is, so a caller overriding `sortField` should
        keep it unique enough not to straddle pages.
        """
        import pandas as pd

        collected, cursor = [], None
        for _ in range(max_pages):
            args = dict(arguments, size=page_size)
            if cursor:
                args["searchAfter"] = cursor
            page, meta = self.frame(tool, **args)
            if len(page):
                collected.append(page)
            cursor = meta.get("nextSearchAfter")
            if not cursor or len(page) < page_size:
                break
            cursor = cursor.split(",")
        return (pd.concat(collected, ignore_index=True) if collected
                else pd.DataFrame())


def _ny(epoch_millis):
    """Epoch milliseconds -> tz-aware America/New_York, the repo's index convention."""
    import pandas as pd

    return pd.to_datetime(epoch_millis, unit="ms", utc=True).dt.tz_convert("America/New_York")

