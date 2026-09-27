# Regime detection — spec, data requirements, and gaps

**Paper:** Devanga Jyoti Goswami, *Identifying and Forecasting US Equity Market Regimes:
An Unsupervised and Supervised Machine Learning Analysis of Bull and Bear Market States,
2010–2025*. Local copy: [`papers/regime_detection.pdf`](../../papers/regime_detection.pdf).

**Notebook:** [`regime_detection.ipynb`](regime_detection.ipynb) — the replication, the
charts, and the verdict.

---

## 1 — The spec

This is not a trading-signal paper with a rule you can draw as a timeline. It is a
three-stage pipeline whose *conclusion* is that the three stages answer different
questions, and that succeeding at the first tells you very little about the second or
third. The replication has to keep them apart too.

### Stage 1 — identify (Sections V.B–V.F)

1. Build 15 daily features from an S&P 500 price/volume series plus VIX, 10-year
   Treasury yield, and WTI crude (listed below).
2. Split the sample chronologically 70/30. Fit **everything** on the first 70% only.
3. Standardise each feature to zero mean and unit variance, scaler fit on train.
4. **PCA**, keeping the smallest number of components reaching ≥90% cumulative variance.
   The paper needs 8 for 92.1%.
5. **K-Means**, `n_init=50`, for K = 2…6 on the training-period component scores.
   Select K by **silhouette**. The paper selects K=2 at 0.466.
6. Validate against a **2-component full-covariance GMM** on the same scores; report raw
   label agreement as a descriptive diagnostic only.
7. **Label**: the cluster with the higher mean daily return is `Bull / Low-Risk`, the
   other `Bear / High-Risk`. This is ex-post and the paper says so (Section V.F).

### Stage 2 — describe (Sections VI.C–VI.F)

Per-regime descriptive statistics, Welch t-tests, Mann-Whitney U, Cohen's d, a 5,000-draw
bootstrap CI on the Bull-minus-Bear mean return, a first-order transition matrix, and
episode-duration statistics.

### Stage 3 — forecast, then trade (Sections V.I, V.J)

**Forecast.** X is day *t*'s features, y is day *t+1*'s label. Train Logistic Regression,
Random Forest and Gradient Boosting on the first 70%; score on the last 30% against a
**persistence baseline** ("tomorrow's regime equals today's"), evaluated on identical rows.

**Trade.** 100% equity the day after a Bull label, 0% the day after a Bear label. Signal
lagged one trading day. 10 bp charged on every unit of position change. Compound daily net
returns; report total return, CAGR, annualised volatility, Sharpe, max drawdown, Calmar,
and percentage of positive days.

### The features

`Return`, `Log_Return`, `Volatility_20D`, `Volatility_60D`, `Momentum_20D`,
`Momentum_60D`, `Distance_MA50`, `Distance_MA200`, `Drawdown`, `Volume_Ratio`, `VIX`,
`VIX_Change`, `VIX_Ratio`, `Treasury_20D_Change`, `Oil_Return`

Raw OHLC levels, raw volume and the running maximum are **deliberately excluded** as
non-stationary (Section IV). Section XI.J restores them as a negative control and watches
the pipeline fail — that check is replicated in notebook section 11.

### The mechanism claim

Table 12: **mean forward returns after a Bear day exceed those after a Bull day at the 1-,
5- and 20-day horizons.** This is the cross-sectional fact that explains the strategy's
return shortfall, it needs no equity curve, and it is the first thing worth targeting.

### The numbers to hit

| | Paper |
|---|---|
| observations | 3,823 (2010-10-18 → 2025-12-31) |
| components to ≥90% variance | 8 (92.14%) |
| PC1 share | 36.67% |
| K selected / silhouette | 2 / 0.466 |
| Bull / Bear split | 83.7% / 16.3% |
| Bull mean daily return | +0.1376% |
| Bear mean daily return | −0.3810% |
| Bull / Bear annualised volatility | 11.73% / 32.49% |
| Bull / Bear mean VIX | 16.16 / 28.30 |
| Bull / Bear mean drawdown | −2.42% / −13.12% |
| Bull→Bull / Bear→Bear transition | 96.47% / 81.83% |
| Bear episodes: count / mean / median / max | 113 / 5.50 / 1 / 67 |
| Bull episodes: count / mean / median / max | 114 / 28.08 / 3 / 404 |
| best forecaster | **persistence**, 93.55% |
| RF top-4 features | Distance_MA200, Momentum_60D, Drawdown, Distance_MA50 (67.9%) |
| strategy CAGR / Sharpe / MDD | 5.80% / 0.546 / −28.20% |
| buy & hold CAGR / Sharpe / MDD | 12.68% / 0.780 / −33.92% |
| 20-day forward return after Bear / Bull | 2.504% / 0.720% |
| widened-spec best silhouette | 0.324 (from 0.466) |
| widened-spec test-window collapse | one cluster = 79.7% of test, 5.2% of train |

---

## 2 — Data requirements, and what we can actually serve

| Needed | Granularity | Why | Served by |
|---|---|---|---|
| S&P 500 closing level | daily | return, momentum, MA distance, drawdown | Alpaca `SPY` `1Day`, `adjustment="raw"` |
| Trading volume | daily | `Volume_Ratio` | Alpaca `SPY` `1Day` |
| VIX level | daily | `VIX`, `VIX_Change`, `VIX_Ratio` | **Cboe `VIX_History.csv`** |
| 10-year UST yield | daily | `Treasury_20D_Change` | **FRED `DGS10`** |
| WTI crude | daily | `Oil_Return` | Alpaca `USL` `1Day` (proxy) |

### The gaps, and which direction each one bites

**History start.** Alpaca equities begin **2016-01-04**. The paper starts 2010-10-18. With
a 200-session warmup for `Distance_MA200`, the replication runs **2016-10-17 → 2026-09-18,
n = 2,494** — 65% of the paper's sample. It drops 2011's near-bear and the 2010–2015
low-volatility grind, so our window is *more* volatile than theirs. That is the most likely
cause of our larger Bear share (18.1% vs 16.3%) and lower silhouette (0.410 vs 0.466).

Running past the paper's 2025-12-31 end date is a bonus, not a cost: it adds a genuinely
out-of-paper stretch.

**No index data on Alpaca.** The VIX is not a tradable equity and Alpaca serves no indices;
Massive's indices tier is not entitled on this account. The Cboe file is the index
publisher's own daily history, fetched over one scripted HTTP GET in
[`fetch_data.py`](fetch_data.py).

**No macro data on Alpaca.** Same story for the 10-year yield, from FRED.

Both CSV sources sit against [`data-routing`](../../.claude/skills/data-routing/SKILL.md)'s
"no vendored price CSVs" rule. That rule exists so a live order is never priced off a
scraped quote; these are published index and macro histories feeding a research notebook,
and nothing here reaches an order. Sanctioned for this project by the user. Massive's
`/fed/v1/treasury-yields` would serve DGS10 through an MCP lane, but only into context —
FRED keeps `fetch_data.py` self-contained and the panel regenerable.

**No futures on Alpaca, and `USO` is not served.** `USL` (United States 12 Month Oil Fund)
is the WTI proxy that is. It carries roll cost, so `Oil_Return` is noisier than the paper's
spot return. At 2.4% of RF importance and absent from PC1's top loadings, the effect on the
regime solution is negligible.

**Adjustment.** `adjustment="raw"`, not the repo default `"all"`. Four features are a
distance from a moving average or a drawdown from a running maximum on a *price* level;
dividend-adjusting SPY restates that level and shifts all four.

**Running-maximum anchor.** Our `Drawdown` measures from a maximum anchored at 2016-01-04
rather than 2010, which would understate early-2016 drawdowns. SPY made new highs in July
2016 — inside the 200-session warmup that gets discarded — so no surviving row is affected.

---

## 3 — What the paper's own numbers disagree about

**The feature count.** The abstract, Section IV and Table 2 all say **sixteen** features.
Section IV's own list names **fifteen**. Table 9's Random Forest importances list
**fourteen**, omitting `Log_Return`. No sixteenth variable appears anywhere in the text.
This replication uses the fifteen that are named.

---

## 4 — Layout

```
papers/regime_detection.pdf           the source
research/regime_detection/
    regime_detection.md               this file
    fetch_data.py                     SPY/USL (Alpaca) + VIX (Cboe) + DGS10 (FRED)
    features.py                       the 15 features, same-day by construction
    pipeline.py                       PCA/K-Means/GMM, stats, forecasting, strategy
    run_replication.py                writes every table as parquet
    regime_detection.ipynb            the exploration and the charts
    raw/                              cached publisher CSVs
data/regime_panel.parquet             the joined daily panel
out/regime_detection/*.parquet        one file per table the paper reports
```

Reproduce from scratch:

```bash
source .venv/bin/activate
python -m research.regime_detection.fetch_data        # --force to re-download the CSVs
python -m research.regime_detection.run_replication
jupyter lab research/regime_detection/regime_detection.ipynb
```

---

## 5 — Verdict, in one line each

Every claim the paper makes replicates, including both of its negative ones: K=2 is
selected, the regimes are economically coherent and near-identically characterised,
persistence beats all three classifiers, the tactical strategy trades return for risk and
loses on Sharpe, the forward-return mechanism holds, and the widened-feature negative
control collapses harder here than in the paper.

Three things do not, and the notebook's section 12 carries the detail:

1. The feature count (fifteen named, sixteen claimed, fourteen used in Table 9).
2. The *probability* of a positive 20-day return after Bear — higher than Bull in the
   paper, lower here. The mean forward return claim, which is what the argument rests on,
   holds either way.
3. **The drawdown benefit as a general property.** Max drawdown improves, as the paper
   reports — but in our sample the strategy's *median* drawdown is three times worse, it is
   the shallower of the two on only 21% of days, and removing the Feb–Jun 2020 window erases
   the max-drawdown advantage entirely. The improvement is one event, not a habit.
