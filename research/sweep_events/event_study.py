"""Does the underlying jump after a sweep signal?

Model-free event study on the 15-minute bars: for every trigger with a fill, the
underlying's return from the signal spot to fixed horizons after the print, signed
in the trade's direction (a BEAR signal earns a positive number when the stock
falls), raw and net of SPY over the same bars. Horizons *before* the print are
included so a jump can be told apart from a chase — a sweep that prints after the
move has already happened looks very different from one that precedes it.

    python research/sweep_events/event_study.py                 # all filled triggers
    python research/sweep_events/event_study.py data/exit_lab/PUT_CONFIG.json   # one export's signals
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
DATA = HERE.parents[1] / "data"
TZ = "America/New_York"
# label -> bars relative to the first bar at/after the print (26 regular bars per session)
HORIZONS = [("-1d", -26), ("-2h", -8), ("-1h", -4), ("-15m", -1), ("+15m", 1), ("+30m", 2), ("+1h", 4),
            ("+2h", 8), ("EOD", "eod"), ("+1d", 26), ("+2d", 52), ("+5d", 130), ("+10d", 260), ("expiry", "expiry")]


def load_bars() -> dict[str, pd.DataFrame]:
    b = pd.read_parquet(DATA / "bars_15min_raw.parquet")
    b["ts"] = pd.to_datetime(b["ts"]).dt.tz_convert(TZ).dt.tz_localize(None)
    return {s: g.reset_index(drop=True) for s, g in b.groupby("symbol")}


def study(trades: pd.DataFrame, bars: dict) -> pd.DataFrame:
    spy = bars["SPY"]; spy_t = spy["ts"].values
    rows = []
    for t in trades.itertuples():
        b = bars.get(t.ticker)
        if b is None:
            continue
        ts = b["ts"].values; i0 = int(np.searchsorted(ts, np.datetime64(t.confirm_ts)))
        if i0 >= len(ts):
            continue
        sess = pd.Series(b["ts"].dt.normalize().values)
        s0 = float(t.confirm_spot); dirn = 1 if t.direction == "BULL" else -1
        spy0 = float(spy["close"].iloc[max(0, int(np.searchsorted(spy_t, ts[i0])) - 1)]) if spy_t[min(len(spy_t)-1, int(np.searchsorted(spy_t, ts[i0])))] != ts[i0] else float(spy["close"].iloc[int(np.searchsorted(spy_t, ts[i0]))])
        row = {"ticker": t.ticker, "direction": t.direction, "confirm_ts": t.confirm_ts}
        for label, h in HORIZONS:
            if h == "eod":
                j = int(sess[sess == sess.iloc[i0]].index.max())
            elif h == "expiry":
                if pd.isna(t.exit_spot):
                    continue
                row[label] = dirn * (float(t.exit_spot) / s0 - 1); row[label + "_x"] = np.nan
                continue
            else:
                j = i0 + h
            if j < 0 or j >= len(ts):
                continue
            px = float(b["close"].iloc[j]); und = dirn * (px / s0 - 1)
            k = int(np.searchsorted(spy_t, ts[j])); k = k if k < len(spy_t) and spy_t[k] == ts[j] else max(0, k - 1)
            spy_ret = float(spy["close"].iloc[k]) / spy0 - 1
            row[label] = und; row[label + "_x"] = und - dirn * spy_ret
        rows.append(row)
    return pd.DataFrame(rows)


def table(ev: pd.DataFrame) -> str:
    out = ["  horizon     n    mean    median   %>0   |  net of SPY: mean   median   %>0"]
    for label, _ in HORIZONS:
        if label not in ev:
            continue
        v = ev[label].dropna(); x = ev[label + "_x"].dropna()
        xs = f"{x.mean():+7.2%}  {x.median():+7.2%}  {(x>0).mean():5.0%}" if len(x) else "      —"
        out.append(f"  {label:>7}  {len(v):4d}  {v.mean():+7.2%}  {v.median():+7.2%}  {(v>0).mean():5.0%}   |  {xs}")
    return "\n".join(out)


# ---------------------------------------------------------------- excursions
# A fixed-horizon table averages a jump away: a name that gaps +12% on day 6 and
# gives half back by day 10 shows nothing at +10d. These measure the best and worst
# the window ever offered, and whether any single bar or day was an outlier move.
WINDOW_SESSIONS = 10
BARS_PER_SESSION = 26


def _window_stats(close: np.ndarray, sess: np.ndarray, i0: int, dirn: int,
                  bar_sd: float, n_bars: int) -> dict | None:
    """Signed excursions and jump sizes over `n_bars` after i0."""
    j1 = min(len(close), i0 + n_bars + 1)
    if j1 - i0 < 3:
        return None
    seg = close[i0:j1]
    s0 = seg[0]
    signed = dirn * (seg / s0 - 1.0)
    k_mfe = int(np.argmax(signed))
    bar_ret = dirn * (seg[1:] / seg[:-1] - 1.0)
    # session closes inside the window -> day-over-day moves
    ss = sess[i0:j1]
    last = np.where(np.diff(ss) != 0)[0]
    day_close = seg[np.append(last, len(seg) - 1)]
    day_ret = dirn * (day_close[1:] / day_close[:-1] - 1.0) if len(day_close) > 1 else np.array([])
    return {
        "mfe": float(signed.max()), "mfe_session": int(ss[k_mfe] - ss[0]),
        "mae": float(signed.min()), "end": float(signed[-1]),
        "max_bar": float(bar_ret.max()) if len(bar_ret) else np.nan,
        "min_bar": float(bar_ret.min()) if len(bar_ret) else np.nan,
        "max_day": float(day_ret.max()) if len(day_ret) else np.nan,
        "min_day": float(day_ret.min()) if len(day_ret) else np.nan,
        "jump_z": float(bar_ret.max() / bar_sd) if len(bar_ret) and bar_sd > 0 else np.nan,
    }


def excursions(trades: pd.DataFrame, bars: dict, controls: int = 20,
               sessions: int = WINDOW_SESSIONS, seed: int = 0) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Event windows and matched random-entry controls on the same tickers.

    The control is the whole point: "60% of these names moved 5% at some point in
    ten days" says nothing until you know what a random ten days on the same names
    does. Each trigger draws `controls` random entry bars from its own ticker, at
    least one session away from any real trigger on it, with the same direction.
    """
    rng = np.random.default_rng(seed)
    n_bars = sessions * BARS_PER_SESSION
    prep, real, ctrl = {}, [], []
    for sym, b in bars.items():
        c = b["close"].to_numpy(float)
        s = b["ts"].dt.normalize().factorize()[0]
        r = np.diff(c) / c[:-1]
        prep[sym] = (c, s, float(np.std(r[np.abs(r) < 0.5])) if len(r) else np.nan, b["ts"].to_numpy())

    by_ticker = {t: g["confirm_ts"].to_numpy() for t, g in trades.groupby("ticker")}
    for t in trades.itertuples():
        if t.ticker not in prep:
            continue
        c, s, sd, ts = prep[t.ticker]
        i0 = int(np.searchsorted(ts, np.datetime64(t.confirm_ts)))
        dirn = 1 if t.direction == "BULL" else -1
        st = _window_stats(c, s, i0, dirn, sd, n_bars)
        if st is None:
            continue
        real.append({"ticker": t.ticker, "direction": t.direction, "confirm_ts": t.confirm_ts, **st})
        # controls: same ticker, same direction, away from every real trigger on it
        busy = np.searchsorted(ts, by_ticker[t.ticker].astype("datetime64[ns]"))
        pool = np.arange(0, len(c) - n_bars - 1)
        pool = pool[np.min(np.abs(pool[:, None] - busy[None, :]), axis=1) > BARS_PER_SESSION]
        if not len(pool):
            continue
        for k in rng.choice(pool, size=min(controls, len(pool)), replace=False):
            cs = _window_stats(c, s, int(k), dirn, sd, n_bars)
            if cs:
                ctrl.append({"ticker": t.ticker, "direction": t.direction, **cs})
    return pd.DataFrame(real), pd.DataFrame(ctrl)


def excursion_table(real: pd.DataFrame, ctrl: pd.DataFrame, label: str = "") -> str:
    def blk(d, name):
        if not len(d):
            return f"  {name}: n=0"
        return (f"  {name:<10} n={len(d):<5} MFE mean {d.mfe.mean():+6.2%} median {d.mfe.median():+6.2%}"
                f" | MAE mean {d.mae.mean():+6.2%} | end {d.end.mean():+6.2%}"
                f" | max day {d.max_day.mean():+6.2%} / min day {d.min_day.mean():+6.2%}")
    out = [f"{label}", blk(real, "signal"), blk(ctrl, "control")]
    if len(real) and len(ctrl):
        out.append(f"  difference  MFE {real.mfe.mean()-ctrl.mfe.mean():+.2%}  "
                   f"MAE {real.mae.mean()-ctrl.mae.mean():+.2%}  end {real.end.mean()-ctrl.end.mean():+.2%}")
        for thr in (0.05, 0.10, 0.20):
            out.append(f"    MFE >= {thr:.0%}: signal {(real.mfe >= thr).mean():5.1%}   control {(ctrl.mfe >= thr).mean():5.1%}")
        for z in (3, 5):
            out.append(f"    a favourable bar >= {z} sd: signal {(real.jump_z >= z).mean():5.1%}   control {(ctrl.jump_z >= z).mean():5.1%}")
        out.append(f"    day move >= +5%: signal {(real.max_day >= .05).mean():5.1%}   control {(ctrl.max_day >= .05).mean():5.1%}"
                   f"   |  <= -5%: signal {(real.min_day <= -.05).mean():5.1%}  control {(ctrl.min_day <= -.05).mean():5.1%}")
        out.append(f"    days to MFE: signal median {real.mfe_session.median():.0f}   control median {ctrl.mfe_session.median():.0f}")
    return "\n".join(out)


if __name__ == "__main__":
    bars = load_bars()
    if len(sys.argv) > 1:
        sys.path.insert(0, str(HERE)); import exit_lab
        _, sig = exit_lab.load(sys.argv[1])
        log = pd.read_parquet(DATA / "dashboard_trades.parquet").reset_index().rename(columns={"index": "trade_index"})
        trades = log[log.trade_index.isin(sig.trade_index)]
        print(f"signals from {sys.argv[1]}: {len(trades)}")
    else:
        trades = pd.read_parquet(DATA / "dashboard_trades.parquet"); trades = trades[trades.simulable]
        print(f"all filled triggers: {len(trades)}")
    ev = study(trades, bars)
    print("signed in the trade's direction (BEAR flipped); net-of-SPY subtracts SPY's move over the same bars\n")
    print(table(ev))
    for d in ("BULL", "BEAR"):
        sub = ev[ev.direction == d]
        if len(sub):
            print(f"\n{d} only ({len(sub)}):"); print(table(sub))

    # Excursions come last because they are the slower pass. The control draw walks
    # the rng once per trade, so it is only reproducible when the whole set is passed
    # in one call and sliced afterwards — never by re-running on a subset.
    print("\n\n" + "=" * 78)
    real, ctrl = excursions(trades, bars)
    print(excursion_table(real, ctrl, f"ALL ({len(real)} signals vs {len(ctrl)} random-entry windows)"))
    for d in ("BULL", "BEAR"):
        if (real.direction == d).any():
            print()
            print(excursion_table(real[real.direction == d], ctrl[ctrl.direction == d], f"{d} ONLY"))
