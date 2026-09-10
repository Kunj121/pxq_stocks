# IBS Country ETF — executable spec

**This is the runbook, not the research.** The study is in
[`../ibs_country_etf.md`](../ibs_country_etf.md) and
[`../ibs_country_etf.ipynb`](../ibs_country_etf.ipynb); this file is what you would
actually do at 15:58 each afternoon, sized to the account that exists.

> **Nothing here is authorised to run live.** No order without explicit human
> confirmation, every time — [`execution/PROTOCOL.md`](../../../execution/PROTOCOL.md).

---

## 1. Verdict first

**Do not trade this.** Not because the effect is fake — it is real and still alive — but
because the strategy turns over its entire book every day, and that turnover costs more
than the edge is worth at retail execution.

### Long-only, N=3, $10,000

| Window | Slippage | Sharpe | CAGR | MDD | **$/year on $10k** |
|---|---|---|---|---|---|
| paper window 2016-19 | 0 bps | 1.40 | 25.9% | 16.7% | **$2,588** |
| paper window 2016-19 | **5 bps** | 0.29 | 3.7% | 28.2% | **$366** |
| paper window 2016-19 | 10 bps | −0.82 | −14.7% | 53.1% | **−$1,465** |
| out-of-sample 2020-26 | 0 bps | 1.19 | 27.1% | 40.4% | **$2,708** |
| out-of-sample 2020-26 | **5 bps** | 0.32 | 4.7% | 42.3% | **$467** |
| out-of-sample 2020-26 | 10 bps | −0.55 | −13.8% | 68.4% | **−$1,381** |
| post-publication 2023-26 | 0 bps | 1.54 | 28.7% | 12.9% | **$2,871** |
| post-publication 2023-26 | **5 bps** | 0.42 | 6.0% | 23.5% | **$598** |
| post-publication 2023-26 | 10 bps | −0.69 | −12.7% | 45.9% | **−$1,274** |

Benchmark over the same windows: IVV buy-and-hold returns 14.8% / 15.4% / **20.7%** CAGR
at Sharpe 1.14 / 0.81 / **1.35**, for one trade.

### The arithmetic that decides it

```
mean daily turnover  =  154% of the book   (~3/4 of holdings replaced each session)
                     =  388x book turnover per year

   at  5 bps per side  ->  0.077% / day  ->  ~19 points of CAGR gone
   at 10 bps per side  ->  0.154% / day  ->  ~39 points of CAGR gone
```

Five basis points is roughly 2.5 cents on a $50 ETF. That is achievable on IVV or EWJ.
It is **not** achievable on EIS, PIN or EZA, which are the names the cross-sectional
ranking most often selects precisely because they move the most. A blended 5–10 bps is
the realistic retail range, and at that level the strategy returns between a little and
nothing — while underperforming a single IVV purchase.

---

## 2. What is actually worth keeping

Three findings from the replication survive and are worth acting on, even though the
strategy itself is not:

1. **The long leg of the IBS effect is durable.** P(next day up | IBS < 0.2) = **0.565**
   post-publication, identical to the paper's 2009–2019 mean. Ten years and a
   publication later. That is a real, persistent anomaly.
2. **The short leg is dead** — below a coin flip in every window, short-only Sharpe
   −0.43 post-publication. Never short on high IBS.
3. **The edge lives entirely in the close→open gap.** Open-to-open execution gives
   Sharpe ≈ 0. Anything that delays entry past the closing print removes the return.

Finding 1 is usable as a **timing overlay** on positions you were going to take anyway —
if you are buying an ETF this week, buying it on a day it closed near its low is free
edge. That does not require 388× turnover.

---

## 3. Account fit

| | |
|---|---|
| Only agentic-accessible account | **••••8486**, individual, **cash**, ~$282 |
| Shorting needed? | **No** — long-only is the better strategy here (post-publication Sharpe 1.54 vs 0.67). The cash-account restriction costs nothing. |
| Leverage needed? | **No.** Sharpe is leverage-invariant; 100% gross is the base case. |
| Settlement | **T+1 blocks this outright.** The book turns over daily, so proceeds would need to settle overnight to fund tomorrow's purchases. In a cash account they do not. |

The settlement constraint is fatal in a way slippage is not: it is not a cost that shrinks
the edge, it is a rule that prevents the strategy from being run at all in this account.
A margin account would be required, and margin is not needed for leverage here — only to
recycle unsettled proceeds.

---

## 4. Parameters, if it were run

| Parameter | Paper | **Executable** | Why |
|---|---|---|---|
| Universe | 15 country ETFs | **the liquid subset only** | EIS/PIN/EZA spreads are where the slippage comes from |
| Direction | long + short | **long only** | short leg is negative on this data |
| N per leg | 1 | **3** | best Sharpe across windows; N=1 is noisier, N>4 decays |
| Holding period | 1 day | **1 day** | 2+ days degrades sharply, as the paper found |
| IBS lookback | 1 day | **1 day** | monotone decline at 2, 3, 4 days |
| Entry / exit | close → close | **close → close** | open-to-open is worth ~0 |
| Borrow | 0.01%/day | **n/a** | long-only pays none |

---

## 5. Daily sequence

All times ET.

```
 15:50  pull the day's high, low and current price for each ETF in the universe
 15:56  IBS = (last - low) / (high - low), per ETF
        └─ rank; the 3 lowest are tomorrow's book
        └─ compare against today's holdings -> the trade list
 15:58  SELL everything not in tomorrow's book
        BUY  everything in it, equal weight
        └─ this is where the entire edge is won or lost
 16:00  close
```

The signal is computed from an incomplete bar. The true high/low/close are only known
*at* the close, so a live implementation always trades on a slightly wrong IBS. That
error is not modelled anywhere in the backtest and can only make results worse.

---

## 6. Gates before any live order

- [ ] **Measured close-execution slippage < 3 bps**, from a quarter of logged fills.
      Above ~5 bps there is no strategy; see §1.
- [ ] **Margin account** — not for leverage, but so daily proceeds recycle. T+1 in a cash
      account blocks the strategy outright.
- [ ] **A market-on-close mechanism.** Robinhood does not offer MOC orders. A market
      order in the final seconds is the nearest substitute and is exactly the execution
      the strategy cannot absorb.
- [ ] **Restrict the universe to ETFs whose closing spread is under 3 bps**, then re-run
      the backtest on that reduced universe — it will have a lower Sharpe than the
      15-name version and that number, not this one, is the honest expectation.
- [ ] **Capital above ~$5,000**, so whole-share quantization across 3 positions does not
      distort weights.
- [ ] **A written decay review.** Sharpe has fallen 1.70 → 0.97 → 0.67 across three
      consecutive windows. Set a level at which you stop.

---

## 7. Guardrails

`execution/guardrails.py`, reading the repo-root `.env`:

| Var | Current | For this strategy |
|---|---|---|
| `ALLOW_LIVE_ORDERS` | `false` | must be flipped deliberately |
| `MAX_ORDER_NOTIONAL_USD` | `500` | at $10k with N=3 each position is ~$3,300 — **would reject every trade** |
| `MAX_DAILY_ORDERS` | `10` | ~4–6 orders/day; within the cap |

Order flow follows [`execution/PROTOCOL.md`](../../../execution/PROTOCOL.md) without
exception. Every price that reaches an order comes from a live Robinhood MCP call made
now; Alpaca bars motivated this study and may never price one of its trades.

---

## 8. Honest summary

The paper's mechanism replicates on its long side and has survived publication. Its
headline does not: Sharpe 1.70 on the slice of its own window we can see, against a
claimed 2.908, decaying to 0.67 post-publication — at which point holding IVV was better
on every measure.

The strategy's problem is not the signal, it is that the signal is worth roughly 8 bps a
day and the strategy spends 154% of the book to collect it. **Recommended action: keep
the IBS long-side insight as an entry-timing overlay; do not run the rotation.**
