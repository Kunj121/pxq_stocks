"""Exit policies on a common footing — one walker, one grid, costs, and an oracle.

`exit_rules.simulate` bakes its decision grid into `for i in range(1, len(mark))` and
charges nothing to trade, which makes two comparisons impossible: a policy that acts
daily against one that acts every fifteen minutes, and an early exit against holding
to settlement. Both matter here — the second is a systematic ~2pp gift to every
early-exit rule, on effects that are themselves ~5pp.

So every exit becomes a `Policy` passed to `evaluate()`, and the grid and the cost are
arguments rather than assumptions. `evaluate(grid="fine", cost_frac=0)` reproduces
`exit_rules.simulate` trade for trade — `check_reconciles()` asserts it, which keeps
the dashboard reconciliation in `jump_sweeps.md` §10 intact.

**The split is by expiry, not by entry.** Splitting on entry date leaves training
trades holding later expiries whose bars run through the whole test window — 13 of
them did in the original 4-cycle sample. Cycles and the split point are derived from
the data, so a back-filled log widens the validation instead of silently reusing a
hardcoded boundary.

    python research/sweep_events/stopping.py          # reconciliation + Gate 0
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import event_study as E  # noqa: E402
import exit_rules as X  # noqa: E402

DATA = HERE.parents[1] / "data"
TRADES = DATA / "dashboard_trades.parquet"
TRAIN_FRAC = 2 / 3          # earliest cycles train, the rest test
YEAR = 365.0
SEED = 11


# ------------------------------------------------------------------ loading
def load(bars: dict | None = None):
    """(trades, bars, paths) for every simulable trade."""
    bars = E.load_bars() if bars is None else bars
    trades = pd.read_parquet(TRADES)
    trades = trades[trades.simulable]
    return trades, bars, X.build_paths(trades, bars)


def cycles(trades: pd.DataFrame, paths: dict) -> list[str]:
    """Expiry cycles present, oldest first — derived, never hardcoded."""
    exp = pd.to_datetime(trades.loc[list(paths), "expiry"])
    return [str(c.date()) for c in sorted(exp.unique())]


def split_expiry(trades: pd.DataFrame, paths: dict, train_frac: float = TRAIN_FRAC) -> pd.Timestamp:
    """The cycle the train/test boundary sits on."""
    cs = cycles(trades, paths)
    k = max(1, min(len(cs) - 1, round(len(cs) * train_frac)))
    return pd.Timestamp(cs[k - 1])


def split_index(trades: pd.DataFrame, paths: dict,
                split: pd.Timestamp | None = None) -> tuple[list[int], list[int]]:
    """Train/test by EXPIRY cycle, so no training path's bars reach into the test window.

    Splitting on entry date instead would leave training trades holding later expiries
    whose bars run through the whole test window — 13 of them did, in the 4-cycle sample.
    """
    split = split_expiry(trades, paths) if split is None else split
    exp = pd.to_datetime(trades.loc[list(paths), "expiry"])
    train = sorted(exp.index[exp <= split])
    test = sorted(exp.index[exp > split])
    assert exp.loc[train].max() <= split, "a training path expires after the split"
    return train, test


# ------------------------------------------------------------------- state
def path_tau(p: dict, trade, bars: dict) -> np.ndarray:
    """Years to expiry at each bar, rebuilt on `build_paths`' own convention.

    The -15 minutes treats a bar's stamp as its open, so time is measured to the
    bar's close. The length assertion is what stops this drifting from `build_paths`.
    """
    b = bars[trade.ticker]
    ts = b["ts"].values
    i0 = int(np.searchsorted(ts, np.datetime64(trade.confirm_ts)))
    end = pd.Timestamp(trade.expiry) + pd.Timedelta(hours=16)
    j1 = int(np.searchsorted(ts, np.datetime64(end)))
    mins = (end - pd.to_datetime(ts[i0:j1])).total_seconds() / 60.0 - 15.0
    tau = np.maximum(0.0, mins) / (60 * 24 * YEAR)
    assert len(tau) == len(p["mark"]), f"tau/mark length mismatch for {trade.ticker}"
    return tau


def decision_idx(p: dict, grid: str = "daily") -> np.ndarray:
    """Bar positions where an exit may be taken. Never 0 — entry cannot trigger."""
    n = len(p["mark"])
    if grid == "fine":
        return np.arange(1, n)
    sess = p["sess"]
    last = np.flatnonzero(np.r_[sess[1:] != sess[:-1], True])          # last bar of each session
    if grid == "daily":
        keep = last
    elif grid == "open":
        keep = np.r_[0, last[:-1] + 1]
    else:
        raise ValueError(f"unknown grid {grid!r}")
    return keep[keep > 0]


def state_matrix(p: dict, trade, tau: np.ndarray, idx: np.ndarray):
    """State at each decision bar.

    Deliberately collapsed. `log(S0/K)` has sd 0.013 across this book — the contracts
    are at the money by construction — so log-moneyness, underlying return since entry
    and option return from the fill are one variable to three decimals. Keep the one
    every benchmark is already stated in.
    """
    entry, mark, sig = p["entry"], p["mark"], float(trade.entry_iv)
    ret = mark[idx] / entry - 1.0
    runmax = np.maximum.accumulate(mark)[idx] / entry - 1.0
    cols = ["ret", "tau", "tau_frac", "sig_sqrt_tau", "sess", "runmax", "spot_ret"]
    Xm = np.column_stack([
        ret,
        tau[idx],
        tau[idx] / max(tau[0], 1e-12),
        sig * np.sqrt(tau[idx]),
        p["sess"][idx].astype(float),
        runmax,
        p["spot"][idx] / p["spot0"] - 1.0,
    ])
    return Xm, cols


# ---------------------------------------------------------------- policies
# Every constructor returns a FACTORY: `evaluate` calls it once per trade to get a
# fresh stepper `f(s: dict, k: int) -> bool`. That matters because a policy may carry
# state within a path — `adaptive` decides its target at one ordinal and remembers it —
# and sharing one object across trades would leak that decision.
def hold():
    def make():
        return lambda s, k: False
    return make


def flat(tp: float | None = None, sl: float | None = None):
    """Fixed target and/or stop. Stop is checked first, as `exit_rules.simulate` does."""
    def make():
        def step(s, k):
            if sl is not None and s["ret"] <= -sl / 100:
                return True
            return tp is not None and s["ret"] >= tp / 100
        return step
    return make


def boundary(a: float, b: float):
    """Target that decays toward expiry: exit when ret >= a + b * (tau / tau_0)."""
    def make():
        return lambda s, k: s["ret"] >= a + b * s["tau_frac"]
    return make


def adaptive(decide_k: int, tp_green: float, tp_red: float | None):
    """The incumbent: look once at ordinal `decide_k`, choose a target there and only there.

    Nothing is armed before the check — reading the outcome at `decide_k` and letting a
    target fire earlier is the lookahead that `exit_rules.simulate` documents.
    """
    def make():
        chosen = {}

        def step(s, k):
            if k < decide_k:
                return False
            if k == decide_k:
                chosen["tp"] = tp_green if s["ret"] > 0 else tp_red
                if chosen["tp"] is None:
                    return True                       # cut at the check
            tp = chosen.get("tp")
            return tp is not None and s["ret"] >= tp / 100
        return step
    return make


# ---------------------------------------------------------------- evaluate
def prepare(paths: dict, trades: pd.DataFrame, bars: dict, idx, grid: str = "daily") -> dict:
    """Decision bars and state, once per trade. A boundary grid search re-walks these
    hundreds of times; rebuilding tau each pass costs more than the search itself."""
    out = {}
    for i in idx:
        p, trade = paths[i], trades.loc[i]
        d = decision_idx(p, grid)
        Xm, cols = state_matrix(p, trade, path_tau(p, trade, bars), d)
        out[i] = (d, Xm, cols)
    return out


def evaluate(paths: dict, trades: pd.DataFrame, bars: dict, idx, make_policy,
             grid: str = "daily", cost_frac: float = 0.0, prep: dict | None = None) -> pd.DataFrame:
    """Walk each path, one row per trade.

    `cost_frac` is a half-spread paid on any early exit, as a fraction of the mark.
    Expiry settles at real intrinsic and pays nothing — which is exactly why leaving
    it at zero flatters every early-exit rule.
    """
    rows = []
    for i in idx:
        p = paths[i]
        trade = trades.loc[i]
        d, Xm, cols = prep[i] if prep is not None else (
            lambda dd: (dd, *state_matrix(p, trade, path_tau(p, trade, bars), dd))
        )(decision_idx(p, grid))
        step = make_policy()
        out = None
        for k in range(len(d)):
            s = dict(zip(cols, Xm[k]))
            if step(s, k):
                gross = float(Xm[k][0])
                net = (1.0 + gross) * (1.0 - cost_frac) - 1.0
                out = {"ret": net, "reason": "policy", "bar": int(d[k]), "k": k,
                       "sessions": int(p["sess"][d[k]])}
                break
        if out is None:
            out = {"ret": p["settle"] / p["entry"] - 1.0, "reason": "expiry",
                   "bar": len(p["mark"]) - 1, "k": len(d), "sessions": int(p["sess"][-1])}
        rows.append({"idx": i, "ticker": trade.ticker, "direction": trade.direction,
                     "cycle": str(trade.expiry), "entry": p["entry"], **out})
    return pd.DataFrame(rows)


def oracle(paths: dict, trades: pd.DataFrame, idx, grid: str = "daily") -> pd.DataFrame:
    """Perfect foresight on the same grid — the denominator every arm is reported against."""
    rows = []
    for i in idx:
        p = paths[i]
        d = decision_idx(p, grid)
        best = max(float((p["mark"][d] / p["entry"] - 1.0).max()), p["settle"] / p["entry"] - 1.0)
        rows.append({"idx": i, "ticker": trades.loc[i, "ticker"], "ret": best})
    return pd.DataFrame(rows)


# ------------------------------------------------------------- uncertainty
def paired_cluster_ci(a: pd.DataFrame, b: pd.DataFrame, n: int = 2000, seed: int = SEED):
    """Mean of (a - b) with a ticker-clustered CI on the DIFFERENCE.

    Overlapping CIs on two levels answer a different question; events inside one
    ticker are not independent, and two tickers are 29% of this book.
    """
    j = a.set_index("idx")[["ticker", "ret"]].join(b.set_index("idx")[["ret"]], rsuffix="_b")
    d = (j.ret - j.ret_b).values
    tk = j.ticker.values
    rng = np.random.default_rng(seed)
    uniq = np.unique(tk)
    means = []
    for _ in range(n):
        pick = rng.choice(uniq, len(uniq), replace=True)
        v = np.concatenate([d[tk == t] for t in pick])
        if len(v):
            means.append(v.mean())
    return float(d.mean()), float(np.percentile(means, 2.5)), float(np.percentile(means, 97.5))


# -------------------------------------------------------------------- fits
def fit_flat_tp(paths, trades, bars, idx, grid="daily", cost_frac=0.0,
                candidates=tuple(range(10, 310, 10)), prep=None) -> tuple[float, float]:
    """Best single take-profit on `idx`. One free parameter — the honest competitor."""
    prep = prep if prep is not None else prepare(paths, trades, bars, idx, grid)
    best, best_m = None, -np.inf
    for tp in candidates:
        m = evaluate(paths, trades, bars, idx, flat(tp=tp), grid, cost_frac, prep).ret.mean()
        if m > best_m:
            best, best_m = float(tp), float(m)
    return best, best_m


def fit_boundary(paths, trades, bars, idx, grid="daily", cost_frac=0.0, prep=None):
    """Tier 1: two parameters, `exit if ret >= a + b * tau_frac`.

    A target that decays toward expiry. This is the low-dimensional projection of what
    an LSMC would produce anyway — under frozen IV the state collapses to (return, time).
    """
    prep = prep if prep is not None else prepare(paths, trades, bars, idx, grid)
    best, best_m = None, -np.inf
    for a in np.arange(0.0, 1.55, 0.1):
        for b in np.arange(-0.6, 1.05, 0.1):
            m = evaluate(paths, trades, bars, idx, boundary(a, b), grid, cost_frac, prep).ret.mean()
            if m > best_m:
                best, best_m = (float(a), float(b)), float(m)
    return best, best_m


def loo_cycle(paths, trades, bars, idx, grid="daily", cost_frac=0.0) -> pd.DataFrame:
    """GATE 0 — fit the one-parameter policy on three expiry cycles, test on the fourth.

    If a single free parameter cannot transfer across these four regimes, nothing with
    more capacity will; the extra flexibility only buys a prettier in-sample number.
    """
    exp = trades.loc[idx, "expiry"].astype(str)
    rows = []
    for c in cycles(trades, {i: None for i in idx}):
        tr = [i for i in idx if exp[i] != c]
        te = [i for i in idx if exp[i] == c]
        if not te or not tr:
            continue
        tp, in_s = fit_flat_tp(paths, trades, bars, tr, grid, cost_frac)
        pte = prepare(paths, trades, bars, te, grid)
        oos = evaluate(paths, trades, bars, te, flat(tp=tp), grid, cost_frac, pte).ret.mean()
        hold_oos = evaluate(paths, trades, bars, te, hold(), grid, cost_frac, pte).ret.mean()
        rows.append({"held_out": c, "n_test": len(te), "fitted_tp": tp,
                     "in_sample": in_s, "oos": float(oos), "oos_hold": float(hold_oos),
                     "vs_hold": float(oos - hold_oos)})
    return pd.DataFrame(rows)


# ----------------------------------------------------------- reconciliation
def check_reconciles(paths, trades, bars, idx) -> None:
    """`evaluate` on the fine grid with no cost must equal `exit_rules.simulate`."""
    rule = X.Rule(tp=100, sl=56)
    mine = evaluate(paths, trades, bars, idx, flat(tp=100, sl=56), grid="fine", cost_frac=0.0)
    theirs = pd.DataFrame([{"idx": i, "ret": X.simulate(paths[i], rule)["ret"]} for i in idx])
    j = mine.set_index("idx")[["ret"]].join(theirs.set_index("idx")[["ret"]], rsuffix="_x")
    worst = (j.ret - j.ret_x).abs().max()
    assert worst < 1e-9, f"evaluate diverges from exit_rules.simulate by {worst:.2e}"
    print(f"  reconciliation OK — {len(j)} trades, max |diff| {worst:.2e}")


def compare(paths, trades, bars, train, test, grid="daily", cost_frac=0.02) -> pd.DataFrame:
    """Every arm on one grid, against hold, with a paired ticker-clustered CI.

    The test-fitted target is reported as an upper bound and labelled; tuning a
    benchmark on the test set is leakage that biases the comparison in its favour.
    """
    ptr = prepare(paths, trades, bars, train, grid)
    pte = prepare(paths, trades, bars, test, grid)
    tp_tr, _ = fit_flat_tp(paths, trades, bars, train, grid, cost_frac, prep=ptr)
    tp_te, _ = fit_flat_tp(paths, trades, bars, test, grid, cost_frac, prep=pte)
    (a, b), _ = fit_boundary(paths, trades, bars, train, grid, cost_frac, prep=ptr)
    orc = oracle(paths, trades, test, grid).ret.mean()
    arms = [
        ("hold to expiry", hold(), False),
        ("flat TP fitted on train", flat(tp=tp_tr), False),
        ("Tier 0: flat +30, unfitted", flat(tp=30), False),
        (f"Tier 1: ret >= {a:.1f} {b:+.1f}*tau", boundary(a, b), False),
        ("adaptive 70/20 (incumbent)", adaptive(1, 70, 20), False),
        ("flat TP fitted on TEST", flat(tp=tp_te), True),
    ]
    base = evaluate(paths, trades, bars, test, hold(), grid, cost_frac, pte)
    rows = []
    for name, pol, is_oracle in arms:
        r = evaluate(paths, trades, bars, test, pol, grid, cost_frac, pte)
        d, lo, hi = paired_cluster_ci(r, base)
        rows.append({"arm": name, "mean": r.ret.mean(), "win": (r.ret > 0).mean(),
                     "pct_oracle": r.ret.mean() / orc, "vs_hold": d, "lo": lo, "hi": hi,
                     "upper_bound_only": is_oracle})
    out = pd.DataFrame(rows)
    out.attrs.update(grid=grid, cost=cost_frac, oracle=orc, tp_train=tp_tr, tp_test=tp_te, a=a, b=b)
    return out


if __name__ == "__main__":
    trades, bars, paths = load()
    cs = cycles(trades, paths)
    split = split_expiry(trades, paths)
    train, test = split_index(trades, paths, split)
    print(f"{len(paths)} simulable paths · {len(cs)} expiry cycles {cs[0]} .. {cs[-1]}")
    print(f"train {len(train)} (cycles <= {split.date()}) · test {len(test)}")

    print("\n=== reconciliation against exit_rules.simulate ===")
    check_reconciles(paths, trades, bars, sorted(paths))

    print("\n=== GATE 0 — one free parameter, leave one expiry cycle out (daily grid, no cost) ===")
    g = loo_cycle(paths, trades, bars, sorted(paths))
    print(g.to_string(index=False, float_format=lambda x: f"{x:,.3f}"))
    flips = int(((g.in_sample > 0) & (g.oos < 0)).sum())
    print(f"\n  folds where an in-sample gain became an out-of-sample loss: {flips}/{len(g)}")
    print(f"  mean out-of-sample vs hold: {g.vs_hold.mean():+.3f}")
    passed = flips < len(g) - 1 and g.vs_hold.mean() > 0
    print("\n  VERDICT: " + ("gate passed — a fitted policy is worth attempting" if passed else
                             "gate FAILED — do not fit a higher-capacity policy on this data"))

    for grid in ("daily", "fine"):
        c = compare(paths, trades, bars, train, test, grid=grid)
        print(f"\n=== {grid.upper()} grid · test n={len(test)} · cost {c.attrs['cost']:.0%} "
              f"· oracle {c.attrs['oracle']:+.1%} ===")
        print(f"  train-fitted TP {c.attrs['tp_train']:.0f}%  ·  test-fitted TP "
              f"{c.attrs['tp_test']:.0f}% (upper bound)  ·  boundary a={c.attrs['a']:.1f} "
              f"b={c.attrs['b']:+.1f}")
        print(f"  {'arm':<32}{'mean':>8}{'win':>7}{'%orc':>7}{'vs hold':>9}{'95% CI':>20}")
        for r in c.itertuples():
            tag = "  <- upper bound, not a result" if r.upper_bound_only else ""
            print(f"  {r.arm:<32}{r.mean:>+8.1%}{r.win:>7.0%}{r.pct_oracle:>7.0%}"
                  f"{r.vs_hold:>+9.1%}   [{r.lo:+.1%}, {r.hi:+.1%}]{tag}")
