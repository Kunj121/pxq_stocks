"""Price panels around each logged sweep.

    python research/sweep_events/plot_events.py
    python research/sweep_events/plot_events.py --since 2026-08-01 --until 2026-08-31 \
        --out sweep_price_panels_aug.png

Reads sweeps.parquet, writes a panel per sweep. Add a row to the log and re-run
to redraw; filter with --since/--until to chart one window.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
sys.path[:0] = [str(HERE), str(ROOT)]
from backtest.data import load_bars  # noqa: E402
from gates import split_factor        # noqa: E402

OUT = ROOT / "out" / "sweep_events"
PRE = POST = 70                      # hourly bars either side (~10 sessions)
SURF, INK, INK2, MUTED, MARK = "#fcfcfb", "#0b0b0b", "#52514e", "#b8b7b0", "#e34948"
SERIES = "#2a78d6"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", help="only sweeps on/after this date (YYYY-MM-DD)")
    ap.add_argument("--until", help="only sweeps on/before this date")
    ap.add_argument("--out", default="sweep_price_panels.png")
    ap.add_argument("--title", default="ISO sweeps")
    ap.add_argument("--month", help="YYYY-MM — sets since/until/out/title in one go")
    a = ap.parse_args()

    if a.month:
        start = pd.Timestamp(a.month + "-01")
        a.since = start.strftime("%Y-%m-%d")
        a.until = (start + pd.offsets.MonthEnd(1)).strftime("%Y-%m-%d")
        a.out = f"sweep_price_panels_{start:%Y_%m}.png"
        a.title = f"{start:%B %Y} ISO sweeps"

    sweeps = pd.read_parquet(HERE / "sweeps.parquet")
    ts = pd.to_datetime(sweeps.swept_at)
    if a.since:
        sweeps = sweeps[ts >= pd.Timestamp(a.since)]
    if a.until:
        sweeps = sweeps[ts <= pd.Timestamp(a.until) + pd.Timedelta(days=1)]
    sweeps = sweeps.sort_values("swept_at").reset_index(drop=True)
    if sweeps.empty:
        raise SystemExit("no sweeps in that window")
    bars = load_bars(sorted(sweeps.symbol.unique()), "2025-09-19",
                     timeframe="1Hour", session="regular").frame
    close = bars["close"].unstack("symbol").sort_index()

    plt.rcParams.update({
        "font.size": 9, "axes.facecolor": SURF, "figure.facecolor": SURF,
        "text.color": INK, "axes.labelcolor": INK2, "xtick.color": INK2,
        "ytick.color": INK2, "axes.edgecolor": MUTED, "axes.linewidth": .8})

    n = len(sweeps)
    ncol = 3
    nrow = int(np.ceil(n / ncol))
    fig, axes = plt.subplots(nrow, ncol, figsize=(5 * ncol, 3.8 * nrow), squeeze=False)
    bb = dict(boxstyle="round,pad=0.28", fc=SURF, ec="none", alpha=.94)

    for ax, (_, s) in zip(axes.ravel(), sweeps.iterrows()):
        ser = close[s.symbol].dropna()
        # The bar CONTAINING the sweep, not the next one: a 15:50 print belongs to
        # the 15:00 bar. Snapping forward lands on the next session's open, which
        # across an overnight gap misplaces the marker badly (OKTA gapped ~25% on
        # earnings between its 15:50 sweep and the following open).
        t = pd.Timestamp(s.swept_at, tz=ser.index.tz)
        i = max(0, min(ser.index.searchsorted(t, side="right") - 1, len(ser) - 1))
        # Bars are split-adjusted; the vendor's print is not. Put strike and spot
        # into the bars' terms or the marker lands nowhere near the series.
        f, ok = split_factor(s.spot, ser.iloc[i])
        spot, strike = s.spot / f, s.strike / f
        if not ok:
            spot = strike = float("nan")
        lo, hi = max(0, i - PRE), min(len(ser), i + POST + 1)
        w = ser.iloc[lo:hi]
        x, xi = np.arange(len(w)), i - lo

        ax.axvspan(0, xi, color=MUTED, alpha=.16, lw=0)
        ax.plot(x, w.values, color=SERIES, lw=1.6, solid_capstyle="round")
        ax.axhline(strike, color=INK2, lw=1.1, ls=(0, (4, 3)))
        ax.axvline(xi, color=MARK, lw=1.4)
        # Anchor on the vendor's printed spot, never the bar close — that is the
        # price the trade was actually struck against.
        ax.plot([xi], [spot], "o", ms=7, color=MARK,
                markeredgecolor=SURF, markeredgewidth=2, zorder=5)

        move = (w.iloc[-1] / spot - 1) * 100
        ax.set_title(f"{s.label}   ${s.premium_musd:.2f}M   Δ{s.vendor_delta:+.2f}",
                     fontsize=10, weight="bold", color=INK, loc="left", pad=9)
        tag = f"strike {strike:g}" + (f"  (split {f:g}:1)" if f != 1.0 else "")
        ax.text(.02, .955, tag, transform=ax.transAxes,
                fontsize=7.5, color=INK2, va="top", bbox=bb)
        ax.text(.98, .05, f"{move:+.1f}% since signal", transform=ax.transAxes,
                fontsize=8.5, color=INK, va="bottom", ha="right",
                weight="bold", bbox=bb)
        ticks = [0, xi, len(w) - 1]
        ax.set_xticks(ticks)
        ax.set_xticklabels([w.index[t].strftime("%b %-d") for t in ticks], fontsize=7.5)
        for sp in ("top", "right"):
            ax.spines[sp].set_visible(False)
        ax.grid(axis="y", color=MUTED, alpha=.35, lw=.6)
        ax.set_axisbelow(True)

    for ax in axes.ravel()[n:]:
        ax.set_visible(False)

    fig.suptitle(f"{a.title} — underlying around the signal   "
                 "(shaded = before signal · red = signal · dashed = strike)",
                 fontsize=11.5, weight="bold", color=INK, x=.008, ha="left", y=.985)
    fig.tight_layout(rect=[0, 0, 1, .945])
    OUT.mkdir(parents=True, exist_ok=True)
    fig.savefig(OUT / a.out, dpi=170)
    print(f"wrote {len(sweeps)} panels -> {OUT / a.out}")


if __name__ == "__main__":
    main()
