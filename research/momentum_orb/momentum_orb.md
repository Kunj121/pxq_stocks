# Momentum ORB — 5-Minute Opening Range Breakout on Stocks in Play

Summary of **"A Profitable Day Trading Strategy For The U.S. Equity Market"**
Carlo Zarattini (Concretum Research), Andrea Barbon (University of St. Gallen / Swiss
Finance Institute), Andrew Aziz (Peak Capital Trading / Bear Bull Traders).
First version 16 February 2024. SSRN 4729284. Local copy: [`papers/ssrn-4729284.pdf`](../../papers/ssrn-4729284.pdf).

---

## 1. What the strategy actually is

An **Opening Range Breakout (ORB)**: define a range from the first *n* minutes of the
regular session, then trade a breakout of that range — but **only in the direction the
opening range itself moved**.

The paper's contribution is not the ORB (Crabel, 1990). It is the demonstration that
ORB is close to worthless applied indiscriminately, and strongly profitable once
restricted to **Stocks in Play** — names with abnormal opening-range volume relative to
their own recent history.

### The decision, in order, for one stock on one day

```
09:30 ─────────── 09:35 ──────────────────────────────────── 16:00
   │                │                                          │
   │  opening range │        wait for breakout                 │  close
   │  (first 5 min) │                                          │  position
   └────────────────┘                                          │
        │                                                      │
        ├── close > open  (bullish)  → LONG only,  stop-BUY  at OR high
        ├── close < open  (bearish)  → SHORT only, stop-SELL at OR low
        └── close = open  (doji)     → no trade
```

1. **Direction is locked by the first candle.** A bullish opening range permits *only*
   a long. If price instead breaks the range low, nothing happens — the trade is not
   taken. This asymmetry is the paper's stated departure from the textbook ORB.
2. **Entry** is a stop order resting at the opening range high (long) or low (short).
   If it is never touched, there is no trade that day.
3. **Stop loss** at **10% of the 14-day ATR** away from the *executed entry price*.
4. **Profit target: none.** Winners are held to the 16:00 close.
5. **Position size** so that being stopped out costs exactly **1% of account equity**.
   Shares = `0.01 × Equity ÷ (0.10 × ATR14)`. Capped by **4× leverage** in aggregate,
   which in practice binds constantly and makes most positions smaller than 1% risk.

### The filters (this is where the alpha lives)

| # | Filter | Purpose |
|---|---|---|
| 1 | Opening price > **$5** | exclude penny stocks |
| 2 | Avg daily volume, prior **14 days** ≥ **1,000,000** shares | liquidity |
| 3 | **ATR(14) > $0.50** | enough range to pay for the spread |
| 4 | **Relative Volume ≥ 100%** | the stock is *in play* today |
| 5 | Trade only the **top 20** names by Relative Volume | concentrate on the most in play |

**Relative Volume** is the whole game:

$$\text{RelVol}_{t,j} = \frac{\text{ORVolume}_{t,j}}{\frac{1}{14}\sum_{i=1}^{14}\text{ORVolume}_{t-i,j}}$$

where `ORVolume` is volume in the **first 5 minutes only** — not the day's volume.
It compares a stock's opening-range activity to *its own* recent opening ranges, so it
is scale-free across large and small caps.

Filters 1–3 alone are the **Base Strategy**. Filters 4–5 turn it into the headline
strategy. The gap between them is the paper's entire result.

---

## 2. Headline results to replicate

**Table 2** — 2016-01-01 to 2023-12-31, $25,000 start, $0.0035/share commission:

| Strategy | Total Return | IRR | Volatility | Sharpe | Hit Ratio | MDD | Worst Day | Alpha | Beta |
|---|---|---|---|---|---|---|---|---|---|
| ORB Base (filters 1–3) | **29%** | 3.2% | 6.6% | 0.48 | 41.4% | 13% | −0.8% | 3.3% | 0.01 |
| **ORB + RelVol (1–5)** | **1,637%** | **41.6%** | 14.8% | **2.81** | 48.4% | 12% | −1.61% | **35.8%** | 0.00 |
| S&P 500 buy & hold | 198% | 14.2% | 18.3% | 0.78 | 54.9% | 34% | −10.9% | 0.00% | 1.00 |

**Table 3** — same rules at other opening-range lengths:

| Opening range | Total Return | IRR | Sharpe | MDD |
|---|---|---|---|---|
| **5-minute** | **1,637%** | 41.6% | **2.81** | 12% |
| 15-minute | 272% | 17.4% | 1.43 | 11% |
| 30-minute | 21% | 2.3% | 0.21 | 35% |
| 60-minute | 39% | 4.1% | 0.40 | 21% |
| COMBO (equal-weight of all four) | 234% | 15.8% | 1.99 | 7% |

The 5-minute dominance is stated by the authors to be unexplained.

**Figure 4** — average PnL per trade, in units of R, bucketed by Relative Volume:

| RelVol bucket | Avg PnL |
|---|---|
| < 100% | **−0.02 R** |
| > 100% | **+0.08 R** |
| > 3,000% (30×) | **+0.38 R** |

This monotonic relationship is the causal claim. **If a replication reproduces nothing
else, it should reproduce this** — it is the mechanism, and it needs far less data than
the equity curve does.

### R — the unit everything is quoted in

`R` = the per-share risk = `0.10 × ATR14`. A trade that moves $6.81 in your favour with
R = $0.50 is `+13.62R`. Quoting PnL in R makes trades comparable across a $9 stock and a
$900 stock, and makes results independent of the position-sizing rule.

---

## 3. Data requirements

### 3.1 What the paper used

| | |
|---|---|
| **Universe** | ~7,000 stocks, all NYSE + Nasdaq listings, 2016-01-01 → 2023-12-31 |
| **Survivorship** | Free of it — CRSP includes delisted names (they cite Twitter, delisted 2022-10-27) |
| **Daily data** | CRSP — for ATR(14) and 14-day average volume |
| **Intraday data** | IQFeed, **1-minute**, **unadjusted** for splits and dividends |
| **Benchmark** | S&P 500 |
| **Tooling** | MATLAB R2023a |

### 3.2 What this replication needs

| Requirement | Granularity | Why | Source here |
|---|---|---|---|
| Opening range OHLCV | **1-minute**, 09:30–09:35 ET | direction, entry level, RelVol numerator | Alpaca `1Min`, `session=regular` |
| Intraday path 09:35 → 16:00 | **1-minute** | detect entry trigger, then stop-loss touch, then close | Alpaca `1Min` |
| Prior 14 days of opening-range volume | 1-minute, first 5 min of each prior day | RelVol denominator | same series |
| Daily OHLC, prior 14 days | **1-day** | ATR(14) — needs prior close for true range | Alpaca `1Day` |
| Daily volume, prior 14 days | 1-day | liquidity filter | Alpaca `1Day` |
| Day's opening price | 1-minute or daily open | the > $5 filter | Alpaca |
| Benchmark daily closes | 1-day | Sharpe/alpha/beta comparison | Alpaca `SPY` |

**Adjustment:** pull **`raw`** (unadjusted). The paper is explicit about this. Using
`adjustment="all"` would retroactively restate 2016 prices by every subsequent dividend
and split, which silently breaks the `> $5` and `ATR > $0.50` filters — a stock that
traded at $6 in 2016 can appear as $3 today, and would be wrongly excluded.

**Minimum viable slice.** To reproduce Figure 4 (the mechanism) you need only the
opening range and the day's path for a modest universe. To reproduce the *equity curve*
you need breadth — the top-20-of-7,000 selection is a ~0.3rd-percentile cut, and it
cannot be approximated from a hundred stocks.

### 3.3 What we cannot get, and what it costs

| Gap | Consequence |
|---|---|
| **No enumeration of the 2016 listed universe.** Alpaca's asset master is *today's* tradable list. | Universe construction is survivorship-biased. Bars for delisted names *are* served once you know the ticker (APC, MXIM, XEC, CLR all return history up to their delisting) — but you cannot *discover* the names that vanished. |
| **~7,000 stocks is not fetchable here.** 7,000 × 8 years × 390 bars/day ≈ 5.5 billion bars. | Universe capped at 100 names. The top-20 filter therefore selects the top 20% of a curated list, not the top 0.3% of the market. **Expect materially lower returns than the paper**, and much lower realized RelVol on selected trades. |
| **No news / catalyst data.** | "Stocks in Play" is proxied *only* by RelVol, as the paper does. No loss of fidelity — the paper does the same. |
| **1-minute bars, not tick data.** | Stop and entry fills are modelled within a bar. See assumptions in the notebook. |

Alpaca equity history begins **2016-01-04**, which lines up with the paper's window —
so, unusually, there is no start-date truncation to apologise for here.

---

## 4. Implementation notes that matter

- **ATR(14)** is Wilder's: `TR = max(H−L, |H−C_prev|, |L−C_prev|)`, then Wilder's
  smoothing (not a simple mean). Use only days **strictly before** the trade date.
- **All lookbacks must be causal.** RelVol uses the previous 14 days' opening ranges,
  not including today. The 14-day volume and ATR filters likewise.
- **Entry and stop in the same bar** is common with a stop this tight. Sort the entry
  bar three ways: *provably stopped* (closes beyond the stop, or filled at the bar open),
  *provably safe* (the bar **opens** beyond the stop, so for a long price rose from under
  it to the trigger and the low necessarily predates the fill), and *genuinely
  ambiguous*. Only the third is in dispute. Charging every excursion — the naive
  "conservative" reading — silently charges losses to positions that did not exist yet,
  and on this data it is worth over 1,700 points of total return.
- **Doji** (open == close in the opening range) means no trade — and on 1-minute data
  it is not rare for illiquid names.
- **Gap risk is limited** by construction: positions are opened and closed inside one
  session, so there is no overnight gap. This is why the strategy's worst day (−1.61%)
  is so much smaller than the S&P's (−10.9%).
- **Leverage cap binds.** With 20 positions each sized to risk 1%, gross exposure would
  routinely exceed 4×. The cap scales every position down pro-rata, so realized risk per
  trade is usually well below 1%.

---

## 5. Reading list from the paper

- Crabel (1990) — original ORB.
- Zarattini & Aziz (2023) — the QQQ/TQQQ precursor this paper generalises.
- Wu et al. (2021) — finds profit targets *hurt* ORB, stop losses help. This is why the
  strategy has a stop but no target.
- Tsai et al. (2019) — finds the 5-minute range optimal on index futures, consistent
  with Table 3 here.

---

## 6. Running it

```bash
source .venv/bin/activate

# 1. freeze the universe (writes universe_selected.csv)
python research/momentum_orb/select_universe.py

# 2. download 1-minute bars — long; run it in the background
python research/momentum_orb/fetch_minutes.py

# 3. the engine's fragile part, pinned
python -m pytest research/momentum_orb/test_orb.py -q

# 4. produce every artefact into out/momentum_orb/
python research/momentum_orb/run_backtest.py

# 5. the exploration
jupyter lab research/momentum_orb/momentum_orb.ipynb
```

| File | Role |
|---|---|
| `universe.py` | candidate tickers, seeded from the paper's own Tables 4 and 5 |
| `select_universe.py` | picks the universe **by rule** and freezes it to CSV |
| `fetch_minutes.py` | resumable background downloader |
| `orb.py` | the engine — stage 1 (per-symbol, in R) and stage 2 (sizing, leverage, costs) |
| `metrics.py` | statistics matching the paper's Table 2 columns |
| `test_orb.py` | unit tests for the entry/stop logic |
| `run_backtest.py` | runs every variant, writes parquet to `out/momentum_orb/` |
| `momentum_orb.ipynb` | the replication, the charts, assumptions, pros/cons, live requirements |
| [`executable/momentum_orb.md`](executable/momentum_orb.md) | **the runbook** — parameters sized to the real account, daily sequence, expected PnL, gates before any live order |

Nothing here touches a broker. This is the **historical** lane — Alpaca, read-only. Live
order flow is Robinhood MCP under `execution/PROTOCOL.md`, and a backtest may motivate a
trade but may never price one.
