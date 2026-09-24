"""Replay exit rules over the dashboard's option-mark paths, in Python.

The dashboard marks each option with Black-Scholes off the vol implied by the real
entry fill, along the underlying's 15-minute closes, settling at real intrinsic.
This is the same construction, server-side, so a rule can be evaluated over the
whole trade log without hand-driving the page.

**It is checked against the page, not trusted on its own.** Replaying PUT_CONFIG's
flat +100/-56 rule here reproduces the dashboard export's total to the cent
($19,278) — see `research/sweep_events/jump_sweeps.md` §10. Re-run that comparison
after any change to `build_paths` or `simulate`:

    _, sig = exit_lab.load("data/exit_lab/PUT_CONFIG.json")
    run(build_paths(log.loc[sorted(sig.trade_index)], bars),
        Rule(tp=100, sl=56), budget=3000, zero="one").pnl.sum()
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd
from scipy.stats import norm

HERE = Path(__file__).resolve().parent
DATA = HERE.parents[1] / "data"
TZ = "America/New_York"
YEAR = 365.0
BARS_PER_SESSION = 26


def bs_price(S, K, tau, sig, right):
    if tau <= 1e-7 or sig <= 0:
        return max(0.0, S - K) if right == "CALL" else max(0.0, K - S)
    sq = sig * np.sqrt(tau)
    d1 = (np.log(S / K) + 0.5 * sig * sig * tau) / sq
    return (S * norm.cdf(d1) - K * norm.cdf(d1 - sq)) if right == "CALL" else (K * norm.cdf(sq - d1) - S * norm.cdf(-d1))


def build_paths(trades: pd.DataFrame, bars: dict) -> dict[int, dict]:
    """Per trade: the option mark at every 15-minute close from entry to expiry."""
    out = {}
    for t in trades.itertuples():
        b = bars.get(t.ticker)
        if b is None or pd.isna(t.entry_price) or pd.isna(t.entry_iv) or pd.isna(t.strike):
            continue
        ts = b["ts"].values
        i0 = int(np.searchsorted(ts, np.datetime64(t.confirm_ts)))
        end = pd.Timestamp(t.expiry) + pd.Timedelta(hours=16)
        j1 = int(np.searchsorted(ts, np.datetime64(end)))
        if j1 - i0 < 2:
            continue
        S = b["close"].values[i0:j1]
        mins = (end - pd.to_datetime(ts[i0:j1])).total_seconds() / 60.0 - 15.0
        tau = np.maximum(0.0, mins) / (60 * 24 * YEAR)
        mark = np.array([bs_price(S[k], t.strike, tau[k], t.entry_iv, t.right) for k in range(len(S))])
        gaps = np.diff(ts[i0:j1]).astype("timedelta64[m]").astype(int) > 120
        sess = np.concatenate([[0], np.cumsum(gaps)])
        settle = (max(0.0, t.exit_spot - t.strike) if t.right == "CALL" else max(0.0, t.strike - t.exit_spot)) \
            if not pd.isna(t.exit_spot) else float(mark[-1])
        out[t.Index] = {"mark": mark, "sess": sess, "settle": settle, "entry": float(t.entry_price),
                        "spot": S, "spot0": float(t.confirm_spot), "dir": 1 if t.direction == "BULL" else -1}
    return out


@dataclass
class Rule:
    """A flat target/stop, or an adaptive target chosen by an early check."""
    tp: float | None = None
    sl: float | None = None
    max_sessions: int | None = None
    # adaptive: at `decide_bars` after entry, read the mark; green -> tp_green, red -> tp_red
    decide_bars: int | None = None
    tp_green: float | None = None
    tp_red: float | None = None
    cut_red: bool = False        # red at the check -> close there, don't wait for a bounce
    name: str = ""


def simulate(p, rule: Rule) -> dict:
    """Walk the marked path bar by bar.

    With an adaptive target the branch is only known AT the check, so the target
    cannot arm before it — reading the day-1 outcome and letting the target fire
    on day 0 would be lookahead. Before the check only the stop is live.
    """
    entry, mark, sess = p["entry"], p["mark"], p["sess"]
    adaptive = rule.decide_bars is not None
    tp, branch = (None, "") if adaptive else (rule.tp, "")
    for i in range(1, len(mark)):
        r = mark[i] / entry - 1.0
        last_of_session = i == len(mark) - 1 or sess[i + 1] != sess[i]
        if adaptive and i == rule.decide_bars:
            green = mark[i] > entry
            branch = "green" if green else "red"
            if green:
                tp = rule.tp_green
            elif rule.cut_red:
                return {"ret": r, "reason": "cut", "sessions": int(sess[i]), "branch": branch}
            else:
                tp = rule.tp_red
        # Exit at the bar's own mark, not at the threshold: a 15-minute bar can gap
        # straight past the target, and the page books what the contract was worth
        # when the breach was observed.
        if rule.sl is not None and r <= -rule.sl / 100:
            return {"ret": r, "reason": "sl", "sessions": int(sess[i]), "branch": branch}
        if tp is not None and r >= tp / 100:
            return {"ret": r, "reason": "tp", "sessions": int(sess[i]), "branch": branch}
        if rule.max_sessions is not None and sess[i] >= rule.max_sessions and last_of_session:
            return {"ret": r, "reason": "max_hold", "sessions": int(sess[i]), "branch": branch}
    # path ended before the check ever happened
    if adaptive and not branch:
        branch = "short"
    return {"ret": p["settle"] / entry - 1.0, "reason": "expiry", "sessions": int(sess[-1]), "branch": branch}


def run(paths: dict, rule: Rule, budget: float = 3000.0, zero: str = "one") -> pd.DataFrame:
    rows = []
    for idx, p in paths.items():
        n = int(budget // (p["entry"] * 100))
        if n == 0:
            if zero != "one":
                continue
            n = 1
        r = simulate(p, rule)
        rows.append({"idx": idx, "contracts": n, "entry": p["entry"], **r,
                     "pnl": (p["entry"] * (1 + r["ret"]) - p["entry"]) * 100 * n})
    return pd.DataFrame(rows)


def summary(df: pd.DataFrame, label: str) -> dict:
    if not len(df):
        return {"rule": label, "n": 0}
    gp = df.pnl[df.pnl > 0].sum(); gl = -df.pnl[df.pnl < 0].sum()
    return {"rule": label, "n": len(df), "win": (df.pnl > 0).mean(), "mean": df.ret.mean(),
            "median": df.ret.median(), "total": df.pnl.sum(), "pf": gp / gl if gl else np.inf,
            "hold_d": df.sessions.mean(), "tp": (df.reason == "tp").mean()}
