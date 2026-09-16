"""Daily 5-minute ORB runner — Alpaca **paper**, with Robinhood's limits imposed.

    python daily_orb.py screen           # pre-open: eligible names for today
    python daily_orb.py enter            # 09:35 ET: rank, size, place (dry-run)
    python daily_orb.py enter --place    # ...actually submit the orders
    python daily_orb.py status
    python daily_orb.py flatten --place  # 15:55 ET: close everything
    python daily_orb.py report           # modelled vs actual fills

Why paper on Alpaca but sized like Robinhood
--------------------------------------------
The Alpaca paper account has $94k and 4x margin. Trading it as-is would prove
nothing about what this strategy does in the account that would actually run it.
So every Robinhood constraint is imposed on top:

    capital           $25,000, not the paper account's balance. Matches the
                      paper's starting AUM, and wastes less on whole-share
                      quantization than $10k did (12.4% -> 13.3% IRR).
    leverage          1x. The replication found 1x has the BETTER Sharpe
                      (2.48 vs 1.96) - leverage bought return, not edge.
    commission        $0
    shares            whole only. Robinhood fractional is market-orders-only in
                      regular hours; a stop-entry cannot be fractional.
    exit              market orders near the close. Robinhood has no
                      market-on-close, so `cls` is deliberately NOT used even
                      though Alpaca offers it.
    session           regular hours, day orders only.

What this can and cannot measure
--------------------------------
It CAN prove the plumbing: that 20 bracket stop orders land inside the 09:35
window (today's median entry was 1 minute after the range closed), that RelVol
computed live matches the backtest, that the flatten fires.

It CANNOT measure slippage. Alpaca's paper engine fills against the quote. The
number that decides this strategy is 56,671 shares/year x slippage x 2 sides
against a best-case edge near $1,900/yr - and only real money measures it.
Treat every P&L here as an upper bound.
"""

from __future__ import annotations

import argparse
import logging
import sys
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
STRAT = HERE.parent
ROOT = STRAT.parent.parent
sys.path[:0] = [str(HERE), str(STRAT), str(ROOT)]

from backtest.data import load_bars
from broker import BrokerError, PaperBroker
from orb import ORBConfig, daily_features

LOG_DIR = ROOT / "execution" / "logs"
FILL_LOG = LOG_DIR / "orb_paper_fills.csv"
PLAN_DIR = LOG_DIR / "orb_plans"

logging.basicConfig(level=logging.INFO, format="%(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("backtest").setLevel(logging.WARNING)
log = logging.getLogger("orb.daily")


@dataclass(frozen=True)
class RobinhoodProfile:
    """The constraints of the account this is a proxy for."""
    capital: float = 25_000.0
    max_leverage: float = 1.0
    commission_per_share: float = 0.0
    whole_shares_only: bool = True
    allow_short: bool = True
    use_market_on_close: bool = False    # Robinhood has no MOC
    max_positions: int = 20

    def describe(self) -> str:
        return (f"${self.capital:,.0f} | {self.max_leverage:g}x | "
                f"${self.commission_per_share:.4f}/sh | "
                f"{'short OK' if self.allow_short else 'long only'} | "
                f"{'MOC' if self.use_market_on_close else 'market-at-close'}")


# --------------------------------------------------------------------------- #
# signal
# --------------------------------------------------------------------------- #

def opening_ranges(minutes: pd.DataFrame, n: int = 5) -> pd.DataFrame:
    """Per (date, symbol) opening-range OHLCV from a 1-minute panel."""
    idx = minutes.index.get_level_values("timestamp")
    mod = idx.hour * 60 + idx.minute
    m = minutes[(mod >= 570) & (mod < 570 + n)].copy()
    m["date"] = m.index.get_level_values("timestamp").date
    g = m.groupby(["date", m.index.get_level_values("symbol")])
    m["_mod"] = mod[(mod >= 570) & (mod < 570 + n)]
    out = pd.DataFrame({
        "or_open": g["open"].first(), "or_high": g["high"].max(),
        "or_low": g["low"].min(), "or_close": g["close"].last(),
        "or_volume": g["volume"].sum(), "bars": g["close"].size(),
        "has_open_bar": g["_mod"].min().eq(570),
    })
    out.index.names = ["date", "symbol"]
    return out


def _live_bars(symbols: list[str], start: str, end: str, timeframe: str) -> pd.DataFrame:
    """Fetch straight from the API, bypassing the on-disk cache.

    The cache stores each symbol's full history in one file. Reading 100 of
    those to recover three weeks of bars means decompressing gigabytes — fine
    for research, hopeless for a job that must decide inside the 09:35 window.
    Today's opening range is ~5 bars per name: one request, not a disk scan.
    """
    from backtest.client import AlpacaClient
    rows = []
    with AlpacaClient() as c:
        for i in range(0, len(symbols), 100):
            data = c.stock_bars(symbols[i:i + 100], timeframe, start, end,
                                adjustment="raw")
            for sym, bars in data.items():
                for b in bars:
                    rows.append({"timestamp": b["t"], "symbol": sym,
                                 "open": b["o"], "high": b["h"], "low": b["l"],
                                 "close": b["c"], "volume": b["v"]})
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows)
    df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True).dt.tz_convert(
        "America/New_York")
    return df.set_index(["timestamp", "symbol"]).sort_index()


OR_HISTORY = LOG_DIR / "orb_or_history.csv"


def load_or_history() -> pd.DataFrame:
    if not OR_HISTORY.exists():
        return pd.DataFrame(columns=["date", "symbol", "or_volume"])
    h = pd.read_csv(OR_HISTORY)
    h["date"] = pd.to_datetime(h["date"]).dt.date
    return h


def append_or_history(today: pd.DataFrame) -> None:
    """Record today's opening ranges so tomorrow's RelVol needs no backfill."""
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    cols = ["date", "symbol", "or_open", "or_high", "or_low", "or_close",
            "or_volume"]
    row = today.reset_index()[cols]
    existing = load_or_history()
    if len(existing):
        key = set(zip(existing["date"], existing["symbol"]))
        row = row[[k not in key for k in zip(row["date"], row["symbol"])]]
    if len(row):
        row.to_csv(OR_HISTORY, mode="a", header=not OR_HISTORY.exists(),
                   index=False)


def wait_for_opening_range(symbols: list[str], session: date, cfg: ORBConfig,
                           timeout_s: float = 75.0, poll_s: float = 4.0) -> None:
    """Block until the opening-range bars are actually queryable.

    Timing is not a detail here. Measured over 29,755 trades: 44% of breakouts
    trigger within 60 seconds of the range closing, 55% within two minutes, and
    those early trades carry 73.5% of all profit at +0.279R against +0.123R for
    the rest. A stop order that arrives after price has crossed the trigger is
    immediately marketable and fills at market — not at the trigger the backtest
    assumes.

    So the runner fires AT 09:35 and waits for the data rather than padding the
    schedule with dead minutes. The last opening-range bar (09:34) closes at
    09:35:00 and is usually queryable within seconds.
    """
    import time
    or_end_min = 570 + cfg.opening_minutes
    deadline = time.time() + timeout_s
    probe = symbols[:8]
    while time.time() < deadline:
        now_et = pd.Timestamp.now(tz="America/New_York")
        if now_et.date() != session:
            return
        if now_et.hour * 60 + now_et.minute < or_end_min:
            time.sleep(poll_s)
            continue
        try:
            nxt = (pd.Timestamp(session) + pd.Timedelta(days=1)).date()
            bars = _live_bars(probe, str(session), str(nxt), "1Min")
            if not bars.empty:
                idx = bars.index.get_level_values("timestamp")
                mod = idx.hour * 60 + idx.minute
                have = bars[(mod >= 570) & (mod < or_end_min)]
                per_sym = have.groupby(level="symbol").size()
                if len(per_sym) and per_sym.min() >= cfg.opening_minutes:
                    log.info("opening range complete at %s", now_et.strftime("%H:%M:%S"))
                    return
        except Exception:
            pass
        time.sleep(poll_s)
    log.warning("opening-range bars still incomplete after %.0fs — proceeding",
                timeout_s)


def build_signals(symbols: list[str], session: date, cfg: ORBConfig) -> pd.DataFrame:
    """Everything needed to decide today's book, as known at 09:35.

    Two small live requests, not a cache scan: ~30 daily bars per name for the
    ATR and volume filters, and today's first five minutes for the opening
    range. The RelVol denominator comes from the rolling history file.
    """
    daily_start = (pd.Timestamp(session) - pd.Timedelta(days=90)).date()
    nxt = (pd.Timestamp(session) + pd.Timedelta(days=1)).date()

    daily_raw = _live_bars(symbols, str(daily_start), str(nxt), "1Day")
    if daily_raw.empty:
        raise SystemExit("no daily bars returned")
    feats = daily_features(daily_raw, cfg)

    minutes = _live_bars(symbols, str(session), str(nxt), "1Min")
    if minutes.empty:
        raise SystemExit(f"no minute bars for {session} — is it a trading day?")
    today = opening_ranges(minutes, cfg.opening_minutes)
    today = today.xs(session, level="date") if session in \
        today.index.get_level_values("date") else today.iloc[:0]
    if today.empty:
        raise SystemExit(f"no opening range for {session}")

    hist = load_or_history()
    hist = hist[hist["date"] < session]
    if len(hist):
        # Each symbol's own previous `lookback` opening ranges — NOT the last
        # `lookback` calendar dates. A name that missed a session still has 14
        # observations of its own; requiring presence on 14 shared dates would
        # silently NaN it out and drop it from the ranking, which is exactly how
        # a live runner drifts away from the backtest it was validated against.
        ordered = hist.sort_values(["symbol", "date"])
        tail = ordered.groupby("symbol").tail(cfg.lookback)
        counts = tail.groupby("symbol")["or_volume"].size()
        means = tail.groupby("symbol")["or_volume"].mean()
        avg = means.where(counts >= cfg.lookback)
    else:
        avg = pd.Series(dtype=float)
    today["or_volume_avg"] = today.index.map(avg)
    today["rel_volume"] = today["or_volume"] / today["or_volume_avg"]

    if session in feats.index.get_level_values("date"):
        f = feats.xs(session, level="date")
    else:
        raise SystemExit(f"no daily features for {session}")
    today["atr"] = today.index.map(f["atr"])
    today["avg_volume"] = today.index.map(f["avg_volume"])
    today["day_open"] = today["or_open"]

    # Forward-looking volatility: recorded every session so a panel accumulates.
    # Without history there is no way to test whether IV makes a better stop than
    # the trailing range. Failure here must never affect the ATR book.
    try:
        from iv import atm_iv, implied_daily_move, log_iv
        ivf = atm_iv(list(today.index))
        today["iv"] = today.index.map(ivf["iv"])
        today["iv_move"] = implied_daily_move(today["or_close"], today["iv"])
        log_iv(session, ivf)
    except Exception as exc:
        log.warning("IV snapshot unavailable (%s) — ATR book unaffected", exc)
        today["iv"] = np.nan
        today["iv_move"] = np.nan

    # Require the range to actually start at the bell, matching the backtest.
    # Demanding all `opening_minutes` bars be present is stricter than the study
    # and quietly drops names that simply had no print in one minute.
    today = today[today["has_open_bar"] & (today["bars"] >= 1)]
    today["date"] = session
    return today.reset_index()


def build_plan(sig: pd.DataFrame, cfg: ORBConfig, prof: RobinhoodProfile,
               stop_source: str = "atr") -> pd.DataFrame:
    """Filter, rank, choose direction, size. No orders placed.

    `stop_source` selects what the protective stop is measured from:

      "atr"  10% of ATR(14) — the paper's rule, a TRAILING realised range.
      "iv"   10% of the IV-implied daily move, RESCALED so the median stop width
             matches the ATR book. Without that rescale, switching to IV would
             also make every stop ~24% tighter (IV-move is a 1-sigma
             close-to-close move, ATR a high-low range), and the test would
             measure "tighter stops" rather than "better volatility estimate".
             Rows lacking an IV fall back to ATR.
    """
    s = sig.copy()
    s["direction"] = np.where(s.or_close > s.or_open, 1,
                              np.where(s.or_close < s.or_open, -1, 0))
    m = (s.day_open.gt(cfg.min_price) & s.avg_volume.ge(cfg.min_avg_volume)
         & s.atr.gt(cfg.min_atr) & s.rel_volume.ge(cfg.min_rel_volume)
         & s.direction.ne(0))
    if not prof.allow_short:
        m &= s.direction.eq(1)
    s = s[m].sort_values("rel_volume", ascending=False).head(prof.max_positions).copy()
    if s.empty:
        return s

    if stop_source == "iv" and "iv_move" in s.columns and s["iv_move"].notna().any():
        from iv import iv_stop_scale
        scale = iv_stop_scale(s["iv_move"], s["atr"])
        basis = (s["iv_move"] * scale).where(s["iv_move"].notna(), s["atr"])
        s["stop_basis"], s["iv_scale"] = basis, scale
    else:
        s["stop_basis"], s["iv_scale"] = s["atr"], np.nan
    s["risk_per_share"] = cfg.stop_distance(s["stop_basis"])
    s["entry"] = np.where(s.direction == 1, s.or_high, s.or_low)
    s["stop"] = s.entry - s.direction * s.risk_per_share
    s["shares"] = np.floor(cfg.risk_per_trade * prof.capital / s.risk_per_share)

    gross = (s.shares * s.entry).sum()
    cap = prof.max_leverage * prof.capital
    s["leverage_scale"] = 1.0
    if gross > cap and gross > 0:
        s["leverage_scale"] = cap / gross
        s["shares"] = np.floor(s.shares * s.leverage_scale)
    if prof.whole_shares_only:
        s["shares"] = s.shares.astype(int)
    s = s[s.shares > 0]
    s["side"] = np.where(s.direction == 1, "buy", "sell")
    s["notional"] = s.shares * s.entry
    return s


# --------------------------------------------------------------------------- #
# commands
# --------------------------------------------------------------------------- #

DEFAULT_UNIVERSE = HERE / "universe_live.csv"


#: The live stop is the paper's fixed 10% of ATR — deliberately untuned.
#:
#: A width sweep on 2016-2023 favoured a flexible 8-10% band, which beat the
#: paper by $643/yr net of slippage. Out of sample (30 rule-selected names,
#: 2025-03..2026-09, 2,446 trades) the same band beat the paper by $65/yr and
#: ranked 4th of 5 widths. An improvement that shrinks by 90% out of sample is
#: overfitting, not an edge, so the tuning is reverted.
#:
#: Note what the data actually keeps saying: 5% of ATR won BOTH samples, by a
#: wide margin and with the lowest drawdown. It is rejected on a mechanism the
#: backtest cannot see — a 5% stop is a median 9 cents, at or inside the spread
#: on many of these names, and stop-outs are decided from 1-minute bar lows that
#: do not capture bid-ask bounce. That objection is untestable without real
#: fills, so this is a judgement call overriding two samples of evidence, and it
#: is recorded as such rather than buried.
LIVE_STOP = dict(stop_atr_frac=0.10)


def live_config(**kw) -> ORBConfig:
    return ORBConfig(**{**LIVE_STOP, **kw})


def _universe(path: str | None = None) -> list[str]:
    """Symbols to trade.

    Defaults to `universe_live.csv` (Mag 7 + index/sector ETFs), NOT the
    study's `universe_selected.csv`. The latter was seeded from the tickers the
    paper itself names, which is fine for replicating a 2016-2023 result and
    wrong for deciding what to trade tomorrow.
    """
    f = Path(path) if path else DEFAULT_UNIVERSE
    if not f.exists():
        raise SystemExit(f"universe file not found: {f}")
    return pd.read_csv(f)["symbol"].tolist()


# Windows each phase may act in, Eastern time. These live here rather than in a
# shell wrapper because macOS TCC blocks cron from reading anything under
# ~/Downloads — scheduling is done by launchd calling this file directly, so a
# bash guard would never run. See executable/momentum_orb.md.
PHASE_WINDOWS = {"enter": (935, 945), "flatten": (1545, 1600)}


def guard_window(phase: str) -> bool:
    """True if now is inside `phase`'s Eastern-time window, on a weekday."""
    now = pd.Timestamp.now(tz="America/New_York")
    if now.weekday() > 4:
        log.info("%s: weekend — standing down", phase)
        return False
    lo, hi = PHASE_WINDOWS.get(phase, (0, 2400))
    hhmm = now.hour * 100 + now.minute
    if not (lo <= hhmm <= hi):
        log.info("%s: %s ET is outside %04d-%04d — standing down",
                 phase, now.strftime("%H:%M"), lo, hi)
        return False
    return True


def _session(args) -> date:
    return pd.Timestamp(args.date).date() if args.date else pd.Timestamp.now(
        tz="America/New_York").date()


def cmd_build_history(args) -> None:
    """One-time backfill of the opening-range history from cached minute bars.

    Slow by design — it reads the full-history cache once so that every later
    run needs only one small live request.
    """
    cfg = live_config()
    syms = _universe(args.universe)
    end = _session(args)
    start = (pd.Timestamp(end) - pd.Timedelta(days=args.days)).date()
    frames = []
    for i in range(0, len(syms), 25):
        chunk = syms[i:i + 25]
        mn = load_bars(chunk, str(start), str(end), "1Min",
                       session="regular", adjustment="raw").frame
        if not mn.empty:
            frames.append(opening_ranges(mn, cfg.opening_minutes))
        print(f"  [{min(i+25, len(syms)):3d}/{len(syms)}]", flush=True)
    if not frames:
        print("nothing to backfill")
        return
    allor = pd.concat(frames).sort_index()
    append_or_history(allor)
    h = load_or_history()
    print(f"history now {len(h):,} rows, "
          f"{h['date'].min()} -> {h['date'].max()}, "
          f"{h['symbol'].nunique()} symbols -> {OR_HISTORY}")


def cmd_screen(args) -> None:
    cfg, prof = live_config(), RobinhoodProfile(capital=args.capital)
    sess = _session(args)
    sig = build_signals(_universe(args.universe), sess, cfg)
    ok = sig[(sig.day_open > cfg.min_price) & (sig.avg_volume >= cfg.min_avg_volume)
             & (sig.atr > cfg.min_atr)]
    print(f"session {sess} | universe {len(sig)} with an opening range")
    print(f"pass price/liquidity/ATR filters: {len(ok)}")
    print(f"  of those, RelVol >= {cfg.min_rel_volume:g}: "
          f"{int((ok.rel_volume >= cfg.min_rel_volume).sum())}")


def cmd_enter(args) -> None:
    if args.place and not guard_window("enter"):
        return
    cfg = live_config()
    prof = RobinhoodProfile(capital=args.capital, allow_short=not args.long_only)
    sess = _session(args)
    log.info("profile: %s", prof.describe())

    with PaperBroker() as b:
        acct = b.assert_paper()
        log.info("paper account %s (equity $%s — NOT the sizing basis)",
                 acct["account_number"], acct["equity"])

        universe = _universe(args.universe)
        if args.place and sess == pd.Timestamp.now(tz="America/New_York").date():
            wait_for_opening_range(universe, sess, cfg)
        sig = build_signals(universe, sess, cfg)
        plan = build_plan(sig, cfg, prof)
        if plan.empty:
            print("no qualifying names today")
            return

        cols = ["symbol", "side", "rel_volume", "entry", "stop", "risk_per_share",
                "shares", "notional"]
        print(f"\n=== plan for {sess} — {len(plan)} positions ===")
        print(plan[cols].round(3).to_string(index=False))
        gross = plan.notional.sum()
        print(f"\ngross ${gross:,.0f} on ${prof.capital:,.0f} "
              f"= {gross/prof.capital:.2f}x (cap {prof.max_leverage:g}x)")
        print(f"risk if every stop hits: ${(plan.shares*plan.risk_per_share).sum():,.2f}")

        PLAN_DIR.mkdir(parents=True, exist_ok=True)
        plan.to_csv(PLAN_DIR / f"plan_{sess}.csv", index=False)
        append_or_history(sig.set_index(["date", "symbol"]))

        # Shadow book: identical signals, stop sized from implied volatility
        # instead of trailing ATR. Deliberately NOT placed — two books cannot
        # hold conflicting stops on the same symbol in one account without
        # netting into a single position and corrupting the live book. An ORB
        # outcome is fully determined by entry/stop/exit prices, so simulating
        # the shadow from the same bars is exact, not an approximation.
        shadow = build_plan(sig, cfg, prof, stop_source="iv")
        if len(shadow):
            shadow.to_csv(PLAN_DIR / f"shadow_iv_{sess}.csv", index=False)
            sc = shadow["iv_scale"].dropna()
            print(f"\nshadow book (IV-sized stops): {len(shadow)} positions, "
                  f"median stop ${shadow.risk_per_share.median():.2f} "
                  f"vs ${plan.risk_per_share.median():.2f} on the live book"
                  + (f" [IV rescaled x{sc.iloc[0]:.2f}]" if len(sc) else ""))
            moved = set(shadow.symbol) ^ set(plan.symbol)
            if moved:
                print(f"  different names selected: {' '.join(sorted(moved))}")

        if not args.place:
            print("\nDRY RUN — nothing submitted. Re-run with --place to send.")
            return

        clock = b.clock()
        if not clock.get("is_open"):
            print("\nmarket is closed — refusing to place")
            return

        placed = []
        for _, r in plan.iterrows():
            cid = f"orb-{sess}-{r.symbol}"[:48]
            try:
                o = b.stop_entry_with_bracket(
                    r.symbol, r.side, int(r.shares), float(r.entry),
                    float(r.stop), client_order_id=cid)
                placed.append((r.symbol, o.id, "bracket"))
            except BrokerError as exc:
                try:
                    o = b.stop_entry_simple(r.symbol, r.side, int(r.shares),
                                            float(r.entry), client_order_id=cid)
                    placed.append((r.symbol, o.id, f"simple (bracket rejected)"))
                except BrokerError as exc2:
                    placed.append((r.symbol, "", f"FAILED: {exc2}"[:90]))
        print()
        for sym, oid, note in placed:
            print(f"  {sym:6s} {note:28s} {oid}")
        print(f"\n{sum(1 for _,o,_ in placed if o)} of {len(plan)} submitted")


def cmd_status(args) -> None:
    with PaperBroker() as b:
        b.assert_paper()
        pos, orders = b.positions(), b.orders("open")
        print(f"open positions: {len(pos)} | open orders: {len(orders)}")
        if pos:
            df = pd.DataFrame([{
                "symbol": p["symbol"], "qty": float(p["qty"]),
                "avg_entry": float(p["avg_entry_price"]),
                "current": float(p["current_price"]),
                "unrealized": float(p["unrealized_pl"]),
            } for p in pos])
            print(df.round(3).to_string(index=False))
            print(f"\nunrealized total: ${df.unrealized.sum():+,.2f}")
        for o in orders:
            print(f"  {o.symbol:6s} {o.side:4s} {o.type:5s} qty {o.qty:g} "
                  f"stop {o.stop_price} [{o.status}]")


def cmd_flatten(args) -> None:
    if args.place and not guard_window("flatten"):
        return
    prof = RobinhoodProfile()
    with PaperBroker() as b:
        b.assert_paper()
        pos, orders = b.positions(), b.orders("open")
        print(f"{len(pos)} positions, {len(orders)} resting orders")
        if not args.place:
            print("DRY RUN — re-run with --place to close.")
            return
        if orders:
            b.cancel_all()
            print("cancelled resting orders")
        if pos:
            # Robinhood has no market-on-close, so this is what it would do:
            # plain market orders near the bell, not a `cls` order.
            b.close_all_positions(cancel_orders=True)
            print(f"submitted market exits for {len(pos)} positions")


def cmd_report(args) -> None:
    sess = _session(args)
    plan_path = PLAN_DIR / f"plan_{sess}.csv"
    if not plan_path.exists():
        print(f"no plan recorded for {sess}")
        return
    plan = pd.read_csv(plan_path).set_index("symbol")
    with PaperBroker() as b:
        b.assert_paper()
        fills = [o for o in b.orders("closed", limit=500) if o.filled_qty > 0]
    if not fills:
        print("no fills yet")
        return
    rows = []
    for o in fills:
        if o.symbol not in plan.index:
            continue
        p = plan.loc[o.symbol]
        modelled = float(p.entry) if o.type == "stop" else np.nan
        actual = o.filled_avg_price
        slip = (actual - modelled) * (1 if p.side == "buy" else -1) \
            if (actual and np.isfinite(modelled)) else np.nan
        rows.append({"date": sess, "symbol": o.symbol, "side": o.side,
                     "type": o.type, "qty": o.filled_qty,
                     "modelled_price": modelled, "actual_price": actual,
                     "slippage_per_share": slip, "order_id": o.id})
    if not rows:
        print("no fills matched today's plan")
        return
    df = pd.DataFrame(rows)
    print(df.round(4).to_string(index=False))
    s = df.slippage_per_share.dropna()
    if len(s):
        print(f"\nmedian slippage vs modelled: ${s.median():+.4f}/share")
        print("NOTE: paper fills are simulated against the quote. This number is "
              "not evidence about real slippage.")
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    df.to_csv(FILL_LOG, mode="a", header=not FILL_LOG.exists(), index=False)
    print(f"-> appended to {FILL_LOG}")
    _rebuild_dashboard(sess)


def _rebuild_dashboard(sess) -> None:
    """Rebuild the dashboard in-process. launchd runs one command per agent, so
    chaining `report && dashboard` in a shell is not available to us."""
    try:
        import subprocess
        subprocess.run([sys.executable, str(HERE / "dashboard.py"),
                        "--date", str(sess)], check=False, timeout=900)
    except Exception as exc:
        log.warning("dashboard rebuild failed: %s", exc)


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--capital", type=float, default=25_000.0,
                   help="sizing basis, NOT the paper account balance")
    p.add_argument("--date", default=None, help="YYYY-MM-DD (default: today ET)")
    p.add_argument("--long-only", action="store_true")
    p.add_argument("--universe", default=None,
                   help="CSV with a `symbol` column (default: universe_live.csv)")
    sub = p.add_subparsers(dest="cmd", required=True)
    hp = sub.add_parser("build-history")
    hp.add_argument("--days", type=int, default=60)
    hp.set_defaults(func=cmd_build_history)
    for name, fn, needs_place in (("screen", cmd_screen, False),
                                  ("enter", cmd_enter, True),
                                  ("status", cmd_status, False),
                                  ("flatten", cmd_flatten, True),
                                  ("report", cmd_report, False)):
        sp = sub.add_parser(name)
        if needs_place:
            sp.add_argument("--place", action="store_true",
                            help="actually submit (default: dry run)")
        sp.set_defaults(func=fn)
    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
