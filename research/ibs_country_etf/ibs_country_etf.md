# IBS Country ETF — Min-Max mean reversion

Summary of **"Using Internal Bar Strength as a Key Indicator for Trading Country ETFs"**
Aditya Pandey and Kunal Joshi, NYU Stern. arXiv:2306.12434v1, 14 June 2023.
Local copy: [`papers/ibs_strat.pdf`](../../papers/ibs_strat.pdf).

Builds on Pagonidis (2013), *The IBS Effect: Mean Reversion in Equity ETFs*.

---

## 1. The strategy

**Internal Bar Strength** locates the close inside the day's range:

$$\text{IBS} = \frac{\text{Close} - \text{Low}}{\text{High} - \text{Low}} \in [0, 1]$$

IBS near 0 means the ETF closed on its lows; near 1, on its highs. The claim is
mean reversion: a close on the lows tends to be followed by an up day, and vice versa.

### The Min-Max strategy (the paper's contribution)

```
        close of day t                          close of day t+1
              │                                        │
   compute IBS for all 15 ETFs                         │
              │                                        │
              ├── LONG  the ETF with the MINIMUM IBS ──┤ exit
              └── SHORT the ETF with the MAXIMUM IBS ──┘ exit
```

That is the entire rule. No thresholds, no filters, no stops. **Always in the market**
(100% time-in), one long and one short, held exactly one session, rebalanced daily.

The point of ranking rather than thresholding: a threshold strategy only trades when IBS
happens to breach 0.2/0.8, so it sits in cash most of the time (39–54% time-in per
Table 2). Min-Max is always positioned, and the paper attributes most of its Sharpe
advantage to that.

### Variants the paper tests

| Variant | Finding |
|---|---|
| **Top-N / bottom-N** (long N lowest, short N highest) | Sharpe **rises** with N (Fig. 6) |
| **Basket size** (2–14 ETFs) | Sharpe **rises** with basket size (Fig. 3) |
| **Holding period** 1–4 days | Sharpe **falls** as holding lengthens (Fig. 7) — the signal is one-day only |
| **IBS computed over n days** | 2-day IBS is **worse** than 1-day (Fig. 8) |
| **Open-to-open instead of close-to-close** | **Collapses** to ≈0 or negative (Table 5) |
| **Long-only vs short-only** | Long-only Sharpe consistently higher (Table 4) |
| **Threshold instead of Min-Max** | Universally worse (Figs. 9, 10) |
| **Borrow cost on the short leg** | Survives to ~0.15%/day (~55% annualised), dies past it (Table 6) |
| **Emerging vs developed baskets** | Mixed baskets beat either pure basket (Tables 7, 8) |

---

## 2. Numbers to replicate

### Headline — Table 2

| Strategy | Sharpe | Time in |
|---|---|---|
| **IBS Min-Max (all 15 ETFs)** | **2.908** | 100% |
| PIN threshold | 2.167 | 54.5% |
| EWJ threshold | 1.586 | 41.5% |
| EWI threshold | 1.476 | 46.9% |
| EIS threshold | 1.260 | 47.5% |
| EZA threshold | 1.081 | 45.6% |
| FXI threshold | 1.051 | 39.4% |
| EWA threshold | 0.974 | 45.4% |
| EWT threshold | 0.957 | 42.0% |
| EZU threshold | 0.709 | 48.1% |
| EWS threshold | 0.665 | 43.2% |
| EWZ threshold | 0.482 | 45.0% |
| EWU threshold | 0.288 | 43.8% |
| IVV threshold | 0.201 | 48.1% |
| EWW threshold | −0.193 | 46.8% |
| EWC threshold | −0.451 | 44.8% |

Best hand-picked basket (Table 4): Sharpe **3.904** on
`india, tw, can, israel, uk, spore, aus` at N=2.

### The mechanism — Table 3

P(next-day return favours the position), by ETF:

| Ticker | Long on IBS < 0.2 | Short on IBS > 0.8 |
|---|---|---|
| EWJ | 0.606 | 0.514 |
| EIS | 0.588 | 0.514 |
| PIN | 0.581 | 0.578 |
| EWT | 0.572 | 0.503 |
| FXI | 0.568 | 0.523 |
| IVV | 0.566 | 0.460 |
| EZU | 0.552 | 0.477 |
| EWS | 0.550 | 0.515 |
| EWI | 0.548 | 0.511 |
| EWZ | 0.544 | 0.508 |
| EZA | 0.541 | 0.510 |

**This is the claim to target first.** It is a simple conditional probability, needs
only daily bars, and if it fails the Sharpe is meaningless. Note the asymmetry the
authors do not dwell on: the long side runs 54–61%, while the short side is 46–58% and
sits at or *below* 50% for IVV, EZU and EWZ. The edge is mostly on the long leg.

---

## 3. Data requirements

### 3.1 What the paper used

| | |
|---|---|
| Universe | 15 country ETFs (Table 1) |
| Period | **2009-01-01 → 2019-12-31** (11 years) |
| Frequency | **Daily OHLC** |
| Source | Yahoo Finance |
| Costs | No commissions modelled; borrow assumed 0.01%/day on shorts |
| Tooling | Python, NumPy, Pandas, Seaborn |

**Ticker table error.** Table 1 lists `EWI` as "South Korea". EWI is *iShares MSCI
Italy*; South Korea is EWY. The basket labels in Tables 4/5 use "sk", which maps to EWI
throughout. This replication uses **EWI (Italy)** and reads "sk" as EWI, matching what
the paper actually computed rather than what it says.

### 3.2 What this replication needs

| Requirement | Granularity | Why | Source |
|---|---|---|---|
| Daily OHLC, 15 ETFs | **1 day** | IBS needs H, L, C of the same session | Alpaca `1Day` |
| Daily close series | 1 day | close-to-close returns | same |
| Daily open series | 1 day | the open-to-open robustness check | same |

That is the entire requirement: **~40,000 daily bars**, seconds to fetch. This is a
cheap paper to replicate — the constraint is history, not volume.

**Adjustment:** use `adjustment="all"`. Dividend and split adjustment scales a bar's
O/H/L/C by a common factor, so **IBS is invariant to it** — but close-to-close returns
are not, and country ETFs pay meaningful dividends. Adjusted closes give total returns,
which is what a Sharpe should be computed on.

### 3.3 The gap, and what it costs

| Gap | Consequence |
|---|---|
| **Alpaca equity history starts 2016-01-04.** The paper runs from 2009. | **7 of 11 years are unavailable.** We can see only 2016–2019 of the paper's window — and that excludes the 2009–2012 post-crisis period when mean reversion was unusually strong. Expect a lower Sharpe on the overlap for that reason alone. |
| **PIN was liquidated** (last bar 2026-02-20). | It is in the universe until then and drops out after. Handled by trading only ETFs with data on the day. |
| **No borrow-rate data.** | Borrow is modelled as a flat daily rate, as the paper does, and swept across the same range. |

### 3.4 The windows this replication runs

Because the data starts in 2016, the honest structure is three windows, not one:

| Window | Sessions | What it tests |
|---|---|---|
| **2016-01-04 → 2019-12-31** | ~1,006 | the slice of the paper's period we can see |
| **2020-01-01 → 2026-09-08** | ~1,679 | out-of-sample |
| **2023-06-14 → 2026-09-08** | ~815 | **post-publication** — the strongest test of all |

A strategy this simple, published on arXiv, with a 2.9 Sharpe, is exactly the kind that
should be checked for decay after publication.

---

## 4. Implementation notes

- **Causality.** IBS on day *t* uses only day *t*'s bar and positions are entered at
  that close; the return earned is *t → t+1*. So the signal must be shifted one day
  against the return series. Getting this wrong produces a spectacular and entirely
  fake Sharpe.
- **Sharpe is leverage-invariant**, so gross exposure does not affect the headline
  number. Base case here is dollar-neutral at 100% gross (50% long, 50% short); the
  equity curve scales with that choice but the Sharpe does not.
- **Ties in IBS** are possible when H == L (a zero-range day). Those bars have an
  undefined IBS and must be dropped, not filled with 0.5.
- **Missing days.** ETFs track different countries but trade on the US calendar, so
  bars align. Days where an ETF has no bar (pre-listing, post-liquidation) simply
  exclude it from that day's ranking.
- **The paper reports no risk-free rate.** Sharpe here uses rf = 0 to match.
- **Table 4's baskets are selected on the full sample** — they are the best of many
  random combinations, so those Sharpes are in-sample maxima and should not be treated
  as forecasts. The all-15 basket (2.908) is the honest headline.


---

## 5. Layout

| File | Role |
|---|---|
| `ibs.py` | the engine — IBS, cross-sectional selection, portfolio, Table 3 probabilities |
| `metrics.py` | statistics matching the paper's reporting |
| `test_ibs.py` | unit tests; the causality and open-to-open lags are pinned here |
| `run_backtest.py` | every variant, writing parquet to `out/ibs_country_etf/` |
| `ibs_country_etf.ipynb` | the replication, charts, assumptions, pros/cons, live requirements |
| [`executable/ibs_country_etf.md`](executable/ibs_country_etf.md) | **the runbook** — sized to the real account, with the verdict |

```bash
source .venv/bin/activate
python -m pytest research/ibs_country_etf/test_ibs.py -q
python research/ibs_country_etf/run_backtest.py
jupyter lab research/ibs_country_etf/ibs_country_etf.ipynb
```

This is the **historical** lane — Alpaca, read-only. A replication may motivate a trade;
it may never price one.
