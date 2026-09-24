"""Backtest the spec in strategy.md over the logged sweeps.

    python research/sweep_events/backtest_strategy.py [--capital 10000] [--per-pos 2000]

Rules (see strategy.md):
  direction  from the sweep
  filter     reject if earnings fall between entry and expiry
  contract   ATM strike, third-Friday monthly, roll if under 7 days out
  exit       hold to expiry -- no stop, no target
  sizing     whole contracts, spend <= per-position cap

Reports the spec against two references: buying the swept contract instead, and
the same contract with a +40% target, so the contract-choice and exit-choice
effects stay separable.

MODEL LIMITS: our ATM contract is priced with the SWEPT contract's implied vol,
which carries skew error (largest where the sweep was far from the money). IV is
held constant through the hold, so there is no vega P&L. Fills are at the model
price with no spread or slippage.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
sys.path[:0] = [str(HERE), str(ROOT)]

from backtest.data import load_bars   # noqa: E402
from gates import _bs, split_factor   # noqa: E402

OUT = ROOT / "out" / "sweep_events"
MIN_DAYS = 7
#: Sessions of price history required before a sweep is usable. A name listed
#: weeks earlier has no SMA200, no stable realised vol, and no pre-sweep
#: structure to read — SPCX listed 12 Jun 2026 and was swept on the 16th.
MIN_HISTORY = 120


def third_friday(y: int, m: int) -> pd.Timestamp:
    d = pd.Timestamp(y, m, 1)
    return d + pd.Timedelta(days=(4 - d.dayofweek) % 7 + 14)


def next_monthly(ts: pd.Timestamp, min_days: int = MIN_DAYS,
                 not_before: pd.Timestamp | None = None) -> pd.Timestamp:
    """First third-Friday monthly at least `min_days` out and never earlier than
    `not_before` — the sweep's own expiry. Without that second condition the rule
    could SHORTEN the trade: NBIS was swept for 17 Jul and would have been given
    19 Jun, turning +95% into -99%."""
    y, m = ts.year, ts.month
    for _ in range(14):
        tf = third_friday(y, m)
        if (tf - ts.normalize()).days >= min_days and (not_before is None or tf >= not_before):
            return tf
        m, y = (1, y + 1) if m == 12 else (m + 1, y)
    raise RuntimeError("no monthly found")


def atm_strike(spot: float) -> float:
    step = 2.5 if spot < 50 else 5 if spot < 200 else 10 if spot < 500 \
        else 20 if spot < 1000 else 25
    return round(spot / step) * step


def price_path(spot_path, strike, iv, right, expiry_ts, index):
    T = np.maximum((expiry_ts - index).total_seconds() / 86400 / 365, 1e-9)
    return np.array([_bs(S, strike, t, iv, right) for S, t in zip(spot_path, T)])


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--capital", type=float, default=10_000)
    ap.add_argument("--per-pos", type=float, default=2_000)
    ap.add_argument("--target", type=float, default=40.0,
                    help="profit target for the reference arm only")
    ap.add_argument("--stop", type=float, default=60.0,
                    help="premium stop-loss %% for the stop arm")
    a = ap.parse_args()

    sw = pd.read_parquet(HERE / "sweeps.parquet").sort_values("swept_at")
    vetoed = sw[~sw.tradeable]
    sw = sw[sw.tradeable]
    bars = load_bars(sorted(sw.symbol.unique()), "2025-09-19",
                     timeframe="1Hour", session="regular").frame
    close = bars["close"].unstack("symbol").sort_index()
    last = close.index[-1]

    rows, dropped = [], []
    for _, s in sw.iterrows():
        ser = close[s.symbol].dropna()
        tz = ser.index.tz
        t0 = pd.Timestamp(s.swept_at, tz=tz)
        iv = s.vendor_iv / 100

        # Reject names without enough trading history to be analysable at all.
        hist = (ser.index < t0).sum()
        if hist < MIN_HISTORY:
            dropped.append((s.label, f"only {hist} bars of history"))
            continue

        # Put the vendor's prices into the bars' split-adjusted terms.
        bi = max(0, min(ser.index.searchsorted(t0, side="right") - 1, len(ser) - 1))
        f, ok = split_factor(s.spot, ser.iloc[bi],
                             bar_low=bars.xs(s.symbol, level="symbol").sort_index().low.iloc[bi],
                             bar_high=bars.xs(s.symbol, level="symbol").sort_index().high.iloc[bi])
        if not ok:
            dropped.append((s.label, f"price ratio {f:.3f}"))
            continue
        spot, strike, paid = s.spot / f, s.strike / f, s.price / f

        # --- the spec's contract ---
        ed = next_monthly(pd.Timestamp(s.swept_at),
                          not_before=pd.Timestamp(s.expiry))
        exp = pd.Timestamp(ed, tz=tz) + pd.Timedelta(hours=16)
        K = atm_strike(spot)
        prem = _bs(spot, K, (exp - t0).total_seconds() / 86400 / 365, iv, s.right)
        seg = ser[(ser.index >= t0) & (ser.index <= min(exp, last))]
        if seg.empty or prem <= 0:
            continue
        pnl = (price_path(seg.values, K, iv, s.right, exp, seg.index) / prem - 1) * 100

        # --- reference: the swept contract itself ---
        sexp = pd.Timestamp(s.expiry, tz=tz) + pd.Timedelta(hours=16)
        sseg = ser[(ser.index >= t0) & (ser.index <= min(sexp, last))]
        spnl = (price_path(sseg.values, strike, iv, s.right, sexp, sseg.index)
                / paid - 1) * 100

        cost = prem * 100
        n = int(a.per_pos // cost)
        hit = np.flatnonzero(pnl >= a.target)
        # Stop arm: first breach of -stop%% exits there; otherwise hold. A long
        # option cannot lose more than premium, so the stop only binds above
        # -100%%.
        shit = np.flatnonzero(pnl <= -a.stop)
        stop_ret = -a.stop if len(shit) else pnl[-1]
        # Stop AND target together, whichever comes first along the path.
        first_t = hit[0] if len(hit) else 10**9
        first_s = shit[0] if len(shit) else 10**9
        both_ret = (a.target if first_t < first_s
                    else -a.stop if first_s < first_t else pnl[-1])
        rows.append(dict(
            month=t0.strftime("%b"), sweep=s.label, dir=s.right,
            our_strike=K, our_expiry=ed.strftime("%b %-d"), split_adj=f,
            cost=round(cost), n=n, deployed=round(n * cost),
            spec_ret=round(pnl[-1], 1), spec_pnl=round(n * cost * pnl[-1] / 100),
            target_ret=round(a.target if len(hit) else pnl[-1], 1),
            target_pnl=round(n * cost * (a.target if len(hit) else pnl[-1]) / 100),
            stop_ret=round(stop_ret, 1), stop_pnl=round(n * cost * stop_ret / 100),
            both_ret=round(both_ret, 1), both_pnl=round(n * cost * both_ret / 100),
            stopped=bool(len(shit)),
            swept_ret=round(spnl[-1], 1),
            max_ret=round(pnl.max(), 1),
            open="OPEN" if exp > last else ""))
    d = pd.DataFrame(rows)
    OUT.mkdir(parents=True, exist_ok=True)
    d.to_parquet(OUT / "strategy_backtest.parquet", index=False)

    pd.set_option("display.width", 240)
    if dropped:
        print("\ndropped before trading:")
        for lab, why in dropped:
            print(f"    {lab:<18} {why}")
    adj = d[d.split_adj != 1.0]
    if len(adj):
        print("\nsplit-adjusted:")
        for _, r in adj.iterrows():
            print(f"    {r.sweep:<18} divided by {r.split_adj:g}")
    print(f"\nvetoed on earnings: {len(vetoed)} of {len(vetoed) + len(sw)}")
    for _, r in vetoed.iterrows():
        print(f"    {r.label:<18} earnings {r.earnings_in_window}")
    print(f"\n{d.to_string(index=False)}")

    t = d[d.n > 0]
    print(f"\ntraded {len(t)} of {len(d)} tradeable signals "
          f"({len(d) - len(t)} unaffordable at ${a.per_pos:,.0f})")
    for name, col in (("SPEC  hold to expiry      ", "spec_pnl"),
                      (f"      +{a.target:.0f}% target        ", "target_pnl"),
                      (f"      -{a.stop:.0f}% stop           ", "stop_pnl"),
                      (f"      -{a.stop:.0f}% stop +{a.target:.0f}% tgt  ", "both_pnl")):
        print(f"  {name}: {t[col].sum():+9,.0f}  -> {100*t[col].sum()/a.capital:+6.1f}% "
              f"of ${a.capital:,.0f}   win {100*(t[col]>0).mean():.0f}%")
    print(f"\n  stop fired on {t.stopped.sum()} of {len(t)} traded")
    print(f"  mean return per trade — spec {t.spec_ret.mean():+.1f}%   "
          f"swept contract {t.swept_ret.mean():+.1f}%   peak {t.max_ret.mean():+.1f}%")
    print("\nby month (spec):")
    print(d.groupby("month").agg(n=("sweep", "size"), traded=("n", lambda x: (x > 0).sum()),
                                 pnl=("spec_pnl", "sum"),
                                 mean_ret=("spec_ret", "mean")).round(1).to_string())


if __name__ == "__main__":
    main()
