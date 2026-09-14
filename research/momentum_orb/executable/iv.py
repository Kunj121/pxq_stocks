"""At-the-money implied volatility for the ORB universe.

Why this exists
---------------
The ORB's stop is 10% of ATR(14) — a TRAILING measure. Implied volatility is the
options market's FORWARD estimate, and the two disagree in a way that matters:
when an event is scheduled (earnings tonight, an FDA date), IV jumps immediately
while ATR is still averaging fourteen quiet sessions. A stop sized off stale ATR
is then too tight on exactly the days with the largest moves.

Measured across the live universe on 2026-09-14, the two rank the SAME
(Spearman 0.990), so IV is useless for picking the universe. What varies is the
RATIO — IV-implied move over ATR spread from 0.61 to 0.95 across 30 names. That
ratio is the signal; the level is not.

Units
-----
    IV-implied 1-day move ($) = price * IV / sqrt(252)

That is a 1-sigma close-to-close move, whereas ATR is a high-to-low true range.
For roughly normal returns the range runs ~1.3x the close-to-close move, which is
why the observed median ratio is 0.76 ~ 1/1.32. Comparing them raw would confuse
"different information" with "different units", so callers that size stops from
IV should rescale — see `iv_stop_scale`.
"""

from __future__ import annotations

import logging
import math
import sys
from datetime import date
from pathlib import Path

import httpx
import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
sys.path.insert(0, str(ROOT))

from backtest.config import get_settings

LOG_DIR = ROOT / "execution" / "logs"
IV_HISTORY = LOG_DIR / "orb_iv_history.csv"
DATA_BASE = "https://data.alpaca.markets"
TRADING_DAYS = 252

log = logging.getLogger("orb.iv")


def atm_iv(symbols: list[str], feed: str = "indicative",
           timeout: float = 30.0) -> pd.DataFrame:
    """ATM implied volatility per underlying, from the nearest-to-0.50-delta call.

    Delta is used to locate the money rather than strike-minus-spot because it is
    already normalised and comes back in the same payload, so no second request
    and no spot-price round-trip is needed.
    """
    st = get_settings()
    rows = []
    with httpx.Client(base_url=DATA_BASE, headers=st.auth_headers,
                      timeout=timeout) as c:
        for s in symbols:
            try:
                r = c.get(f"/v1beta1/options/snapshots/{s}",
                          params={"feed": feed, "limit": 250})
                if r.status_code != 200:
                    rows.append({"symbol": s, "iv": np.nan, "contract": "",
                                 "delta": np.nan, "note": f"http {r.status_code}"})
                    continue
                snaps = r.json().get("snapshots", {})
                best = None
                for sym, d in snaps.items():
                    iv = d.get("impliedVolatility")
                    g = d.get("greeks") or {}
                    dl = g.get("delta")
                    if iv is None or dl is None or len(sym) < 10 or sym[-9] != "C":
                        continue
                    score = abs(abs(dl) - 0.5)
                    if best is None or score < best[0]:
                        best = (score, iv, sym, dl)
                if best is None:
                    rows.append({"symbol": s, "iv": np.nan, "contract": "",
                                 "delta": np.nan, "note": "no readable ATM call"})
                else:
                    rows.append({"symbol": s, "iv": float(best[1]),
                                 "contract": best[2], "delta": float(best[3]),
                                 "note": ""})
            except Exception as exc:
                rows.append({"symbol": s, "iv": np.nan, "contract": "",
                             "delta": np.nan, "note": f"{type(exc).__name__}"})
    return pd.DataFrame(rows).set_index("symbol")


def implied_daily_move(price: pd.Series, iv: pd.Series) -> pd.Series:
    """1-sigma close-to-close move in dollars."""
    return price * iv / math.sqrt(TRADING_DAYS)


def iv_stop_scale(implied_move: pd.Series, atr: pd.Series) -> float:
    """Factor putting IV-implied moves on the same footing as ATR.

    Without this, switching the stop to IV would silently also make every stop
    ~24% tighter, and we would be testing "tighter stops" — a question already
    answered (tighter stops lose more to slippage) — instead of "does forward
    -looking volatility information make a better stop?". Rescaling to a common
    median holds average stop width fixed so only the cross-sectional and
    time-series disagreement between IV and ATR is under test.
    """
    j = pd.concat([implied_move.rename("m"), atr.rename("a")], axis=1).dropna()
    j = j[(j.m > 0) & (j.a > 0)]
    if j.empty:
        return 1.0
    return float((j.a / j.m).median())


def log_iv(session: date, frame: pd.DataFrame) -> Path:
    """Append one row per symbol per session. Idempotent on (date, symbol)."""
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    out = frame.reset_index()
    out.insert(0, "date", session)
    if IV_HISTORY.exists():
        old = pd.read_csv(IV_HISTORY)
        key = set(zip(old["date"].astype(str), old["symbol"]))
        out = out[[(str(d), s) not in key
                   for d, s in zip(out["date"], out["symbol"])]]
    if len(out):
        out.to_csv(IV_HISTORY, mode="a", header=not IV_HISTORY.exists(), index=False)
    return IV_HISTORY


def load_iv_history() -> pd.DataFrame:
    if not IV_HISTORY.exists():
        return pd.DataFrame(columns=["date", "symbol", "iv"])
    h = pd.read_csv(IV_HISTORY)
    h["date"] = pd.to_datetime(h["date"]).dt.date
    return h


def main() -> None:
    import argparse
    ap = argparse.ArgumentParser(description="Snapshot ATM IV for the universe.")
    ap.add_argument("--universe", default=None)
    ap.add_argument("--date", default=None)
    a = ap.parse_args()
    sys.path.insert(0, str(HERE))
    from daily_orb import _universe

    sess = pd.Timestamp(a.date).date() if a.date else \
        pd.Timestamp.now(tz="America/New_York").date()
    syms = _universe(a.universe)
    frame = atm_iv(syms)
    path = log_iv(sess, frame)
    ok = int(frame["iv"].notna().sum())
    print(f"{sess}: ATM IV for {ok}/{len(syms)} names -> {path}")
    if ok:
        d = frame.dropna(subset=["iv"]).sort_values("iv", ascending=False)
        print(f"  highest: {', '.join(f'{s} {v:.0%}' for s, v in d['iv'].head(5).items())}")
        print(f"  lowest : {', '.join(f'{s} {v:.0%}' for s, v in d['iv'].tail(3).items())}")
    miss = frame[frame["iv"].isna()]
    if len(miss):
        print(f"  no IV: {' '.join(miss.index)}")


if __name__ == "__main__":
    main()
