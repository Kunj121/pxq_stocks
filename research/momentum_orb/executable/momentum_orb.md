# Momentum ORB — executable spec

**This is the runbook, not the research.** The study lives in
[`../momentum_orb.md`](../momentum_orb.md) and
[`../momentum_orb.ipynb`](../momentum_orb.ipynb); this file is what you would
actually do at 9:29 in the morning, sized to the account that exists.

> **Nothing here is authorised to run live.** No order is placed without explicit
> human confirmation, every time — see [`execution/PROTOCOL.md`](../../../execution/PROTOCOL.md).
> This document exists so that decision can be made on numbers instead of vibes.

---

## 1. Verdict first

At **$10,000, unleveraged**, the honest expected PnL is **$150 – $1,000 per year**,
and the range crosses zero under assumptions that are not pessimistic.

| Intrabar reading | Slippage | IRR | **$/year** | Sharpe | MDD |
|---|---|---|---|---|---|
| favourable | none | 12.4% | **$1,913** | 2.39 | 6.7% |
| favourable | 0.5¢/sh | 7.8% | **$1,034** | 1.56 | 7.7% |
| favourable | 1¢/sh | 3.2% | **$364** | 0.68 | 8.8% |
| favourable | 2¢/sh | −5.2% | **−$433** | −1.10 | 36.6% |
| pessimistic | none | 5.7% | **$696** | 1.28 | 8.5% |
| pessimistic | 0.5¢/sh | 1.5% | **$153** | 0.35 | 10.3% |
| pessimistic | 1¢/sh | −2.7% | **−$249** | −0.62 | 24.3% |
| pessimistic | 2¢/sh | −10.2% | **−$725** | −2.57 | 58.5% |

*"Intrabar" is whether the ambiguous 5.7% of trades — where the stop level sits inside
the entry bar and one-minute OHLC cannot order the two touches — are charged as stopped.
Minute bars cannot settle it; it needs tick data.*

### The one number that decides it

```
56,671 shares traded per year
     x  $0.01 slippage
     x  2 sides
     =  $1,133 / year

  against a best-case gross edge of ~$1,900 / year
```

**One cent of slippage consumes 60% of the best case.** Stop orders firing into a
breakout on a stock that is, by construction, unusually active that morning are not
where you get price improvement. This strategy is not a signal problem; it is an
execution problem.

---

## 2. Account reality

| | |
|---|---|
| Only agentic-accessible account | **••••8486**, individual, **cash**, currently ~$282 |
| Shorting | Not available in a cash account (Reg T). Worth **~$60/year** here — do not take borrow risk for it. |
| Leverage | Not available, and not needed — 1× has a *better* Sharpe (2.48) than 4× (1.96). Leverage bought return, not edge. |
| Settlement | **T+1.** A fully-deployed day cannot be followed by another. This halves trading days and costs **~$500/year** — the single most expensive constraint, and it is unrelated to PDT. |

### What each constraint costs (favourable intrabar, 0.5¢ slippage)

| Configuration | IRR | $/year |
|---|---|---|
| long+short, every day — needs a margin account | 7.8% | $1,034 |
| long-only, every day | 7.5% | $974 |
| long+short, every other day | 9.2% | $526 |
| **long-only, every other day — the cash account** | **9.2%** | **$521** |

IRR *rises* while dollars fall: fewer, larger positions compound better per trading day,
but you get half as many days. Optimise for the dollars.

---

## 3. Parameters as they would actually be set

| Parameter | Research value | **Executable value** | Why it changed |
|---|---|---|---|
| Opening range | 5 min | **5 min** | dominates every other length by a wide margin |
| Direction | long + short | **long only** | cash account; costs ~$60/yr |
| Max leverage | 4× | **1×** | unavailable, and Sharpe is better without it |
| Risk per trade | 1% of equity | **1% nominal** | the 1× cap scales it to ~0.02% realized |
| Stop | 10% × ATR(14) | **10% × ATR(14)** | unchanged |
| Profit target | none, exit 16:00 | **none, exit 15:55** | leave margin before the close |
| Universe | 100 names | **top 20 by RelVol** from a live screen | unchanged |
| Starting capital | $25,000 | **$10,000** | what is available |
| Commission | $0.0035/sh | **$0** | Robinhood; worth ~56 points of total return |

### Operational profile at $10,000

| | |
|---|---|
| Positions per day | median **16**, max 20 |
| Trades per year | ~3,700 |
| Median position | **$782** — 11 shares at a ~$68 median price |
| Median stop distance | **$0.174/share** |
| Realized risk per position | ~$2.07 (0.02% of equity) |
| Median entry | **1 minute** after the range closes (p90: 40 min) |
| Outcome mix | **83% stopped out**, 17% held to the close |
| Per-trade win rate | **16.7%** |
| Losing days | 54.3% |
| Worst / best day | −1.17% / +3.22% |

Read those last four rows together: you lose on five trades out of six and on most days,
and the money arrives in a thin right tail. That is the strategy working as designed, and
it is the reason most people cannot run it.

---

## 4. The daily sequence

All times ET.

```
 09:00  pre-market screen
        └─ for every universe name: prior-14-day avg volume, ATR(14), last close
        └─ drop: price <= $5, avg volume < 1M, ATR <= $0.50

 09:30  session opens — do nothing

 09:35  opening range closes.  THIS IS THE WHOLE DECISION WINDOW.
        └─ per name: OR open/high/low/close, OR volume
        └─ RelVol = OR volume / mean(prior 14 days' OR volume)
        └─ drop RelVol < 1.0; rank; keep top 20
        └─ direction: OR close > OR open -> long.  close < open -> SKIP (no shorts)
        └─ doji (open == close) -> skip
        └─ size: shares = floor(0.01 x equity / (0.10 x ATR14)),
                 then scale all positions pro-rata so gross <= 1.0 x equity

 09:36  place stop-buy orders at each name's OR high
        └─ median fill lands here. Latency is not optional.

 intraday  each filled position carries a stop-loss at entry - 0.10 x ATR14
           no profit target, no intervention

 15:55  flatten everything still open, at market
```

The median entry is **one minute** after the range closes. Twenty orders placed by hand
in that window is not realistic; this is an automation problem before it is a trading
problem.

---

## 5. Gates before any live order

Every one of these must be true. They are ordered by what kills the strategy fastest.

- [ ] **Measured slippage < 0.5¢/share**, from at least one quarter of paper fills.
      This is the whole ballgame; see §6.
- [ ] **Real-time SIP data at 09:35** for the full screening universe. IEX-only data is
      ~2% of volume, and RelVol computed on it is noise, not signal.
- [ ] **Automated order placement.** 20 stop orders in under a minute.
- [ ] **Automated 15:55 flatten**, with a manual fallback.
- [ ] **Settled-cash check** each morning — in a cash account, yesterday's proceeds are
      not available today.
- [ ] **Account funded to the size being modelled.** At $300 the expected PnL is $12 over
      *eight years*; whole-share quantization means only 4% of signals reach one share.
- [ ] **A written drawdown limit** that stops the experiment, decided before the first
      order rather than during the first bad month.

---

## 6. The measurement phase

Do not run this for money before running it for data. The only unknown that matters is
your fill quality, and it costs nothing to measure.

1. **Paper-trade one quarter** at 0.1% risk per trade, not 1%.
2. **Log modelled vs actual on every fill.** The journal
   (`execution/logs/trades.csv`) already records the actual; add a `modelled_price`
   column so the delta is a straight subtraction.
   - entries: modelled = the opening-range high
   - stops: modelled = entry − 0.10 × ATR(14)
3. **Compute median slippage per share, entries and stops separately.** Stops will be
   worse; they fire into momentum.
4. **Substitute the measured number into §1** and re-read the verdict.

If measured slippage lands above ~1¢, the table says stop. That is a successful
experiment, not a failed one — it cost a quarter of paper trading instead of a year of
real money.

---

## 7. Guardrails

`execution/guardrails.py` reads the repo-root `.env`:

| Var | Current | For this strategy |
|---|---|---|
| `ALLOW_LIVE_ORDERS` | `false` | must be flipped **deliberately**, never as a side effect |
| `MAX_ORDER_NOTIONAL_USD` | `500` | median position is $782 — **would reject every trade today** |
| `MAX_DAILY_ORDERS` | `10` | the strategy needs ~16 entries + 16 exits — **would reject most of the day** |

Both caps would have to be raised before this could run, and raising them removes the
protection they exist to give. That is a decision to make consciously, with the numbers
in §1 in view.

Order flow itself follows [`execution/PROTOCOL.md`](../../../execution/PROTOCOL.md)
without exception: `get_accounts` → live quote → `review_equity_order` → **explicit human
confirmation** → `place_equity_order` → confirm the fill. Prices come from a live
Robinhood MCP call made now. Alpaca bars motivated this strategy; they may never price
one of its orders.

---

## 8. Paper trading it on Alpaca

Running now, dry-run by default: [`daily_orb.py`](daily_orb.py),
[`broker.py`](broker.py), [`run_daily.sh`](run_daily.sh),
[`crontab.example`](crontab.example).

**Alpaca paper removes every constraint Robinhood imposed — which is exactly why
they are imposed back on.** The paper account (`PA3GIPVHZV99`) holds $94k with 4x
margin and shorting enabled. Trading it as-is would measure a strategy nobody here
can run. So `RobinhoodProfile` caps it:

| | Alpaca paper allows | Imposed here | Why |
|---|---|---|---|
| Capital | $94,072 | **$10,000** | what the real account would hold |
| Leverage | 4x | **1x** | and 1x has the *better* Sharpe — 2.48 vs 1.96 |
| Shares | fractional | **whole only** | a stop-entry cannot be fractional |
| Exit | `time_in_force="cls"` (MOC) | **market orders near the bell** | Robinhood has no MOC |
| Commission | — | **$0** | matches Robinhood |

### What it can and cannot prove

**Can:** that 20 bracket stop orders land inside the 09:35 window (today's median
entry was **1 minute** after the range closed — that is the real engineering
risk); that RelVol computed live matches the backtest; that the flatten fires;
that halts and missing bars degrade gracefully.

**Cannot: slippage.** Alpaca's paper engine fills against the quote. The number
that decides this strategy is 56,671 shares/year x slippage x 2 sides against a
best-case edge near $1,900/yr. **Treat every paper P&L as an upper bound.**

### Safety

- **Dry run is the default.** Every phase prints the plan and submits nothing
  unless `--place` / `ORB_PLACE=1`.
- **`assert_paper()` refuses to run against a live account** — checked on the
  account number prefix *and* the trading host, because a mis-set env var could
  change either.
- **Every order carries a `client_order_id`**, so a retry after a timeout cannot
  double-fill.
- **Each phase self-guards on the Eastern clock** and declines outside its
  window, so a DST shift degrades to "did nothing" rather than trading an hour
  wrong.
- **Entries are brackets**, so a fill is never left without its protective stop.

### Two live-vs-backtest divergences found and fixed

Both would have made the live runner quietly disagree with the study it was
validated against — the failure mode that matters most here:

1. **RelVol denominator.** Using the last 14 *shared calendar dates* NaN'd out
   any name that missed a session (HOG). The backtest uses each symbol's own
   last 14 opening ranges. Fixed to match.
2. **Opening-range completeness.** Requiring all five 1-minute bars dropped the
   cross-section from 94 names to 83. The backtest only requires a bar at the
   bell. Fixed to match.

After both fixes the live plan reproduces the backtest's top-20 selection and
ordering exactly.

### Scheduling

`crontab.example` is written but **not installed**. It arms with `ORB_PLACE=1`;
leave it at `0` and every run is a dry run that logs the plan without trading.

---

## 9. Honest summary

The edge replicates. The 5-minute ORB on high-relative-volume names produced a
monotonic PnL-vs-RelVol relationship on independent data, from a different vendor,
across 74 million minute bars — that part of the paper is real.

What does not survive contact with this account is the **economics at size**. At $10,000
unleveraged, the expected outcome is a few hundred dollars a year, inside an error bar
set by two things minute bars cannot resolve, against 3,700 trades a year at a 16.7% win
rate. One penny of slippage is worth more than half the edge.

**Recommended next action: measure fills, do not deploy capital.**
