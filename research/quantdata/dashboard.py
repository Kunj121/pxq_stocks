"""Live flow dashboard — four streams of option order flow, one HTML page.

    python research/quantdata/dashboard.py                 # today, to out/
    python research/quantdata/dashboard.py --session 2026-09-22
    python research/quantdata/dashboard.py --out /var/www/flow.html

Four streams, and they are four genuinely different populations rather than
four filters on one:

    golden    ISO + SWEEP. The routing fact — the order really did sweep
              multiple exchanges. This is what sweep_alerts.py alerts on.
    iso500    the same screen at a $500k floor, with no delta band and no fund
              exclusion: every ISO sweep of size, whatever it is.
    unusual   the vendor's `isUnusual` badge.
    opening   the vendor's `isOpeningPosition` badge — new risk, not a close.

**"Golden sweep" means two different things and they barely overlap.** The
vendor's `isGoldenSweep` badge is shown alongside as its own count, because on
2026-09-22 it flagged 155 prints of which only **3 were ISO**: 80 were
multi-leg `MULTI_AUTO_COB` and 70 were `AUTO`, ordinary electronic fills. The
badge is a loose size-and-noteworthiness marker that already implies about $1M
of premium; ISO is a statement about how the order routed. This dashboard
treats ISO as the definition and the badge as a column.

Why the counts are pulled per stream rather than sliced from one download: the
unfiltered feed pages out at 20,000 rows, so filtering locally silently
truncates. Every stream here is filtered server-side and is a true count.
"""
from __future__ import annotations

import argparse
import html
import json
import sys
from dataclasses import dataclass
from pathlib import Path

import pandas as pd

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path[:0] = [str(HERE), str(ROOT / "research" / "sweep_events"), str(ROOT)]

import directional as D  # noqa: E402
import feeds as F  # noqa: E402
import sweep_alerts as A  # noqa: E402
from client import QuantDataError  # noqa: E402

DEFAULT_OUT = ROOT / "out" / "flow_dashboard.html"

#: Rows shown per stream table. The point is the shape of the flow, not a full
#: transcript — the parquet beside it holds everything.
TOP_N = 12
BUCKET = "30min"

#: Kept in step with the deployed alerter so the two describe one universe. A
#: dashboard that shows a wider population than the thing alerting on it makes
#: every "why didn't that alert?" question unanswerable.
DTE_MAX = 60.0
MIN_OI = 2

#: What the controls open on. These are the deployed alerter's own settings, so
#: the first view of the page is the population Slack is actually sending.
DEFAULTS = {
    "premium": 1_000_000,
    "delta_min": 0.30,
    "delta_max": 0.70,
    "dte_max": DTE_MAX,
    "min_oi": MIN_OI,
}


@dataclass(frozen=True)
class Stream:
    key: str
    title: str
    blurb: str
    premium_min: float

    def load(self, session: str, golden: str = "iso") -> pd.DataFrame:
        """Pull this stream for one session.

        `golden` picks what a golden sweep means and applies to **`golden`
        alone**. Every other stream stays ISO whatever it says:

        - `iso500` says ISO in its own name. Following the mode made it an
          exact duplicate of `golden` in badge mode, because the badge already
          implies about $1M of premium and its $500k floor then does nothing.
          It exists precisely to be the ISO cut you can compare against.
        - the badge streams are ISO because a badge alone is not a routing fact.
        """
        if self.key == "golden":
            return F.sweeps(session, premium_min=self.premium_min,
                            **A.GOLDEN_MODES[golden])
        if self.key == "iso500":
            return F.sweeps(session, premium_min=self.premium_min)
        return F.flagged(self.key, session, premium_min=self.premium_min)


#: **Every stream is ISO + SWEEP.** A vendor badge on its own says nothing about
#: how an order routed: only 2.9% of `isUnusual` prints and 2.6% of
#: `isOpeningPosition` prints were ISO sweeps on 2026-09-22, the rest being
#: multi-leg `MULTI_*_COB` and `AUTO` electronic fills.
#:
#: The consequence is worth stating plainly: once restricted to ISO, the badge
#: streams are **subsets of `golden`**, not separate populations — all 110
#: ISO-sweep `isUnusual` prints and all 238 `isOpeningPosition` ones that
#: session were already in it. They are kept as their own tabs because that is
#: how they are read, and the same views are reachable from the badge filters.
#:
#: Badge floors now match `golden`: ISO is the binding constraint, so the old
#: $250k and $500k floors left almost nothing behind it.
STREAMS = (
    Stream("golden", "Golden sweeps · ISO",
           "Intermarket sweeps. The order actually swept multiple exchanges. "
           "This is the population sweep_alerts.py watches.", 100_000),
    Stream("iso500", "ISO sweeps over $500k",
           "Every ISO sweep of size. No delta band, no fund exclusion — "
           "the unfiltered view of large sweep routing.", 500_000),
    Stream("unusual", "Unusual · ISO",
           "ISO sweeps the vendor also badges isUnusual. A subset of the "
           "golden stream, not a separate population.", 100_000),
    Stream("opening", "Opening positions · ISO",
           "ISO sweeps the vendor badges isOpeningPosition: new risk being "
           "put on rather than closed. Also a subset of golden.", 100_000),
)


def build_payload(session: str, streams=STREAMS,
                  sessions: list[str] | None = None,
                  defaults: dict | None = None, golden: str = "iso") -> dict:
    """Pull every stream and shape it for the browser.

    Pulled once, filtered many times. Rows are shipped unfiltered beyond the
    server-side pull, because the page can only narrow what it was given — a
    control that appeared to widen past the pull floor would be lying.

    A failing stream becomes an empty one and is reported, never fatal: three
    working panels beat a blank page.
    """
    days = sessions or [session]
    rows: dict[str, list] = {}
    meta = []
    for stream in streams:
        try:
            frames = [stream.load(day, golden=golden) for day in days]
            frames = [f for f in frames if len(f)]
            merged = (pd.concat(frames, ignore_index=True) if frames
                      else pd.DataFrame())
            rows[stream.key] = client_rows(merged)
            note = ""
        except QuantDataError as exc:
            rows[stream.key] = []
            note = f"  FAILED: {str(exc)[:120]}"
        title, blurb = stream.title, stream.blurb
        if stream.key == "golden" and golden != "iso":
            # Calling it ISO when it is the vendor badge would be a lie on the
            # one label a reader trusts most.
            title = ("Golden sweeps · vendor badge" if golden == "badge"
                     else "Golden sweeps · badge + ISO")
            blurb = ("Prints the vendor flags isGoldenSweep. Entirely SWEEP "
                     "consolidation but only ~3% ISO: about half are AUTO "
                     "electronic fills and 43% are multi-leg, whose direction "
                     "describes one leg rather than the position."
                     if golden == "badge" else
                     "Prints carrying the vendor's isGoldenSweep badge that "
                     "also routed as ISO. The strictest reading of both.")
        meta.append({"key": stream.key, "title": title,
                     "blurb": blurb, "premium_min": stream.premium_min})
        print(f"  {stream.key:10s} {len(rows[stream.key]):>5} prints{note}")

    stamps = [r[0] for rs in rows.values() for r in rs if r[0] is not None]
    if stamps:
        lo = pd.Timestamp(min(stamps), unit="ms", tz="UTC").tz_convert(
            "America/New_York").strftime("%Y-%m-%d")
        hi = pd.Timestamp(max(stamps), unit="ms", tz="UTC").tz_convert(
            "America/New_York").strftime("%Y-%m-%d")
    else:
        lo = hi = days[0]

    return {"columns": COLUMNS, "streams": meta, "rows": rows,
            "defaults": defaults or DEFAULTS, "span": [lo, hi],
            "generated": pd.Timestamp.now(tz="America/New_York").strftime(
                "%H:%M:%S")}


# --------------------------------------------------------------------------- #
# client payload
# --------------------------------------------------------------------------- #

#: Columns shipped to the browser, in order. Rows travel as arrays rather than
#: objects: the keys would otherwise repeat on every row and roughly double the
#: payload for no gain.
COLUMNS = ["ts", "ticker", "cp", "strike", "expiry", "dte", "premium", "price",
           "delta", "iv", "size", "volume", "oi", "side", "dir", "fund", "gold",
           "unusual", "opening", "type"]


def esc(value) -> str:
    return html.escape(str(value))


def money(value: float) -> str:
    """Compact dollars. Used for the console summary; the page formats its own."""
    if value is None or pd.isna(value):
        return "—"
    value = float(value)
    for scale, suffix in ((1e9, "B"), (1e6, "M"), (1e3, "k")):
        if abs(value) >= scale:
            return f"${value / scale:,.2f}{suffix}".replace(".00", "")
    return f"${value:,.0f}"


def _cell(value):
    """JSON-safe scalar. NaN is null, so the page can tell absent from zero."""
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    if isinstance(value, (pd.Timestamp,)):
        return int(value.timestamp() * 1000)
    if hasattr(value, "item"):
        value = value.item()
    return value


def client_rows(frame: pd.DataFrame) -> list[list]:
    """One stream as compact arrays, unfiltered beyond what was pulled.

    Everything the page can filter on must be here, because the browser can
    only narrow what it was given — it cannot reach back to the feed. That is
    why funds are shipped rather than dropped server-side: the page offers a
    toggle, and a toggle over absent rows would be a lie.
    """
    if not len(frame):
        return []
    f = A.prepare(frame)
    f = f.assign(fund=D.is_fund(f))
    out = []
    for r in f.itertuples():
        d = A._fields(r)
        out.append([
            _cell(d["ts"]), d["ticker"], d["contractType"][0],
            _cell(d["strikePrice"]), d["expirationDate"], _cell(d.get("dte")),
            _cell(d["premium"]), _cell(d.get("optionPrice")),
            _cell(d.get("absdelta")), _cell(d.get("impliedVolatility")),
            _cell(d.get("size")), _cell(d.get("volume")), _cell(d.get("openInterest")),
            d.get("tradeSideCode"), d.get("direction"),
            bool(d.get("fund")), bool(d.get("isGoldenSweep")),
            bool(d.get("isUnusual")), bool(d.get("isOpeningPosition")),
            d.get("tradeType"),
        ])
    return out


CSS = """
:root{--ground:#F4F6F8;--surface:#FFF;--sunk:#EAEEF2;--ink:#121820;
--slate:#56626F;--hairline:#D3DBE3;--accent:#0C6C6E;--bar:#A9C3C6;
--bull:#14764B;--bear:#A93A2B;--amber:#8A6512;--focus:#0C6C6E;}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){
--ground:#0E1319;--surface:#161D25;--sunk:#1D262F;--ink:#E4EAF0;
--slate:#94A2B1;--hairline:#2B353F;--accent:#4FC0BE;--bar:#3C5257;
--bull:#49BC85;--bear:#E3806E;--amber:#D3A43E;--focus:#4FC0BE;}}
*{box-sizing:border-box;}
body{margin:0;background:var(--ground);color:var(--ink);
font:14px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;
padding-block:26px 64px;padding-left:20px;padding-right:20px;}
.wrap{max-width:1160px;margin:0 auto;display:flex;flex-direction:column;gap:22px;}
h1{font-size:1.5rem;margin:0;letter-spacing:-.02em;}
h2{font-size:1.06rem;margin:0;letter-spacing:-.01em;}
.sub{color:var(--slate);font-size:.84rem;margin:5px 0 0;}
.blurb{color:var(--slate);font-size:.83rem;margin:4px 0 0;max-width:74ch;}
header{border-bottom:1px solid var(--hairline);padding-bottom:18px;}
.note{background:var(--surface);border:1px solid var(--hairline);
border-left:3px solid var(--amber);padding:13px 16px;font-size:.84rem;
color:var(--slate);}
.note strong{color:var(--ink);}
section{background:var(--surface);border:1px solid var(--hairline);
padding:20px 22px;display:flex;flex-direction:column;gap:15px;}
.tabs{display:flex;flex-wrap:wrap;gap:6px;}
.tab{font:inherit;font-size:.84rem;font-weight:600;cursor:pointer;
background:var(--surface);color:var(--slate);border:1px solid var(--hairline);
padding:7px 13px;border-radius:2px;}
.tab[aria-selected="true"]{background:var(--accent);color:#fff;
border-color:var(--accent);}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"])
.tab[aria-selected="true"]{color:#0E1319;}}
.tab .n{opacity:.72;font-weight:500;margin-left:5px;}
.controls{background:var(--surface);border:1px solid var(--hairline);
padding:16px 18px;display:grid;
grid-template-columns:repeat(auto-fit,minmax(138px,1fr));gap:12px 16px;
align-items:end;}
.ctl{display:flex;flex-direction:column;gap:4px;min-width:0;}
.ctl label{font-size:.63rem;text-transform:uppercase;letter-spacing:.1em;
color:var(--slate);font-weight:600;}
.ctl input,.ctl select{font:inherit;font-size:.84rem;padding:6px 8px;
border:1px solid var(--hairline);background:var(--ground);color:var(--ink);
border-radius:2px;min-width:0;width:100%;
font-family:ui-monospace,"SF Mono",Menlo,monospace;}
.ctl input:focus-visible,.ctl select:focus-visible,.tab:focus-visible,
button:focus-visible{outline:2px solid var(--focus);outline-offset:2px;}
.ctl.check{flex-direction:row;align-items:center;gap:7px;}
.ctl.check input{width:auto;}
.ctl.check label{text-transform:none;letter-spacing:0;font-size:.8rem;
font-weight:500;color:var(--ink);}
.ctl.wide{grid-column:1/-1;flex-direction:row;align-items:center;gap:11px;}
.ctl.wide > label{flex:0 0 auto;min-width:106px;}
.chips{display:flex;flex-wrap:wrap;gap:5px;}
.chip{font:inherit;font-size:.73rem;font-weight:600;cursor:pointer;
background:var(--ground);color:var(--slate);border:1px solid var(--hairline);
padding:4px 10px;border-radius:12px;white-space:nowrap;}
.chip:hover{color:var(--ink);}
.chip[aria-pressed="true"]{background:var(--accent);color:#fff;
border-color:var(--accent);}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"])
.chip[aria-pressed="true"]{color:#0E1319;}}
.chip .c{opacity:.7;font-weight:500;margin-left:5px;}
.reset{font:inherit;font-size:.8rem;font-weight:600;cursor:pointer;
background:var(--sunk);color:var(--ink);border:1px solid var(--hairline);
padding:7px 12px;border-radius:2px;white-space:nowrap;}
.reset.primary{background:var(--accent);color:#fff;border-color:var(--accent);}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"])
.reset.primary{color:#0E1319;}}
.ctl.row-end{flex-direction:row;align-items:center;gap:8px;}
.saved{font-size:.73rem;color:var(--slate);}
.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(104px,1fr));
gap:14px 20px;}
.k{font-size:.63rem;text-transform:uppercase;letter-spacing:.1em;
color:var(--slate);margin:0 0 2px;font-weight:600;}
.v{margin:0;font-size:1.16rem;font-weight:600;font-variant-numeric:tabular-nums;
font-family:ui-monospace,"SF Mono",Menlo,monospace;}
.v.bull{color:var(--bull);}.v.bear{color:var(--bear);}.v.muted{color:var(--slate);}
.chart{display:flex;align-items:flex-end;gap:2px;height:76px;
border-bottom:1px solid var(--hairline);}
.bar{flex:1 1 0;min-width:2px;background:var(--bar);border-radius:3px 3px 0 0;}
.bar.peak{background:var(--accent);}
.axis{display:flex;justify-content:space-between;margin:5px 0 0;
font-size:.65rem;color:var(--slate);
font-family:ui-monospace,"SF Mono",Menlo,monospace;}
.peak-note{float:right;text-transform:none;letter-spacing:0;font-weight:500;
color:var(--slate);font-family:ui-monospace,"SF Mono",Menlo,monospace;}
.scroll{overflow-x:auto;border:1px solid var(--hairline);max-height:430px;}
table{border-collapse:collapse;width:100%;font-size:.82rem;}
th,td{padding:6px 11px;text-align:left;border-bottom:1px solid var(--hairline);
white-space:nowrap;}
th{font-size:.61rem;text-transform:uppercase;letter-spacing:.09em;
color:var(--slate);font-weight:600;background:var(--sunk);
position:sticky;top:0;z-index:1;}
th[data-sort]{cursor:pointer;user-select:none;}
th[data-sort]:hover{color:var(--ink);}
th .caret{opacity:.55;font-size:.85em;}
tr:last-child td{border-bottom:0;}
td.n,td.t{font-family:ui-monospace,"SF Mono",Menlo,monospace;
font-variant-numeric:tabular-nums;}
td.sym{font-weight:700;}
.dir{font-weight:600;font-size:.76rem;}
.bull{color:var(--bull);}.bear{color:var(--bear);}.flat{color:var(--slate);}
.band-fresh,.band-investigate{color:var(--amber);font-weight:600;}
.band-new{color:var(--accent);font-weight:600;}
.above{color:var(--amber);font-weight:700;}
.empty{color:var(--slate);font-size:.84rem;margin:0;font-style:italic;}
.label{font-size:.63rem;text-transform:uppercase;letter-spacing:.1em;
color:var(--slate);font-weight:600;margin:0;}
.cols{display:grid;grid-template-columns:minmax(0,0.8fr) minmax(0,2.2fr);gap:18px;}
footer{color:var(--slate);font-size:.78rem;}
@media (max-width:760px){.cols{grid-template-columns:1fr;}
body{padding-block:18px 40px;}}
"""


JS = r"""
const F = window.FLOW;
const COLS = F.columns;
const IX = Object.fromEntries(COLS.map((c, i) => [c, i]));
const $ = (id) => document.getElementById(id);

const fmtMoney = (v) => {
  if (v == null || !isFinite(v)) return "—";
  const a = Math.abs(v);
  if (a >= 1e9) return "$" + (v / 1e9).toFixed(2).replace(/\.00$/, "") + "B";
  if (a >= 1e6) return "$" + (v / 1e6).toFixed(2).replace(/\.00$/, "") + "M";
  if (a >= 1e3) return "$" + (v / 1e3).toFixed(2).replace(/\.00$/, "") + "k";
  return "$" + Math.round(v).toLocaleString();
};
// Eastern everywhere, wherever this is opened: the session, the strikes and the
// Slack alerts are all quoted in it, and a dashboard that silently re-times them
// to the viewer's zone cannot be reconciled against them.
const ET = { timeZone: "America/New_York" };
const fmtTime = (ms) => new Date(ms).toLocaleTimeString("en-GB",
  { ...ET, hour: "2-digit", minute: "2-digit" });
const fmtDate = (ms) => new Date(ms).toLocaleDateString("en-CA", ET);

const SIDE_LABEL = { ABOVE_ASK: "above ask", ASK: "ask", BID: "bid",
                     BELOW_BID: "below bid", MID_MARKET: "mid" };

function badges(r) {
  // The vendor's own labels, shown rather than hidden: every row here is
  // already an ISO sweep, so these are what still distinguishes one from another.
  const b = [];
  if (r[IX.gold]) b.push("gold");
  if (r[IX.unusual]) b.push("unusual");
  if (r[IX.opening]) b.push("opening");
  return b.length ? b.join(" \u00b7 ") : "\u2014";
}

function band(vol, oi) {
  if (oi === 0) return ["new", "new strike"];
  if (vol == null || oi == null || oi <= 0) return ["", "—"];
  const r = vol / oi;
  const label = r < 0.5 ? "routine" : r < 2 ? "notable" : r < 5 ? "fresh" : "investigate";
  return [label, r.toFixed(1) + "x"];
}

// An empty set means "no constraint", not "nothing selected" — so a group with
// no chip pressed lets everything through, which is what an untouched filter
// should do.
const CHIPS = { "f-cp": new Set(), "f-dir": new Set(),
                "f-side": new Set(), "f-type": new Set() };

const CHIP_DEFS = {
  "f-cp": [["C", "calls"], ["P", "puts"]],
  "f-dir": [["BULL", "bullish"], ["BEAR", "bearish"], ["none", "neutral"]],
  "f-side": [["ABOVE_ASK", "above ask"], ["ASK", "at ask"],
             ["BID", "at bid"], ["BELOW_BID", "below bid"],
             ["MID_MARKET", "mid"]],
};

function buildChips() {
  // Trade types come from the data rather than a hard-coded list: the feed
  // carries a dozen and adds more, and a stale list would quietly hide flow.
  const counts = new Map();
  for (const key of Object.keys(F.rows)) {
    for (const r of F.rows[key]) {
      const t = r[IX.type];
      if (t) counts.set(t, (counts.get(t) || 0) + 1);
    }
  }
  CHIP_DEFS["f-type"] = [...counts.entries()]
    .sort((a, b) => b[1] - a[1])
    .map(([t, n]) => [t, t, n]);

  for (const group of Object.keys(CHIP_DEFS)) {
    const host = $(group);
    if (!host) continue;
    host.innerHTML = CHIP_DEFS[group].map((d) => {
      const count = d[2] == null ? "" : '<span class="c">' + d[2].toLocaleString() + "</span>";
      return '<button type="button" class="chip" aria-pressed="false" data-v="' +
        d[0] + '">' + d[1] + count + "</button>";
    }).join("");
    host.querySelectorAll(".chip").forEach((chip) => {
      chip.onclick = () => {
        const v = chip.dataset.v;
        const on = CHIPS[group].has(v);
        if (on) CHIPS[group].delete(v); else CHIPS[group].add(v);
        chip.setAttribute("aria-pressed", String(!on));
        draw();
      };
    });
  }
}

function readControls() {
  return {
    premium: +$("f-premium").value || 0,
    deltaMin: +$("f-dmin").value || 0,
    deltaMax: +$("f-dmax").value || 1,
    dteMax: $("f-dte").value === "" ? Infinity : +$("f-dte").value,
    minOi: +$("f-oi").value || 0,
    minVolOi: +$("f-voi").value || 0,
    dir: CHIPS["f-dir"],
    cp: CHIPS["f-cp"],
    side: CHIPS["f-side"],
    type: CHIPS["f-type"],
    funds: $("f-funds").checked,
    unusual: $("f-unusual").checked,
    opening: $("f-opening").checked,
    ticker: $("f-ticker").value.trim().toUpperCase(),
    from: $("f-from").value,
    to: $("f-to").value,
  };
}

function apply(rows, c) {
  const fromMs = c.from ? Date.parse(c.from + "T00:00:00-04:00") : -Infinity;
  const toMs = c.to ? Date.parse(c.to + "T23:59:59-04:00") : Infinity;
  return rows.filter((r) => {
    if (!c.funds && r[IX.fund]) return false;
    if (c.unusual && !r[IX.unusual]) return false;
    if (c.opening && !r[IX.opening]) return false;
    if (r[IX.premium] < c.premium) return false;
    const d = r[IX.delta];
    if (d == null || d < c.deltaMin || d > c.deltaMax) return false;
    const dte = r[IX.dte];
    if (dte != null && dte > c.dteMax) return false;
    const oi = r[IX.oi];
    // A null open interest is unknown, not zero: it is not failed against a
    // floor it was never measured against.
    if (c.minOi > 0 && !(oi == null || oi >= c.minOi)) return false;
    if (c.minVolOi > 0) {
      const v = r[IX.volume];
      if (oi !== 0 && !(oi > 0 && v != null && v / oi >= c.minVolOi)) return false;
    }
    // "neutral" is the vendor's NEUTRAL: a print with no aggressor, so no
    // direction. It cannot be matched by equality against a null.
    if (c.dir.size && !c.dir.has(r[IX.dir] == null ? "none" : r[IX.dir])) return false;
    if (c.cp.size && !c.cp.has(r[IX.cp])) return false;
    if (c.side.size && !c.side.has(r[IX.side])) return false;
    if (c.type.size && !c.type.has(r[IX.type])) return false;
    if (c.ticker && !r[IX.ticker].includes(c.ticker)) return false;
    const t = r[IX.ts];
    if (t < fromMs || t > toMs) return false;
    return true;
  });
}

function stats(rows, hiddenFunds) {
  const premium = rows.reduce((a, r) => a + (r[IX.premium] || 0), 0);
  const bull = rows.filter((r) => r[IX.dir] === "BULL").length;
  const bear = rows.filter((r) => r[IX.dir] === "BEAR").length;
  const tickers = new Set(rows.map((r) => r[IX.ticker])).size;
  const gold = rows.filter((r) => r[IX.gold]).length;
  const cells = [
    ["Prints", rows.length.toLocaleString(), ""],
    ["Premium", fmtMoney(premium), ""],
    ["Tickers", tickers.toLocaleString(), ""],
    ["Bullish", bull.toLocaleString(), "bull"],
    ["Bearish", bear.toLocaleString(), "bear"],
    ["Golden badge", gold.toLocaleString(), ""],
  ];
  if (hiddenFunds) cells.push(["Funds hidden", hiddenFunds.toLocaleString(), "muted"]);
  return cells.map(([k, v, cls]) =>
    '<div><p class="k">' + k + '</p><p class="v ' + cls + '">' + v + "</p></div>").join("");
}

function chart(rows) {
  if (!rows.length) return '<p class="empty">Nothing matches these filters.</p>';
  const buckets = new Map();
  for (const r of rows) {
    const key = Math.floor(r[IX.ts] / 1800000) * 1800000;
    buckets.set(key, (buckets.get(key) || 0) + (r[IX.premium] || 0));
  }
  const keys = [...buckets.keys()].sort((a, b) => a - b);
  const peak = Math.max(...buckets.values());
  const bars = keys.map((k) => {
    const v = buckets.get(k);
    const h = Math.max(2, (v / peak) * 100);
    const cls = v === peak ? "bar peak" : "bar";
    return '<div class="' + cls + '" style="height:' + h.toFixed(1) + '%" title="' +
      fmtDate(k) + " " + fmtTime(k) + " ET · " + fmtMoney(v) + '"></div>';
  }).join("");
  const peakKey = keys.find((k) => buckets.get(k) === peak);
  return '<p class="label">Premium per 30 minutes<span class="peak-note">peak ' +
    fmtMoney(peak) + " at " + fmtTime(peakKey) + "</span></p>" +
    '<div class="chart" role="img" aria-label="Premium per 30 minutes, peak ' +
    fmtMoney(peak) + '">' + bars + "</div>" +
    '<p class="axis"><span>' + fmtDate(keys[0]) + " " + fmtTime(keys[0]) + "</span>" +
    "<span>" + fmtDate(keys[keys.length - 1]) + " " +
    fmtTime(keys[keys.length - 1]) + " ET</span></p>";
}

function leaders(rows) {
  if (!rows.length) return "";
  const by = new Map();
  for (const r of rows) {
    const t = r[IX.ticker];
    const e = by.get(t) || { premium: 0, n: 0, signed: 0 };
    e.premium += r[IX.premium] || 0;
    e.n += 1;
    e.signed += (r[IX.dir] === "BULL" ? 1 : r[IX.dir] === "BEAR" ? -1 : 0) * (r[IX.premium] || 0);
    by.set(t, e);
  }
  const top = [...by.entries()].sort((a, b) => b[1].premium - a[1].premium).slice(0, 10);
  const body = top.map((entry) => {
    const t = entry[0], e = entry[1];
    const lean = e.signed > 0 ? "bull" : e.signed < 0 ? "bear" : "flat";
    const word = lean === "bull" ? "net bull" : lean === "bear" ? "net bear" : "balanced";
    const share = e.premium ? Math.abs(e.signed) / e.premium : 0;
    return '<tr><td class="sym">' + t + '</td><td class="n">' + fmtMoney(e.premium) +
      '</td><td class="n">' + e.n + '</td><td class="dir ' + lean + '">' + word +
      " " + Math.round(share * 100) + "%</td></tr>";
  }).join("");
  return '<div class="scroll"><table><thead><tr><th>Ticker</th><th>Premium</th>' +
    "<th>Prints</th><th>Lean</th></tr></thead><tbody>" + body + "</tbody></table></div>";
}

let sortKey = "premium", sortDir = -1;
const ROW_CAP = 200;

function prints(rows) {
  if (!rows.length) return '<p class="empty">Nothing matches these filters.</p>';
  const sorted = rows.slice().sort((a, b) => {
    const x = a[IX[sortKey]], y = b[IX[sortKey]];
    if (x == null) return 1;
    if (y == null) return -1;
    return x > y ? sortDir : x < y ? -sortDir : 0;
  }).slice(0, ROW_CAP);
  const body = sorted.map((r) => {
    const b = band(r[IX.volume], r[IX.oi]);
    const dir = r[IX.dir] || "—";
    const dcls = dir === "BULL" ? "bull" : dir === "BEAR" ? "bear" : "flat";
    const side = SIDE_LABEL[r[IX.side]] || r[IX.side] || "—";
    const sideCls = r[IX.side] === "ABOVE_ASK" ? "above" : "";
    const strike = (+r[IX.strike]).toLocaleString(undefined, { maximumFractionDigits: 2 });
    return '<tr><td class="t">' + fmtDate(r[IX.ts]) + '</td><td class="t">' +
      fmtTime(r[IX.ts]) + '</td><td class="sym">' + r[IX.ticker] + "</td><td>" +
      (r[IX.cp] === "C" ? "CALL" : "PUT") + " $" + strike + '</td><td class="t">' +
      r[IX.expiry] + '</td><td class="n">' +
      (r[IX.dte] == null ? "—" : Math.round(r[IX.dte])) + '</td><td class="n">' +
      fmtMoney(r[IX.premium]) + '</td><td class="n">' +
      (r[IX.delta] == null ? "—" : r[IX.delta].toFixed(2)) +
      '</td><td class="n band-' + b[0] + '">' + b[1] + '</td><td class="n ' +
      sideCls + '">' + side + '</td><td class="dir ' + dcls + '">' + dir +
      '</td><td class="n">' + badges(r) + "</td></tr>";
  }).join("");
  const head = [["ts", "Date"], ["ts", "Time"], ["ticker", "Ticker"],
    ["strike", "Contract"], ["expiry", "Expiry"], ["dte", "DTE"],
    ["premium", "Premium"], ["delta", "Δ"], ["volume", "Vol/OI"],
    ["side", "Side"], ["dir", "Dir"], ["unusual", "Badges"]]
    .map((h) => {
      const caret = h[0] === sortKey ?
        '<span class="caret">' + (sortDir < 0 ? "▾" : "▴") + "</span>" : "";
      return '<th data-sort="' + h[0] + '">' + h[1] + " " + caret + "</th>";
    }).join("");
  const note = rows.length > ROW_CAP
    ? '<p class="empty">Showing the top ' + ROW_CAP + " of " +
      rows.length.toLocaleString() + " by this sort. Narrow the filters to see the rest.</p>"
    : "";
  return '<div class="scroll"><table><thead><tr>' + head + "</tr></thead><tbody>" +
    body + "</tbody></table></div>" + note;
}

let active = F.streams[0].key;

// Per-browser, not per-account: this is a convenience, not shared state. It can
// come back empty (private window, cleared data, a different machine) and in
// some contexts the accessor itself throws, so every touch is guarded and the
// page must render correctly with nothing stored.
const STORE = "pxq.flow.view.v1";

function note(text) {
  const el = $("saved-note");
  if (!el) return;
  el.textContent = text;
  setTimeout(() => { if (el.textContent === text) el.textContent = ""; }, 2500);
}

function saveView() {
  try {
    const inputs = {};
    document.querySelectorAll(".controls input, .controls select").forEach((el) => {
      inputs[el.id] = el.type === "checkbox" ? el.checked : el.value;
    });
    const chips = {};
    Object.keys(CHIPS).forEach((g) => { chips[g] = [...CHIPS[g]]; });
    localStorage.setItem(STORE, JSON.stringify(
      { inputs, chips, active, sortKey, sortDir }));
    note("saved to this browser");
  } catch (e) {
    note("could not save — site data may be blocked");
  }
}

function syncChips() {
  Object.keys(CHIPS).forEach((g) => {
    const host = $(g);
    if (host) host.querySelectorAll(".chip").forEach((c) =>
      c.setAttribute("aria-pressed", String(CHIPS[g].has(c.dataset.v))));
  });
}

function loadView() {
  let v;
  try {
    const raw = localStorage.getItem(STORE);
    if (!raw) return false;
    v = JSON.parse(raw);
  } catch (e) {
    return false;
  }
  try {
    Object.entries(v.inputs || {}).forEach(([id, val]) => {
      const el = $(id);
      if (!el) return;          // a control removed since the view was saved
      if (el.type === "checkbox") el.checked = !!val; else el.value = val;
    });
    // A saved date range from an old session would silently show nothing on a
    // later day. If it cannot overlap the data now loaded, drop it and keep
    // the rest of the view rather than presenting an empty page as a result.
    const from = $("f-from"), to = $("f-to");
    if (from && to && (from.value > F.span[1] || to.value < F.span[0])) {
      from.value = F.span[0];
      to.value = F.span[1];
      note("saved dates were outside this data — reset to its range");
    }
    Object.keys(CHIPS).forEach((g) => {
      CHIPS[g] = new Set(((v.chips || {})[g] || []));
    });
    if (v.active && F.rows[v.active]) active = v.active;
    if (v.sortKey && IX[v.sortKey] != null) {
      sortKey = v.sortKey;
      sortDir = v.sortDir === 1 ? 1 : -1;
    }
    syncChips();
    return true;
  } catch (e) {
    return false;
  }
}

function draw() {
  const c = readControls();
  const all = F.rows[active] || [];
  const hidden = c.funds ? 0 : all.filter((r) => r[IX.fund]).length;
  const rows = apply(all, c);

  F.streams.forEach((s) => {
    const n = apply(F.rows[s.key] || [], c).length;
    const tab = $("tab-" + s.key);
    tab.setAttribute("aria-selected", String(s.key === active));
    tab.querySelector(".n").textContent = n.toLocaleString();
  });
  const meta = F.streams.filter((s) => s.key === active)[0];
  $("panel-title").textContent = meta.title;
  $("panel-blurb").textContent = meta.blurb + " Pulled at a " +
    fmtMoney(meta.premium_min) + " floor, so these controls can only narrow that.";
  $("stats").innerHTML = stats(rows, hidden);
  $("chart").innerHTML = chart(rows);
  $("leaders").innerHTML = leaders(rows);
  $("prints").innerHTML = prints(rows);

  document.querySelectorAll("th[data-sort]").forEach((th) => {
    th.onclick = () => {
      const k = th.dataset.sort;
      if (k === sortKey) { sortDir = -sortDir; } else { sortKey = k; sortDir = -1; }
      draw();
    };
  });
}

// Eastern trading hours, used only to decide how hard to poll. Outside them
// the file cannot change, so hammering it is pure waste.
function marketOpen(now) {
  const parts = new Intl.DateTimeFormat("en-US", {
    timeZone: "America/New_York", weekday: "short",
    hour: "2-digit", minute: "2-digit", hour12: false,
  }).formatToParts(now || new Date());
  const get = (t) => parts.find((p) => p.type === t).value;
  if (["Sat", "Sun"].includes(get("weekday"))) return false;
  const hhmm = get("hour") + ":" + get("minute");
  return hhmm >= "09:30" && hhmm <= "16:00";
}

let lastStamp = null;

function freshen() {
  // Re-fetch the data only, never the page: reloading would throw away the
  // filters, the sort and the scroll position, which is the entire reason this
  // is not a meta refresh. Silent on failure — the last good data stays on
  // screen rather than the page emptying because one poll missed.
  //
  // No cache-buster and no `no-store`: the server answers If-Modified-Since
  // with a 304 and zero bytes, and a query string would defeat that. The file
  // changes every two minutes at most, so most polls should cost nothing.
  fetch(F.dataUrl, { cache: "no-cache" })
    .then((r) => (r.status === 304 ? null : r.ok ? r.json() : null))
    .then((fresh) => {
      if (!fresh || !fresh.rows) return;
      // A 200 can still carry identical data. Re-rendering 200 table rows to
      // show the same numbers is work nobody asked for.
      if (fresh.generated === lastStamp) return;
      lastStamp = fresh.generated;
      F.rows = fresh.rows;
      F.streams = fresh.streams;
      buildChips();
      // Rebuilding the buttons clears their pressed state, so put back what the
      // viewer had selected rather than silently widening their filter.
      syncChips();
      const el = $("updated");
      if (el) el.textContent = "updated " + fresh.generated + " ET";
      draw();
    })
    .catch(() => {});
}

function boot() {
  buildChips();
  const restored = loadView();
  F.streams.forEach((s) => {
    $("tab-" + s.key).onclick = () => { active = s.key; draw(); };
  });
  document.querySelectorAll(".controls input, .controls select")
    .forEach((el) => el.addEventListener("input", draw));
  $("save").onclick = saveView;
  $("reset").onclick = () => {
    try { localStorage.removeItem(STORE); } catch (e) { /* nothing to clear */ }
    document.querySelectorAll(".controls input, .controls select").forEach((el) => {
      if (el.type === "checkbox") { el.checked = el.defaultChecked; }
      else if (el.tagName === "SELECT") { el.selectedIndex = 0; }
      else { el.value = el.getAttribute("value") || ""; }
    });
    Object.keys(CHIPS).forEach((g) => {
      CHIPS[g].clear();
      const host = $(g);
      if (host) host.querySelectorAll(".chip")
        .forEach((c) => c.setAttribute("aria-pressed", "false"));
    });
    note("reset to the alerter's settings");
    draw();
  };
  if (restored) note("restored your saved view");
  draw();
  // No sidecar means this is a published snapshot, so there is nothing to poll.
  if (F.dataUrl) {
    lastStamp = F.generated;
    // One cadence while the tape is live, a lazy one otherwise. Overnight the
    // file is frozen, so a minute-by-minute poll would spend hours asking a
    // question whose answer cannot change. Never zero, so the page still picks
    // up the closing rebuild and recovers on its own the next morning.
    let timer = null, wasOpen = null;
    const schedule = () => {
      const open = marketOpen();
      if (open === wasOpen) return;
      wasOpen = open;
      if (timer) clearInterval(timer);
      timer = setInterval(freshen, open ? 60000 : 600000);
    };
    schedule();
    setInterval(schedule, 60000);
  }
}
boot();
"""


def controls_html(defaults: dict, span: tuple[str, str]) -> str:
    """The filter row. Defaults are the deployed alerter's own settings, so the
    page opens showing exactly what Slack is showing you, and every control is
    a deviation from that rather than an unrelated starting point."""
    lo, hi = span
    parts = [
        '<div class="controls">',
        _num("f-premium", "Premium at least", defaults["premium"], step=50000),
        _num("f-dmin", "Delta from", defaults["delta_min"], step=0.05, hi=1),
        _num("f-dmax", "Delta to", defaults["delta_max"], step=0.05, hi=1),
        _num("f-dte", "Max days to expiry", defaults["dte_max"], step=1),
        _num("f-oi", "Min open interest", defaults["min_oi"], step=1),
        _num("f-voi", "Min vol / OI", 0, step=0.5),
        _chips("f-cp", "Calls or puts", wide=True),
        # The vendor's sentimentType agrees with this on 100% of prints
        # (BULLISH/BEARISH/NEUTRAL against BULL/BEAR/none), so it is one
        # control, not two. Labelled for both names so it is findable.
        _chips("f-dir", "Sentiment", wide=True),
        _chips("f-side", "Execution side", wide=True),
        _chips("f-type", "Trade type", wide=True),
        '<div class="ctl"><label for="f-ticker">Ticker</label>'
        '<input id="f-ticker" type="text" placeholder="any" autocomplete="off"></div>',
        _date("f-from", "Signal from", lo, lo, hi),
        _date("f-to", "Signal to", hi, lo, hi),
        '<div class="ctl check"><input id="f-unusual" type="checkbox">'
        '<label for="f-unusual">Unusual only</label></div>',
        '<div class="ctl check"><input id="f-opening" type="checkbox">'
        '<label for="f-opening">Opening only</label></div>',
        '<div class="ctl check"><input id="f-funds" type="checkbox">'
        '<label for="f-funds">Include ETFs</label></div>',
        '<div class="ctl row-end">'
        '<button id="save" class="reset primary" type="button">Save view</button>'
        '<button id="reset" class="reset" type="button">Reset</button>'
        '<span class="saved" id="saved-note" role="status"></span></div>',
        "</div>",
    ]
    return "".join(parts)


def _num(ident, label, value, step=1, lo=0, hi=None):
    top = f' max="{hi}"' if hi is not None else ""
    shown = f"{value:g}" if isinstance(value, float) else value
    return (f'<div class="ctl"><label for="{ident}">{esc(label)}</label>'
            f'<input id="{ident}" type="number" step="{step}" min="{lo}"{top} '
            f'value="{shown}"></div>')


def _chips(ident, label, wide=False):
    """An empty chip group. The buttons are built by the page from the data it
    actually holds, so the trade-type list can never drift from the feed."""
    cls = "ctl wide" if wide else "ctl"
    return (f'<div class="{cls}"><label id="{ident}-label">{esc(label)}</label>'
            f'<div class="chips" id="{ident}" role="group" '
            f'aria-labelledby="{ident}-label"></div></div>')


def _sel(ident, label, options, selected):
    opts = "".join(
        f'<option value="{esc(v)}"{" selected" if v == selected else ""}>'
        f"{esc(t)}</option>" for v, t in options)
    return (f'<div class="ctl"><label for="{ident}">{esc(label)}</label>'
            f'<select id="{ident}">{opts}</select></div>')


def _date(ident, label, value, lo, hi):
    return (f'<div class="ctl"><label for="{ident}">{esc(label)}</label>'
            f'<input id="{ident}" type="date" value="{esc(value)}" '
            f'min="{esc(lo)}" max="{esc(hi)}"></div>')


def render(session: str, payload: dict, fragment: bool = False,
           data_url: str | None = None, golden: str = "iso") -> str:
    """The page. All filtering happens in the browser, from embedded rows.

    The server pulls once per stream and ships the rows; every control narrows
    that set client-side. The browser can only ever narrow what it was given,
    so the pull floors stay permissive and each panel says what its own floor
    was — a control that appears to widen past it would be lying.
    """
    now = pd.Timestamp.now(tz="America/New_York")
    payload = dict(payload, dataUrl=data_url)

    if golden == "iso":
        golden_note = (
            "<strong>Golden sweep here means ISO.</strong> The routing fact "
            "that the order really did sweep multiple exchanges. The vendor's "
            "own <code>isGoldenSweep</code> badge is a different population "
            "and is shown only as a column: on 2026-09-22 it flagged 155 "
            "prints of which just 3 were ISO.")
    else:
        golden_note = (
            "<strong>Golden sweep here means the vendor's "
            "<code>isGoldenSweep</code> badge</strong>, not ISO routing. That "
            "population is entirely SWEEP consolidation but only ~3% ISO: 52% "
            "is <code>AUTO</code>, ordinary electronic fills, and 43% is "
            "multi-leg <code>MULTI_*_COB</code>. <strong>Direction is weaker "
            "here</strong> &mdash; on a multi-leg print the aggressor side "
            "describes one leg, not the position, and it still gets labelled "
            "BULL or BEAR. The other three tabs remain ISO.")
    # `</script>` inside embedded JSON ends the script element early, whatever
    # the JSON says — the HTML parser never sees the string as a string. The
    # feed is a vendor rather than an attacker, but one odd ticker should not
    # be able to take the page apart. Escaping `<` is the standard defence and
    # stays valid JSON, since \u003c decodes back to the same character.
    data = json.dumps(payload, separators=(",", ":")).replace("<", "\\u003c")
    tabs = "".join(
        f'<button class="tab" id="tab-{esc(s["key"])}" role="tab" '
        f'aria-selected="false" type="button">{esc(s["title"])}'
        f'<span class="n"></span></button>' for s in payload["streams"])

    body = f"""<div class="wrap">
<header>
  <h1>Option flow &middot; {esc(session)}</h1>
  <p class="sub"><span id="updated">updated {now:%H:%M:%S} ET</span> &middot;
  {"refreshes every minute" if data_url else "a snapshot, it does not refresh"}
  &middot; filters run in your browser</p>
</header>
<div class="note">{golden_note}<br><br>
<strong>The controls open on the deployed alerter's own settings</strong>, so
what you see first is what Slack is sending you. Anything you change from here
is a deviation from that, not a different starting point.</div>
<div class="tabs" role="tablist">{tabs}</div>
{controls_html(payload["defaults"], payload["span"])}
<section role="tabpanel">
  <div><h2 id="panel-title"></h2><p class="blurb" id="panel-blurb"></p></div>
  <div class="stats" id="stats"></div>
  <div id="chart"></div>
  <div class="cols">
    <div><p class="label">Most premium</p><div id="leaders"></div></div>
    <div><p class="label">Prints &middot; click a heading to sort</p>
      <div id="prints"></div></div>
  </div>
</section>
<footer>Source <code>research/quantdata/dashboard.py</code>. Counts are
server-side filtered at the pull, so none are truncated by the feed's
20,000-row paging cap. Times are Eastern wherever you open this. Side labels are
estimates: a print tagged at the ask may be one leg of a spread. Mid-market
prints have no aggressor and are counted in neither direction.</footer>
</div>
<script>window.FLOW={data};</script>
<script>{JS}</script>"""

    if fragment:
        return f"<title>Option flow</title>\n<style>{CSS}</style>\n{body}"
    return (f'<!doctype html>\n<html lang="en"><head><meta charset="utf-8">\n'
            f'<meta name="viewport" content="width=device-width,initial-scale=1">\n'
            f'<title>Flow &middot; {esc(session)}</title><style>{CSS}</style>'
            f"</head><body>\n{body}\n</body></html>")


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--session", help="YYYY-MM-DD (default: today, ET)")
    p.add_argument("--start", metavar="YYYY-MM-DD",
                   help="first session of a range; use with --end")
    p.add_argument("--end", metavar="YYYY-MM-DD",
                   help="last session of a range; use with --start")
    p.add_argument("--out", default=str(DEFAULT_OUT), help="output HTML path")
    p.add_argument("--fragment", action="store_true",
                   help="omit the document wrapper, for publishing as an Artifact")
    p.add_argument("--golden", choices=sorted(A.GOLDEN_MODES), default="iso",
                   help="what counts as a golden sweep for the sweep streams")
    args = p.parse_args()

    if bool(args.start) != bool(args.end):
        p.error("--start and --end go together")
    sessions = F.sessions_between(args.start, args.end) if args.start else None
    session = (f"{args.start} to {args.end}" if sessions
               else args.session or A.session_today())
    print(f"building {session}")

    try:
        payload = build_payload(args.session or A.session_today(),
                                sessions=sessions, golden=args.golden)
    except QuantDataError as exc:
        print(f"FATAL: {exc}", file=sys.stderr)
        return 1

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)

    # The page polls this sidecar rather than reloading itself. A published
    # fragment has no server beside it, so it gets no sidecar and stays static.
    data_url = None
    if not args.fragment:
        sidecar = out.with_suffix(".json")
        sidecar.write_text(json.dumps(payload, separators=(",", ":")))
        data_url = sidecar.name
    out.write_text(render(session, payload, fragment=args.fragment,
                          data_url=data_url, golden=args.golden))
    total = sum(len(v) for v in payload["rows"].values())
    print(f"wrote {out} ({out.stat().st_size / 1024:.0f} KB, "
          f"{total:,} rows filterable in the browser)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
