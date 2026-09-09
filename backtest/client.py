"""Thin HTTP client for the Alpaca REST APIs.

This is the *only* place in the package that talks to the network, which is what
keeps the "data comes solely from Alpaca" guarantee easy to audit. It handles
authentication, cursor pagination, retry/backoff, and the SIP-to-IEX fallback for
accounts without a paid market-data subscription.
"""

from __future__ import annotations

import logging
import random
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Iterator

import httpx

from backtest.config import Settings, get_settings

log = logging.getLogger(__name__)

#: HTTP statuses worth retrying: rate limit + transient server errors.
RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})

#: Free and basic Alpaca plans serve full SIP history but embargo the most recent
#: 15 minutes. Asking for a window that runs into the embargo 403s outright rather
#: than truncating, so we truncate the request ourselves — with a minute of slack.
SIP_EMBARGO = timedelta(minutes=16)
_SIP_EMBARGO_MARKER = "recent sip data"


class AlpacaError(RuntimeError):
    """A non-retryable error returned by Alpaca."""

    def __init__(self, status_code: int, message: str, url: str) -> None:
        super().__init__(f"Alpaca {status_code} for {url}: {message}")
        self.status_code = status_code
        self.message = message
        self.url = url


class AlpacaClient:
    """Authenticated, retrying, paginating client.

    Usable as a context manager; otherwise the underlying connection pool is
    closed when the object is garbage collected.
    """

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self._http = httpx.Client(
            headers=self.settings.auth_headers,
            timeout=self.settings.timeout,
            follow_redirects=True,
        )
        #: Set once SIP is rejected outright (no entitlement), so we stop retrying it.
        self._sip_denied = False
        #: Set once we have warned about truncating a request to the SIP embargo.
        self._warned_embargo = False

    def __enter__(self) -> "AlpacaClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        self._http.close()

    # -- core request ---------------------------------------------------------

    def _request(self, url: str, params: dict[str, Any]) -> Any:
        clean = {k: v for k, v in params.items() if v is not None}
        last_error: Exception | None = None

        for attempt in range(self.settings.max_retries + 1):
            try:
                response = self._http.get(url, params=clean)
            except httpx.TransportError as exc:  # DNS, connect, read timeouts
                last_error = exc
                self._sleep_backoff(attempt)
                continue

            if response.status_code == 200:
                return response.json()

            if response.status_code in RETRY_STATUSES and attempt < self.settings.max_retries:
                retry_after = response.headers.get("retry-after")
                self._sleep_backoff(attempt, float(retry_after) if retry_after else None)
                continue

            raise AlpacaError(response.status_code, response.text[:500], str(response.url))

        raise AlpacaError(
            599, f"exhausted retries ({last_error})", url
        ) from last_error

    @staticmethod
    def _sleep_backoff(attempt: int, retry_after: float | None = None) -> None:
        delay = retry_after if retry_after is not None else min(2**attempt, 30)
        # Jitter so parallel jobs don't retry in lockstep.
        time.sleep(delay * (0.75 + 0.5 * random.random()))

    # -- pagination -----------------------------------------------------------

    def paginate(self, path: str, params: dict[str, Any]) -> Iterator[dict[str, Any]]:
        """Yield every page of a cursor-paginated data endpoint.

        Alpaca returns ``next_page_token``; a ``None`` token ends the walk.
        """
        url = f"{self.settings.data_base_url}{path}"
        page_params = dict(params)
        pages = 0
        while True:
            payload = self._request(url, page_params)
            pages += 1
            yield payload
            token = payload.get("next_page_token")
            if not token:
                log.debug("%s complete after %d page(s)", path, pages)
                return
            page_params["page_token"] = token

    # -- market data ----------------------------------------------------------

    def stock_bars(
        self,
        symbols: list[str],
        timeframe: str,
        start: str,
        end: str,
        *,
        adjustment: str = "all",
        feed: str | None = None,
        limit: int = 10_000,
        currency: str | None = None,
    ) -> dict[str, list[dict[str, Any]]]:
        """Fetch OHLCV bars for one or more symbols.

        Returns ``{symbol: [bar, ...]}`` with pages already stitched together.
        Falls back from ``sip`` to ``iex`` once, if the account lacks SIP access.
        """
        feed = feed or self.settings.stock_feed
        if feed == "sip" and self._sip_denied:
            feed = "iex"

        params = {
            "symbols": ",".join(symbols),
            "timeframe": timeframe,
            "start": start,
            "end": end,
            "adjustment": adjustment,
            "feed": feed,
            "limit": limit,
            "currency": currency,
            "sort": "asc",
        }

        merged: dict[str, list[dict[str, Any]]] = {s: [] for s in symbols}
        try:
            for page in self.paginate("/v2/stocks/bars", params):
                for symbol, bars in (page.get("bars") or {}).items():
                    merged.setdefault(symbol, []).extend(bars)
        except AlpacaError as exc:
            if feed != "sip" or exc.status_code not in (401, 403):
                raise

            retry = dict(
                symbols=symbols,
                timeframe=timeframe,
                start=start,
                adjustment=adjustment,
                limit=limit,
                currency=currency,
            )
            if _SIP_EMBARGO_MARKER in exc.message.lower():
                # Entitled to SIP history, just not to the last few minutes of it.
                capped = _cap_to_sip_embargo(end, start)
                if capped is None:
                    log.warning(
                        "The whole requested window falls inside Alpaca's 15-minute "
                        "SIP embargo; returning no bars. Use feed='iex' for real-time."
                    )
                    return merged
                if not self._warned_embargo:
                    log.info(
                        "Truncating the SIP request to %s — this plan cannot query the "
                        "last 15 minutes of consolidated data.",
                        capped,
                    )
                    self._warned_embargo = True
                return self.stock_bars(**retry, end=capped, feed="sip")

            log.warning(
                "SIP feed denied for these credentials; falling back to IEX. "
                "IEX covers only IEX-executed volume, so prices and volume "
                "filters will differ from the consolidated tape."
            )
            self._sip_denied = True
            return self.stock_bars(**retry, end=end, feed="iex")
        return merged

    def crypto_bars(
        self,
        symbols: list[str],
        timeframe: str,
        start: str,
        end: str,
        *,
        loc: str = "us",
        limit: int = 10_000,
    ) -> dict[str, list[dict[str, Any]]]:
        """Fetch crypto OHLCV bars. Symbols look like ``BTC/USD``."""
        params = {
            "symbols": ",".join(symbols),
            "timeframe": timeframe,
            "start": start,
            "end": end,
            "limit": limit,
            "sort": "asc",
        }
        merged: dict[str, list[dict[str, Any]]] = {s: [] for s in symbols}
        for page in self.paginate(f"/v1beta3/crypto/{loc}/bars", params):
            for symbol, bars in (page.get("bars") or {}).items():
                merged.setdefault(symbol, []).extend(bars)
        return merged

    # -- trading API (reference data only; this package never places orders) ---

    def calendar(self, start: str, end: str) -> list[dict[str, Any]]:
        """Official NYSE session calendar, including half days."""
        url = f"{self.settings.trading_base_url}/v2/calendar"
        payload = self._request(url, {"start": start, "end": end})
        # This endpoint returns a bare JSON array.
        return payload if isinstance(payload, list) else []

    def assets(
        self,
        *,
        status: str = "active",
        asset_class: str = "us_equity",
        exchange: str | None = None,
    ) -> list[dict[str, Any]]:
        """Tradable asset master — useful for filtering a universe."""
        url = f"{self.settings.trading_base_url}/v2/assets"
        payload = self._request(
            url, {"status": status, "asset_class": asset_class, "exchange": exchange}
        )
        return payload if isinstance(payload, list) else []


def _cap_to_sip_embargo(end: str, start: str = "") -> str | None:
    """Pull ``end`` back to the edge of the SIP embargo.

    A date-only ``end`` covers that whole day for Alpaca, so it is read as the
    last instant of the day — otherwise "today" looks safely in the past here
    while still tripping the embargo server-side.

    Returns ``None`` when the truncated window would start after it ends, i.e.
    the caller asked only for data inside the embargo.
    """
    cutoff = datetime.now(timezone.utc) - SIP_EMBARGO
    requested = _parse_bound(end, end_of_day=True)
    if requested is not None and requested <= cutoff:
        return None  # not an embargo problem after all; nothing more to try
    begins = _parse_bound(start, end_of_day=False)
    if begins is not None and begins >= cutoff:
        return None
    return cutoff.isoformat()


def _parse_bound(value: str, *, end_of_day: bool) -> datetime | None:
    """Parse an Alpaca date or RFC-3339 bound into an aware UTC datetime."""
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    is_date_only = len(value) == 10 and value.count("-") == 2
    if is_date_only and end_of_day:
        parsed += timedelta(days=1) - timedelta(microseconds=1)
    return parsed
