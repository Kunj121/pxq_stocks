"""Earnings report dates — vendor-sourced, replacing the hand-typed EARNINGS dict.

Source: Robinhood MCP `get_earnings_calendar`, which returns `{date, timing, verified}`
per report — `timing` is already the `am`/`pm` the log encodes, and `verified`
distinguishes a confirmed date from a projected one.

**Why this is not a plain API call.** The MCP tool is callable from Claude's tool
layer, not from Python, and it answers market-wide (~6,000 records per 31-day
window) rather than per-ticker. So the pull is a two-step: Claude calls the tool
once per 31-day window, each response lands in the session's tool-results
directory, and `ingest()` folds those dumps into `data/earnings_dates.parquet`.
The parquet is the durable artefact; the dumps are session-scoped scratch.

To refresh, ask Claude to re-pull the windows and re-run `ingest()`. This is the
one calendar in the repo that cannot regenerate itself unattended — Quant Data
serves no earnings dates, and the Massive endpoints that do
(`/benzinga/v1/earnings`, `/tmx/v1/corporate-events`) are not entitled on the
current plan.

The dict this replaces had one failure mode above all: a symbol absent from it
passed the veto in silence. Here absence is an exception — see `require()`.
"""
from __future__ import annotations

import glob
import json
from pathlib import Path

import pandas as pd

HERE = Path(__file__).resolve().parent
STORE = HERE.parents[1] / "data" / "earnings_dates.parquet"

# Same convention build_log.py has always used: a before-open report is priced
# into the 09:30 open, an after-close one into the 16:00 close.
OPEN_HOUR, CLOSE_HOUR = 9.5, 16.0


class NoCoverage(LookupError):
    """A symbol was scored against a calendar that has never heard of it."""


def ingest(dump_dir: str | Path, symbols: set[str] | None = None) -> pd.DataFrame:
    """Fold Robinhood calendar dumps into the parquet store.

    `dump_dir` is the session tool-results directory. Windows are pulled with
    overlap so nothing falls between them, so duplicates are expected and one
    report per (symbol, date) is kept.
    """
    files = sorted(glob.glob(f"{dump_dir}/*get_earnings_calendar-*.txt"))
    if not files:
        raise FileNotFoundError(f"no get_earnings_calendar dumps under {dump_dir}")

    rows = []
    for path in files:
        for rec in json.load(open(path))["data"]["results"]:
            if symbols and rec["symbol"] not in symbols:
                continue
            report = rec["report"]
            rows.append({
                "symbol": rec["symbol"],
                "date": report["date"],
                "timing": report.get("timing"),
                "verified": bool(report.get("verified")),
                "fiscal_year": rec["year"],
                "quarter": rec["quarter"],
                "eps_estimate": rec["eps"].get("estimate"),
                "eps_actual": rec["eps"].get("actual"),
            })

    frame = (pd.DataFrame(rows)
             .drop_duplicates(subset=["symbol", "date"])
             .sort_values(["symbol", "date"])
             .reset_index(drop=True))
    frame["date"] = pd.to_datetime(frame["date"])
    # A report with no timing cannot be placed against an intraday print. Default
    # it to `pm`, the conservative side: it widens the veto window rather than
    # letting a sweep slip under an event whose hour is unknown.
    frame["timing"] = frame["timing"].fillna("pm")
    frame["reported_at"] = frame["date"] + pd.to_timedelta(
        frame["timing"].map({"am": OPEN_HOUR, "pm": CLOSE_HOUR}), unit="h")
    frame.to_parquet(STORE, index=False)
    return frame


def frame() -> pd.DataFrame:
    if not STORE.exists():
        raise FileNotFoundError(f"{STORE} missing — run ingest() first")
    return pd.read_parquet(STORE)


def _moments(cal: pd.DataFrame) -> dict[str, list[pd.Timestamp]]:
    return {sym: sorted(grp["reported_at"]) for sym, grp in cal.groupby("symbol")}


def require(symbols, cal: pd.DataFrame | None = None) -> None:
    """Raise unless every symbol has calendar coverage.

    The hand-typed dict's worst property was that a missing symbol passed the
    earnings veto without a word. Call this before scoring so a gap stops the
    run instead of quietly producing a tradeable signal.
    """
    cal = frame() if cal is None else cal
    absent = sorted(set(symbols) - set(cal["symbol"]))
    if absent:
        raise NoCoverage(
            f"no earnings calendar for {absent} — re-pull the windows and re-run "
            f"ingest(); these would otherwise pass the veto silently")


def next_in_window(symbol: str, start, end, cal: pd.DataFrame | None = None) -> str | None:
    """First report strictly after `start` and at or before `end`.

    Half-open exactly as the hand-rolled veto was, so a sweep printing after the
    bell on report day is not vetoed by the report it already saw.
    """
    cal = frame() if cal is None else cal
    a, b = pd.Timestamp(start), pd.Timestamp(end)
    if a.tz is not None:
        a, b = a.tz_convert("America/New_York").tz_localize(None), b.tz_convert("America/New_York").tz_localize(None)
    hits = [m for m in _moments(cal).get(symbol, []) if a < m <= b]
    return hits[0].date().isoformat() if hits else None


if __name__ == "__main__":
    import sys

    dump_dir = sys.argv[1] if len(sys.argv) > 1 else None
    if not dump_dir:
        cal = frame()
        print(f"{len(cal)} reports, {cal.symbol.nunique()} symbols, "
              f"{cal.date.min():%Y-%m-%d} .. {cal.date.max():%Y-%m-%d}, "
              f"{int(cal.verified.sum())} verified")
    else:
        syms = set(pd.read_parquet(HERE / "sweeps.parquet")["symbol"].unique())
        cal = ingest(dump_dir, symbols=syms)
        print(f"wrote {len(cal)} reports across {cal.symbol.nunique()} symbols -> {STORE}")
        print(f"span {cal.date.min():%Y-%m-%d} .. {cal.date.max():%Y-%m-%d}  "
              f"verified {int(cal.verified.sum())}/{len(cal)}")
