"""Assemble the daily panel the regime-detection paper needs.

Four series, three sources:

=====================  ==========================================  ==============
series                 source                                      why not Alpaca
=====================  ==========================================  ==============
SPY close + volume     Alpaca ``1Day``, ``adjustment="raw"``        --
USL close (WTI proxy)  Alpaca ``1Day``, ``adjustment="raw"``        --
VIX close              CBOE ``VIX_History.csv``                     no index data
DGS10 (10y UST)        FRED ``fredgraph.csv?id=DGS10``              no macro data
=====================  ==========================================  ==============

The two CSV pulls are from the series' own publishers (Cboe publishes the VIX;
the St. Louis Fed publishes DGS10), fetched over plain HTTP so the whole panel
regenerates from this one script.  See ``regime_detection.md`` for why the repo's
Alpaca-only rule does not reach them.

``adjustment="raw"``, not ``"all"``: the paper's features are distances from a
moving average and a drawdown from a running maximum, computed on an S&P 500
*price* level.  A dividend-adjusted SPY restates that level and would shift
every one of them.

Usage::

    python -m research.regime_detection.fetch_data          # writes the panel
    python -m research.regime_detection.fetch_data --force  # re-download CSVs
"""

from __future__ import annotations

import argparse
import logging
from pathlib import Path

import pandas as pd

LOG = logging.getLogger(__name__)

REPO = Path(__file__).resolve().parents[2]
RAW = Path(__file__).resolve().parent / "raw"
PANEL = REPO / "data" / "regime_panel.parquet"

# Alpaca equity history begins 2016-01-04; asking earlier just logs a warning.
START = "2016-01-04"

VIX_URL = "https://cdn.cboe.com/api/global/us_indices/daily_prices/VIX_History.csv"
DGS10_URL = "https://fred.stlouisfed.org/graph/fredgraph.csv?id=DGS10&cosd=2015-06-01"

EQUITY = "SPY"  # the S&P 500 level
OIL = "USL"  # United States 12 Month Oil Fund -- the WTI proxy Alpaca will serve


def _download(url: str, dest: Path, force: bool = False) -> Path:
    """Cache a publisher CSV under ``raw/``.  Idempotent unless ``force``."""
    if dest.exists() and not force:
        LOG.info("cached %s", dest.name)
        return dest
    import httpx

    LOG.info("downloading %s", url)
    dest.parent.mkdir(parents=True, exist_ok=True)
    with httpx.Client(follow_redirects=True, timeout=60.0) as client:
        response = client.get(url)
        response.raise_for_status()
    dest.write_bytes(response.content)
    return dest


def load_vix(force: bool = False) -> pd.Series:
    """Daily VIX close from Cboe, indexed by date."""
    path = _download(VIX_URL, RAW / "vix_history.csv", force)
    frame = pd.read_csv(path, parse_dates=["DATE"])
    return (
        frame.set_index("DATE")["CLOSE"]
        .rename("VIX")
        .sort_index()
        .pipe(lambda s: s[~s.index.duplicated(keep="last")])
    )


def load_dgs10(force: bool = False) -> pd.Series:
    """10-year Treasury constant-maturity yield from FRED, indexed by date.

    FRED writes ``.`` on market holidays; those become NaN and are forward
    filled onto the equity calendar by the caller, never back filled.
    """
    path = _download(DGS10_URL, RAW / "dgs10.csv", force)
    frame = pd.read_csv(path, parse_dates=["observation_date"])
    series = pd.to_numeric(frame["DGS10"], errors="coerce")
    series.index = frame["observation_date"]
    return series.rename("DGS10").sort_index()


def load_alpaca_close(symbol: str, start: str = START, end: str | None = None) -> pd.DataFrame:
    """Raw daily bars for one symbol, via the repo's Alpaca lane."""
    import sys

    if str(REPO) not in sys.path:
        sys.path.insert(0, str(REPO))
    from backtest.data import load_bars

    panel = load_bars(
        [symbol],
        timeframe="1Day",
        start=start,
        end=end,
        adjustment="raw",
        session="regular",
    )
    frame = panel.frame if hasattr(panel, "frame") else panel
    frame = frame.reset_index()
    frame = frame[frame["symbol"] == symbol]
    frame["date"] = pd.to_datetime(frame["timestamp"]).dt.tz_localize(None).dt.normalize()
    return frame.set_index("date")[["close", "volume"]].sort_index()


def build_panel(start: str = START, end: str | None = None, force: bool = False) -> pd.DataFrame:
    """The paper's raw inputs on one row-per-trading-day index.

    The equity calendar is authoritative: VIX and DGS10 are reindexed onto it.
    A missing VIX print would be a data error, so it is left as NaN rather than
    filled; DGS10 is forward filled because a yield is a level that persists
    across a bond-market holiday.
    """
    equity = load_alpaca_close(EQUITY, start, end)
    oil = load_alpaca_close(OIL, start, end)["close"].rename("oil_close")
    vix = load_vix(force)
    dgs10 = load_dgs10(force)

    panel = pd.DataFrame(
        {
            "close": equity["close"],
            "volume": equity["volume"],
            "oil_close": oil.reindex(equity.index),
            "VIX": vix.reindex(equity.index),
            "DGS10": dgs10.reindex(equity.index).ffill(),
        }
    )
    panel.index.name = "date"
    return panel


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start", default=START)
    parser.add_argument("--end", default=None)
    parser.add_argument("--force", action="store_true", help="re-download the publisher CSVs")
    parser.add_argument("--out", default=str(PANEL))
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    panel = build_panel(args.start, args.end, args.force)

    missing = panel.isna().sum()
    if missing.any():
        LOG.warning("missing values after join:\n%s", missing[missing > 0])

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    panel.to_parquet(out)
    print(f"wrote {len(panel):,} rows x {panel.shape[1]} cols -> {out}")
    print(f"{panel.index.min().date()} -> {panel.index.max().date()}")


if __name__ == "__main__":
    main()
