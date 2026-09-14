"""Daily ORB dashboard — what fired, why, and how each trade played out.

    python dashboard.py                  # today
    python dashboard.py --date 2026-09-14
    python dashboard.py --open           # and open it in a browser

Writes a self-contained HTML file to execution/logs/orb_dashboard_<date>.html.
No external assets, so it opens from disk with no server.

The point of this file is the *reasoning*, not the P&L. A selection table alone
cannot tell you why a name was chosen, because the interesting information is in
what was rejected and at which gate. So the dashboard leads with the funnel —
every name in the universe, the gate it died at, and how far it missed — and only
then shows the positions and their charts.
"""

from __future__ import annotations

import argparse
import base64
import io
import logging
import sys
import webbrowser
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import Rectangle

HERE = Path(__file__).resolve().parent
STRAT = HERE.parent
ROOT = STRAT.parent.parent
sys.path[:0] = [str(HERE), str(STRAT), str(ROOT)]

from daily_orb import RobinhoodProfile, _live_bars, _universe, build_plan, build_signals
from orb import ORBConfig, daily_features, simulate_symbol

LOG_DIR = ROOT / "execution" / "logs"

logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("backtest").setLevel(logging.WARNING)
log = logging.getLogger("orb.dash")

# Validated categorical palette (see the dataviz palette reference).
C = {"blue": "#2a78d6", "orange": "#eb6834", "aqua": "#1baf7a",
     "yellow": "#eda100", "violet": "#4a3aa7", "red": "#e34948",
     "green": "#008300"}
INK, INK2, GRID, SURF = "#0b0b0b", "#52514e", "#e4e3df", "#fcfcfb"

plt.rcParams.update({
    "figure.facecolor": SURF, "axes.facecolor": SURF, "savefig.facecolor": SURF,
    "axes.edgecolor": GRID, "axes.linewidth": 1.0,
    "axes.grid": True, "grid.color": GRID, "grid.linewidth": 0.7,
    "axes.spines.top": False, "axes.spines.right": False,
    "text.color": INK, "axes.labelcolor": INK2, "axes.titlecolor": INK,
    "xtick.color": INK2, "ytick.color": INK2,
    "font.size": 9, "axes.titlesize": 10.5, "axes.titleweight": "bold",
    "legend.frameon": False, "figure.dpi": 110, "lines.linewidth": 1.6,
})


# --------------------------------------------------------------------------- #
# the funnel — why each name did or did not make the book
# --------------------------------------------------------------------------- #

GATES = [
    ("opening range", "no 09:30 bar — the range must start at the bell"),
    ("price > $5", "penny-stock filter"),
    ("avg vol >= 1M", "14-day average volume, liquidity floor"),
    ("ATR > $0.50", "needs enough daily range to pay for the spread"),
    ("direction", "doji opening range — open == close, no direction"),
    ("RelVol >= 1.0", "opening-range volume below its own 14-day average"),
    ("top 20 by RelVol", "qualified, but out-ranked"),
    ("shares > 0", "stop too wide for the book — quantized to zero"),
]


def build_funnel(sig: pd.DataFrame, plan: pd.DataFrame, cfg: ORBConfig,
                 prof: RobinhoodProfile, universe: list[str]) -> pd.DataFrame:
    """One row per universe name with the gate it died at, and the margin."""
    rows = []
    have = set(sig["symbol"])
    for s in universe:
        if s not in have:
            rows.append({"symbol": s, "gate": "opening range", "detail": "no data",
                         "relvol": np.nan, "passed": False})
    s = sig.copy()
    s["direction"] = np.where(s.or_close > s.or_open, 1,
                              np.where(s.or_close < s.or_open, -1, 0))
    selected = set(plan["symbol"]) if len(plan) else set()
    # A name that qualified but was out-ranked needs the pre-top-N cut to be known.
    qualified = s[(s.day_open > cfg.min_price) & (s.avg_volume >= cfg.min_avg_volume)
                  & (s.atr > cfg.min_atr) & (s.direction != 0)
                  & (s.rel_volume >= cfg.min_rel_volume)]
    ranked = qualified.sort_values("rel_volume", ascending=False)
    in_top = set(ranked.head(prof.max_positions)["symbol"])

    for _, r in s.iterrows():
        sym = r["symbol"]
        if sym in selected:
            rows.append({"symbol": sym, "gate": "SELECTED", "detail": "",
                         "relvol": r.rel_volume, "passed": True})
        elif not (r.day_open > cfg.min_price):
            rows.append({"symbol": sym, "gate": "price > $5",
                         "detail": f"open ${r.day_open:,.2f}", "relvol": r.rel_volume,
                         "passed": False})
        elif not (r.avg_volume >= cfg.min_avg_volume):
            rows.append({"symbol": sym, "gate": "avg vol >= 1M",
                         "detail": f"{r.avg_volume/1e6:,.2f}M", "relvol": r.rel_volume,
                         "passed": False})
        elif not (r.atr > cfg.min_atr):
            rows.append({"symbol": sym, "gate": "ATR > $0.50",
                         "detail": f"ATR ${r.atr:,.2f}", "relvol": r.rel_volume,
                         "passed": False})
        elif r.direction == 0:
            rows.append({"symbol": sym, "gate": "direction", "detail": "doji",
                         "relvol": r.rel_volume, "passed": False})
        elif not (r.rel_volume >= cfg.min_rel_volume):
            rows.append({"symbol": sym, "gate": "RelVol >= 1.0",
                         "detail": f"{r.rel_volume:.2f}x", "relvol": r.rel_volume,
                         "passed": False})
        elif sym not in in_top:
            rows.append({"symbol": sym, "gate": "top 20 by RelVol",
                         "detail": f"{r.rel_volume:.2f}x, out-ranked",
                         "relvol": r.rel_volume, "passed": False})
        else:
            rows.append({"symbol": sym, "gate": "shares > 0",
                         "detail": "stop too wide for the book",
                         "relvol": r.rel_volume, "passed": False})
    return pd.DataFrame(rows)


# --------------------------------------------------------------------------- #
# charts
# --------------------------------------------------------------------------- #

def png(fig) -> str:
    buf = io.BytesIO()
    fig.savefig(buf, format="png", bbox_inches="tight", dpi=110)
    plt.close(fig)
    return base64.b64encode(buf.getvalue()).decode()


def funnel_chart(funnel: pd.DataFrame) -> str:
    order = ["SELECTED"] + [g for g, _ in GATES]
    counts = funnel["gate"].value_counts().reindex(order).fillna(0).astype(int)
    counts = counts[counts > 0]
    fig, ax = plt.subplots(figsize=(8.4, max(2.2, 0.42 * len(counts))))
    colors = [C["blue"] if g == "SELECTED" else GRID for g in counts.index]
    bars = ax.barh(range(len(counts)), counts.values, color=colors, height=0.62, zorder=3)
    for i, (g, v) in enumerate(counts.items()):
        ax.annotate(f" {v}", (v, i), va="center", fontsize=9,
                    color=INK if g == "SELECTED" else INK2,
                    fontweight="bold" if g == "SELECTED" else "normal")
    ax.set_yticks(range(len(counts)))
    ax.set_yticklabels(counts.index, fontsize=9)
    ax.invert_yaxis()
    ax.set_xlabel("names")
    ax.set_title("Where every name in the universe ended up")
    ax.xaxis.grid(True); ax.yaxis.grid(False); ax.set_axisbelow(True)
    ax.set_xlim(0, counts.max() * 1.15)
    return png(fig)


def relvol_chart(funnel: pd.DataFrame) -> str:
    d = funnel.dropna(subset=["relvol"]).sort_values("relvol", ascending=False)
    if d.empty:
        return ""
    fig, ax = plt.subplots(figsize=(8.4, max(2.4, 0.26 * len(d))))
    colors = [C["blue"] if p else GRID for p in d["passed"]]
    ax.barh(range(len(d)), d["relvol"], color=colors, height=0.68, zorder=3)
    ax.axvline(1.0, color=C["red"], lw=1.6, ls="--", zorder=4)
    ax.annotate("RelVol = 1.0\n(its own 14-day average)", xy=(1.0, len(d) * 0.02),
                xytext=(6, 0), textcoords="offset points", fontsize=8.5,
                color=C["red"], va="top")
    ax.set_yticks(range(len(d)))
    ax.set_yticklabels(d["symbol"], fontsize=8)
    ax.invert_yaxis()
    ax.set_xlabel("opening-range volume vs its own 14-day average (x)")
    ax.set_title("Relative volume — the ranking key. Blue = traded.")
    ax.xaxis.grid(True); ax.yaxis.grid(False); ax.set_axisbelow(True)
    return png(fig)


def trade_chart(sym: str, bars: pd.DataFrame, row: pd.Series,
                plan_row: pd.Series | None, cfg: ORBConfig) -> str:
    """Intraday path with the opening range, trigger, fill, stop and exit."""
    b = bars.copy()
    t = b.index
    mod = t.hour * 60 + t.minute
    b = b[(mod >= 570) & (mod < 960)]
    if b.empty:
        return ""
    t = b.index
    fig, ax = plt.subplots(figsize=(8.6, 3.0))
    ax.plot(t, b["close"], color=INK2, lw=1.2, zorder=3)

    or_end = t[0] + pd.Timedelta(minutes=cfg.opening_minutes)
    ax.add_patch(Rectangle((t[0], row.or_low), or_end - t[0], row.or_high - row.or_low,
                           facecolor=C["yellow"], alpha=0.18, edgecolor=C["yellow"],
                           lw=1.0, zorder=2))
    ax.annotate("opening range\n09:30-09:35", xy=(or_end, row.or_high),
                xytext=(6, 4), textcoords="offset points", fontsize=8, color=INK2)

    long_ = row.direction == 1
    trig = row.or_high if long_ else row.or_low
    ax.axhline(trig, color=C["blue"], lw=1.3, ls="--", zorder=4)
    ax.annotate(f"trigger {trig:,.2f}", xy=(t[-1], trig), xytext=(4, 0),
                textcoords="offset points", fontsize=8, color=C["blue"], va="center")

    if pd.notna(row.entry_price):
        et = t[min(int(row.entry_minute) - 575, len(t) - 1)] if pd.notna(row.entry_minute) else t[0]
        ax.scatter([et], [row.entry_price], s=54, color=C["blue"], zorder=6,
                   edgecolor=SURF, linewidth=1.4)
        ax.annotate(f"  entry {row.entry_price:,.2f}", xy=(et, row.entry_price),
                    fontsize=8.5, color=C["blue"], fontweight="bold", va="bottom")
        ax.axhline(row.stop_price, color=C["red"], lw=1.2, ls=":", zorder=4)
        ax.annotate(f"stop {row.stop_price:,.2f}", xy=(t[-1], row.stop_price),
                    xytext=(4, 0), textcoords="offset points", fontsize=8,
                    color=C["red"], va="center")
        if pd.notna(row.exit_price):
            win = row.pnl_r > 0
            col = C["green"] if win else C["red"]
            ax.scatter([t[-1]], [row.exit_price], s=54, marker="X", color=col,
                       zorder=6, edgecolor=SURF, linewidth=1.2)
            ax.annotate(f"  exit {row.exit_price:,.2f} ({row.exit_reason})",
                        xy=(t[-1], row.exit_price), fontsize=8.5, color=col,
                        fontweight="bold", va="top", ha="right")
            lo, hi = sorted([row.entry_price, row.exit_price])
            ax.axhspan(lo, hi, color=col, alpha=0.07, zorder=1)

    side = "LONG" if long_ else "SHORT"
    r = f"{row.pnl_r:+.2f}R" if pd.notna(row.pnl_r) else "not triggered"
    sh = f" · {int(plan_row.shares)} sh" if plan_row is not None else ""
    ax.set_title(f"{sym} — {side} · RelVol {row.rel_volume:.2f}x · {r}{sh}")
    ax.set_ylabel("price")
    import matplotlib.dates as mdates
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M"))
    ax.margins(x=0.02)
    return png(fig)


# --------------------------------------------------------------------------- #
# page
# --------------------------------------------------------------------------- #

CSS = """
:root{--bg:#fcfcfb;--card:#ffffff;--ink:#0b0b0b;--ink2:#52514e;--muted:#8a8985;
--line:#e4e3df;--blue:#2a78d6;--red:#e34948;--green:#008300;--yellow:#eda100;}
*{box-sizing:border-box}
body{margin:0;padding:28px 20px 64px;background:var(--bg);color:var(--ink);
font:15px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",Helvetica,Arial,sans-serif;}
.wrap{max-width:980px;margin:0 auto}
h1{font-size:26px;margin:0 0 4px;letter-spacing:-.01em}
h2{font-size:18px;margin:38px 0 6px;letter-spacing:-.01em}
h3{font-size:14px;margin:22px 0 6px;color:var(--ink2);text-transform:uppercase;
letter-spacing:.06em;font-weight:600}
.sub{color:var(--ink2);margin:0 0 18px;font-size:14px}
.note{color:var(--ink2);font-size:13.5px;margin:6px 0 14px;max-width:74ch}
.cards{display:flex;flex-wrap:wrap;gap:10px;margin:16px 0 6px}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;
padding:11px 14px;min-width:112px}
.card .k{font-size:11px;color:var(--muted);text-transform:uppercase;letter-spacing:.05em}
.card .v{font-size:20px;font-weight:650;margin-top:2px;letter-spacing:-.01em}
.pos{color:var(--green)} .neg{color:var(--red)}
table{border-collapse:collapse;width:100%;font-size:13.5px;margin:8px 0 4px}
th{text-align:left;font-weight:600;color:var(--ink2);border-bottom:1.5px solid var(--line);
padding:7px 9px;font-size:12px;text-transform:uppercase;letter-spacing:.04em}
td{padding:7px 9px;border-bottom:1px solid var(--line)}
tr:last-child td{border-bottom:none}
td.n,th.n{text-align:right;font-variant-numeric:tabular-nums}
.tag{display:inline-block;padding:1px 7px;border-radius:11px;font-size:11.5px;
font-weight:600;letter-spacing:.02em}
.long{background:#e7f0fb;color:#1b5aa6}.short{background:#fdeceb;color:#a92e2d}
.sel{background:#e7f0fb;color:#1b5aa6}.rej{background:#f2f1ee;color:#6b6a66}
img{max-width:100%;height:auto;display:block;margin:10px 0 4px}
.scroll{overflow-x:auto}
.foot{margin-top:44px;padding-top:16px;border-top:1px solid var(--line);
color:var(--muted);font-size:12.5px}
code{background:#f2f1ee;padding:1px 5px;border-radius:4px;font-size:12.5px}
"""


def card(k, v, cls=""):
    return f'<div class="card"><div class="k">{k}</div><div class="v {cls}">{v}</div></div>'


def render(session: date, sig, plan, funnel, results, prof, cfg,
           charts, funnel_png, relvol_png, acct) -> str:
    traded = results[results.exit_reason.isin(["stop", "eod"])] if len(results) else results
    pnl = float(traded["pnl"].sum()) if len(traded) else 0.0
    tot_r = float(traded["pnl_r"].sum()) if len(traded) else 0.0
    wins = int((traded["pnl"] > 0).sum()) if len(traded) else 0
    gross = float(plan["notional"].sum()) if len(plan) else 0.0

    h = [f"<h1>5-minute ORB — {session:%A %d %B %Y}</h1>",
         f'<p class="sub">Alpaca paper <code>{acct}</code> · sized as '
         f'${prof.capital:,.0f} at {prof.max_leverage:g}× · '
         f'{"long + short" if prof.allow_short else "long only"} · '
         f'{len(funnel)} names screened</p>']

    h.append('<div class="cards">')
    h.append(card("selected", len(plan)))
    h.append(card("triggered", len(traded)))
    h.append(card("winners", f"{wins}/{len(traded)}" if len(traded) else "—"))
    h.append(card("total R", f"{tot_r:+.2f}", "pos" if tot_r > 0 else "neg"))
    h.append(card("P&L", f"${pnl:+,.2f}", "pos" if pnl > 0 else "neg"))
    h.append(card("gross", f"${gross:,.0f}"))
    h.append(card("leverage", f"{gross/prof.capital:.2f}×"))
    h.append("</div>")

    h.append("<h2>Why these names</h2>")
    h.append('<p class="note">The strategy is a funnel. Every name in the universe '
             'is screened each morning and most are rejected — the table below shows '
             'exactly which gate each one died at, because that is where the reasoning '
             'lives. The last gate, <em>top 20 by RelVol</em>, is the only one that is '
             'competitive rather than absolute: those names qualified and were simply '
             'out-ranked.</p>')
    if funnel_png:
        h.append(f'<img src="data:image/png;base64,{funnel_png}" alt="selection funnel">')

    h.append("<h3>Relative volume — the ranking key</h3>")
    h.append('<p class="note">RelVol is today\'s 09:30–09:35 volume divided by the same '
             "stock's average over its previous 14 sessions. Above 1.0 means the stock "
             'is unusually busy at the open — the paper\'s "stock in play" test, and the '
             'one input that actually predicts the day.</p>')
    if relvol_png:
        h.append(f'<img src="data:image/png;base64,{relvol_png}" alt="relative volume">')

    h.append("<h3>Every name, and where it stopped</h3>")
    h.append('<div class="scroll"><table><tr><th>symbol</th><th>outcome</th>'
             '<th class="n">RelVol</th><th>detail</th></tr>')
    f = funnel.copy()
    f["_o"] = (f["gate"] != "SELECTED").astype(int)
    for _, r in f.sort_values(["_o", "relvol"], ascending=[True, False]).iterrows():
        tag = "sel" if r.gate == "SELECTED" else "rej"
        rv = f"{r.relvol:.2f}×" if pd.notna(r.relvol) else "—"
        h.append(f'<tr><td><b>{r.symbol}</b></td>'
                 f'<td><span class="tag {tag}">{r.gate}</span></td>'
                 f'<td class="n">{rv}</td><td style="color:var(--ink2)">{r.detail}</td></tr>')
    h.append("</table></div>")

    if len(plan):
        h.append("<h2>The book</h2>")
        h.append('<p class="note">Direction is set by the opening candle, not by a view: '
                 'a bullish 09:30–09:35 range permits only a long, a bearish one only a '
                 'short. Entry is a stop order resting at the range extreme, so a name '
                 'that never breaks out is never traded. The stop sits 10% of ATR away, '
                 'and size is set so that being stopped costs 1% of the book — before the '
                 f'{prof.max_leverage:g}× cap scales every position down.</p>')
        h.append('<div class="scroll"><table><tr><th>symbol</th><th>side</th>'
                 '<th class="n">RelVol</th><th class="n">entry</th><th class="n">stop</th>'
                 '<th class="n">risk/sh</th><th class="n">shares</th>'
                 '<th class="n">notional</th><th>outcome</th><th class="n">R</th>'
                 '<th class="n">P&L</th></tr>')
        res = results.set_index("symbol") if len(results) else pd.DataFrame()
        for _, p in plan.iterrows():
            r = res.loc[p.symbol] if p.symbol in res.index else None
            side = "long" if p.side == "buy" else "short"
            out = r.exit_reason if r is not None else "—"
            rr = f"{r.pnl_r:+.2f}" if r is not None and pd.notna(r.pnl_r) else "—"
            pl = f"${r.pnl:+,.2f}" if r is not None and pd.notna(r.pnl) else "—"
            cls = "" if r is None or pd.isna(r.pnl) else ("pos" if r.pnl > 0 else "neg")
            h.append(f'<tr><td><b>{p.symbol}</b></td>'
                     f'<td><span class="tag {side}">{side}</span></td>'
                     f'<td class="n">{p.rel_volume:.2f}×</td>'
                     f'<td class="n">{p.entry:,.2f}</td><td class="n">{p.stop:,.2f}</td>'
                     f'<td class="n">${p.risk_per_share:,.2f}</td>'
                     f'<td class="n">{int(p.shares)}</td>'
                     f'<td class="n">${p.notional:,.0f}</td>'
                     f'<td style="color:var(--ink2)">{out}</td>'
                     f'<td class="n {cls}">{rr}</td><td class="n {cls}">{pl}</td></tr>')
        h.append("</table></div>")

        h.append("<h2>How each one played out</h2>")
        h.append('<p class="note">Shaded band is the opening range. Dashed blue is the '
                 'trigger the stop order rests at; dotted red is the protective stop. '
                 'The dot is the fill, the cross is the exit.</p>')
        for sym, b64 in charts:
            if b64:
                h.append(f'<img src="data:image/png;base64,{b64}" alt="{sym}">')

    h.append('<div class="foot">Generated from Alpaca bars — historical lane, read-only. '
             'Marks may lag the live tape by ~15 minutes while the session is open '
             '(SIP embargo). Paper account: no real money. '
             'Regenerate with <code>python dashboard.py --date '
             f'{session}</code>.</div>')
    return (f"<!doctype html><html><head><meta charset=utf-8>"
            f"<meta name=viewport content='width=device-width,initial-scale=1'>"
            f"<title>ORB {session}</title><style>{CSS}</style></head><body>"
            f'<div class="wrap">{"".join(h)}</div></body></html>')


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default=None)
    ap.add_argument("--capital", type=float, default=10_000.0)
    ap.add_argument("--open", action="store_true")
    ap.add_argument("--universe", default=None)
    a = ap.parse_args()

    sess = pd.Timestamp(a.date).date() if a.date else \
        pd.Timestamp.now(tz="America/New_York").date()
    cfg = ORBConfig()
    prof = RobinhoodProfile(capital=a.capital)
    universe = _universe(a.universe)

    print(f"building dashboard for {sess} ...", flush=True)
    sig = build_signals(universe, sess, cfg)
    plan = build_plan(sig, cfg, prof)
    funnel = build_funnel(sig, plan, cfg, prof, universe)

    # Outcomes: re-simulate the day so entries, stops and exits are exact.
    syms = list(plan["symbol"]) if len(plan) else []
    results, charts = pd.DataFrame(), []
    if syms:
        nxt = (pd.Timestamp(sess) + pd.Timedelta(days=1)).date()
        feats = daily_features(
            _live_bars(syms, str((pd.Timestamp(sess) - pd.Timedelta(days=90)).date()),
                       str(nxt), "1Day"), cfg)
        mins = _live_bars(syms, str(sess), str(nxt), "1Min")
        rows = []
        pl = plan.set_index("symbol")
        for s in syms:
            try:
                m = mins.xs(s, level="symbol")
            except KeyError:
                continue
            o = simulate_symbol(m, feats.xs(s, level="symbol")["atr"], cfg)
            r = o[o.date == sess]
            if r.empty:
                continue
            r = r.iloc[0].copy()
            r["symbol"] = s
            r["rel_volume"] = float(pl.loc[s, "rel_volume"])
            sh = int(pl.loc[s, "shares"])
            r["pnl"] = sh * r.pnl_per_share if pd.notna(r.pnl_per_share) else np.nan
            rows.append(r)
            charts.append((s, trade_chart(s, m, r, pl.loc[s], cfg)))
        results = pd.DataFrame(rows)

    from broker import PaperBroker
    try:
        with PaperBroker() as b:
            acct = b.assert_paper()["account_number"]
    except Exception:
        acct = "unavailable"

    html = render(sess, sig, plan, funnel, results, prof, cfg, charts,
                  funnel_chart(funnel), relvol_chart(funnel), acct)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    out = LOG_DIR / f"orb_dashboard_{sess}.html"
    out.write_text(html)
    print(f"-> {out}  ({len(html)/1024:,.0f} KB)")
    if a.open:
        webbrowser.open(f"file://{out}")


if __name__ == "__main__":
    main()
