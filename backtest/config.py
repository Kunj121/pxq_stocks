"""Configuration and credential resolution for the Alpaca-backed backtester.

Credentials are never stored in this repo. They are resolved, in order, from:

1. Process environment (``ALPACA_API_KEY`` / ``ALPACA_SECRET_KEY``, or the
   ``APCA_API_KEY_ID`` / ``APCA_API_SECRET_KEY`` aliases Alpaca's own SDKs use).
2. The repo's ``.env`` (guardrails live there; credentials normally do not).
3. The env file the Alpaca MCP server already uses, by default
   ``~/.claude/mcp-servers/alpaca.env``. Override the path with ``ALPACA_ENV_FILE``.

The first source that supplies *both* a key and a secret wins.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

from dotenv import dotenv_values

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_MCP_ENV_FILE = Path.home() / ".claude" / "mcp-servers" / "alpaca.env"

DATA_BASE_URL = "https://data.alpaca.markets"
PAPER_TRADING_BASE_URL = "https://paper-api.alpaca.markets"
LIVE_TRADING_BASE_URL = "https://api.alpaca.markets"

#: Feeds the stock data API accepts. ``sip`` is full-market consolidated tape and
#: needs a paid data subscription; ``iex`` is free but only IEX's ~2% of volume.
STOCK_FEEDS = ("sip", "iex", "otc", "delayed_sip", "boats", "overnight")

_KEY_ALIASES = ("ALPACA_API_KEY", "APCA_API_KEY_ID")
_SECRET_ALIASES = ("ALPACA_SECRET_KEY", "APCA_API_SECRET_KEY")


class ConfigError(RuntimeError):
    """Raised when credentials or settings cannot be resolved."""


def _first(mapping, names: tuple[str, ...]) -> str | None:
    for name in names:
        value = mapping.get(name)
        if value:
            return str(value).strip()
    return None


def _candidate_sources() -> list[tuple[str, dict]]:
    """Credential sources, highest precedence first."""
    sources: list[tuple[str, dict]] = [("environment", dict(os.environ))]

    repo_env = REPO_ROOT / ".env"
    if repo_env.is_file():
        sources.append((str(repo_env), dotenv_values(repo_env)))

    mcp_env = Path(os.environ.get("ALPACA_ENV_FILE", DEFAULT_MCP_ENV_FILE)).expanduser()
    if mcp_env.is_file():
        sources.append((str(mcp_env), dotenv_values(mcp_env)))

    return sources


def _resolve_credentials() -> tuple[str, str, str]:
    """Return ``(api_key, secret_key, source_label)``."""
    checked = []
    for label, mapping in _candidate_sources():
        checked.append(label)
        key = _first(mapping, _KEY_ALIASES)
        secret = _first(mapping, _SECRET_ALIASES)
        if key and secret:
            return key, secret, label
    raise ConfigError(
        "No Alpaca API credentials found. Checked: "
        + ", ".join(checked)
        + ". Set ALPACA_API_KEY and ALPACA_SECRET_KEY in the environment, or point "
        "ALPACA_ENV_FILE at a file that defines them."
    )


def _resolve_option(names: tuple[str, ...], default: str) -> str:
    for _, mapping in _candidate_sources():
        value = _first(mapping, names)
        if value:
            return value
    return default


def _as_bool(value: str) -> bool:
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    """Resolved configuration for every Alpaca call the backtester makes."""

    api_key: str = field(repr=False)
    secret_key: str = field(repr=False)
    #: Where the credentials came from, for diagnostics. Never contains a secret.
    credential_source: str = "environment"
    #: Default stock feed. Falls back to ``iex`` automatically on a 403.
    stock_feed: str = "sip"
    #: Paper vs live only affects the *trading* API (calendar, clock, assets).
    #: Market data is identical either way.
    paper: bool = True
    cache_dir: Path = REPO_ROOT / "data" / "alpaca_cache"
    output_dir: Path = REPO_ROOT / "out"
    #: Seconds to wait for a single HTTP request.
    timeout: float = 30.0
    #: Retries for 429/5xx responses, with exponential backoff.
    max_retries: int = 5

    @property
    def auth_headers(self) -> dict[str, str]:
        return {
            "APCA-API-KEY-ID": self.api_key,
            "APCA-API-SECRET-KEY": self.secret_key,
            "accept": "application/json",
        }

    @property
    def trading_base_url(self) -> str:
        return PAPER_TRADING_BASE_URL if self.paper else LIVE_TRADING_BASE_URL

    @property
    def data_base_url(self) -> str:
        return DATA_BASE_URL


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Load settings once per process.

    Recognised optional keys (env, repo ``.env``, or the MCP env file):

    ``ALPACA_DATA_FEED``
        Default stock feed. Defaults to ``sip``.
    ``ALPACA_PAPER_TRADE``
        ``true``/``false``. Only selects which trading host serves the calendar.
    ``BACKTEST_CACHE_DIR`` / ``BACKTEST_OUTPUT_DIR``
        Override the on-disk cache and report directories.
    """
    api_key, secret_key, source = _resolve_credentials()

    feed = _resolve_option(("ALPACA_DATA_FEED", "ALPACA_FEED"), "sip").lower()
    if feed not in STOCK_FEEDS:
        raise ConfigError(f"ALPACA_DATA_FEED={feed!r} is not one of {STOCK_FEEDS}")

    cache_dir = Path(
        _resolve_option(("BACKTEST_CACHE_DIR",), str(REPO_ROOT / "data" / "alpaca_cache"))
    ).expanduser()
    output_dir = Path(
        _resolve_option(("BACKTEST_OUTPUT_DIR",), str(REPO_ROOT / "out"))
    ).expanduser()

    return Settings(
        api_key=api_key,
        secret_key=secret_key,
        credential_source=source,
        stock_feed=feed,
        paper=_as_bool(_resolve_option(("ALPACA_PAPER_TRADE",), "true")),
        cache_dir=cache_dir,
        output_dir=output_dir,
    )
