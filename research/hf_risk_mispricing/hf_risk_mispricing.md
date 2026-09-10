# High-frequency risk decomposition and mispricing

Summary of **"Characteristic-based Risk Decomposition and Mispricing in High-frequency
Return Panels"** — Youngmin Choi (Xavier), Soohun Kim (KAIST), Suzanne S. Lee (Georgia
Tech). March 2026. SSRN 5632370. Local copy: [`papers/ssrn-5632370.pdf`](../../papers/ssrn-5632370.pdf).

This is an academic asset-pricing paper, not a trading note. It is replicable only in
part, and **the part that is not replicable is the part the title is about.** That
constraint is stated up front rather than buried, because it determines everything below.

---

## 1. What the paper does

A four-step framework applied to a monthly panel of intraday returns, where `R` is an
`N × T` matrix of 30-minute returns for `N` stocks over `T` intervals in one month.

```
 STEP 1   split R into diffusive and jump components
          └─ Lee & Mykland (2008) jump test on each 30-min return
          └─ |r / (sigma_hat * sqrt(dt))| in the rejection region  ->  jump
          └─ yields  R_C  (continuous)  and  R_J  (jump), same shape as R

 STEP 2   estimate characteristic-based betas
          └─ betas_C = X . Theta_betaC ,  betas_J = X . Theta_betaJ
          └─ X is the N x L matrix of FIRM CHARACTERISTICS
          └─ Projected PCA on R_C and R_J separately
          └─ K_C = 4 diffusive factors, K_J = 2 jump factors (PER test)

 STEP 3   estimate mispricing orthogonal to both risk components
          └─ alpha = X . Theta_alpha

 STEP 4   intraday arbitrage portfolio
          └─ w = (1/N) . X . Theta_alpha
          └─ estimate on month m, hold through month m+1, roll forward
          └─ scale w to 10% annualised volatility in the estimation month
```

The novelty is **Step 2**: projecting the factor structure onto firm characteristics
rather than running unconstrained PCA. Everything characteristic-flavoured in the paper —
the cluster attributions, the risk-premia decomposition, the mispricing map — flows from
that projection.

---

## 2. Numbers to replicate

### Table 1 Panel C — the variance decomposition

Per stock-month, sampled at 30 minutes:

| Quantity | Mean | P25 | Median | P75 |
|---|---|---|---|---|
| Realized variation (RV) | 0.0191 | 0.0048 | 0.0094 | 0.0198 |
| Continuous variation (CV) | 0.0152 | 0.0041 | 0.0079 | 0.0161 |
| Jump variation (JV) | 0.0040 | 0.0000 | 0.0008 | 0.0030 |
| **Jump share (JV/RV)** | **0.1479** | 0.0000 | 0.1053 | 0.2240 |
| **Jump count per month** | **1.7910** | 0 | 1 | 2 |
| Mean \|jump\| (conditional) | 0.0372 | 0.0218 | 0.0312 | 0.0456 |

**This is the replicable core.** It needs only intraday bars and the Lee–Mykland test —
no characteristics — and it is a precise numeric target.

### Sample scale (Panel A)

| | |
|---|---|
| Period | Jan 2000 – Dec 2021 (252 months); **ours: Jan 2016 – Dec 2023** |
| Stocks per month | mean 1,336 (P25 1,156, P75 1,465) |
| HF observations per month | mean 364,139 |
| Total | 352,595 stock-months, **96,132,699** intraday returns |

### The tradeable claim

| | |
|---|---|
| Intraday arbitrage portfolio, annual Sharpe | **2.49** |
| Annual return | 32.66% |
| GFC (Oct 2007 – Mar 2009) | **+87.7%** vs S&P 500 −47.7% |
| COVID onset (Feb – Mar 2020) | +5.95% vs S&P 500 −19.9% |

### Table 2 — alpha against standard factor models

| Model | Monthly alpha | t-stat |
|---|---|---|
| CAPM | 2.92% | 11.92 |
| FF3 | 2.88% | 12.00 |
| FF3+UMD | 2.83% | 11.82 |
| FF5 | 2.77% | 10.97 |
| FF5+UMD | 2.75% | 10.94 |

Adjusted R² below 10% in every specification. Post-2008 subsample: alpha 1.19–1.26%
per month, t > 4.

### Robustness the paper reports (Internet Appendix IA.6)

Two of these matter a great deal for interpretation:

- **Estimation window**: Sharpe falls monotonically from **2.49** at one month to **0.33**
  at twelve. The signal is short-lived.
- **Rebalancing**: Sharpe **1.15–1.49** daily and **1.11–1.44** monthly — that is, under
  realistic trading the 2.49 becomes roughly 1.2–1.5.
- **Factor choice**: Sharpe 1.99–2.61 across `(K_C, K_J)` grids. Critically, *"Portfolios
  with K_J = 0 achieve slightly higher Sharpe ratios"* — **the jump decomposition does not
  improve the portfolio.** The authors are explicit that it separates risk components
  rather than adding performance.
- **Liquidity**: stricter screens give 1.79–2.26.

---

## 3. Data requirements

### 3.1 What the paper used

| Requirement | Source |
|---|---|
| 30-minute intraday returns, 6,600 US equities, 2000–2021 | **TAQ** (monthly product to 2014, daily millisecond after) |
| Daily/monthly returns, volume, market cap | **CRSP** |
| **153 firm characteristics per firm per month, lagged 1 month** | **Jensen, Kelly & Pedersen (2023)** |
| Fama-French factors | WRDS |

### 3.2 What we can serve

| Requirement | Our source | Status |
|---|---|---|
| 30-minute intraday returns | Alpaca `30Min`, `session=regular` — **13 bars/day aligned exactly to 09:30**, giving 12 intraday returns | **available** |
| Daily prices and volume for screening | Alpaca `1Day` | available |
| Market benchmark | SPY | available |
| **Firm characteristics** | — | **NOT AVAILABLE** |
| Fama-French / momentum factors | — | not available (WRDS-only); CAPM against SPY substitutes |

### 3.3 The blocker, stated plainly

**We have no firm-characteristic panel and cannot construct one.** The Jensen et al.
(2023) dataset is 153 attributes per firm per month built from CRSP and Compustat. This
repo has neither. The Robinhood MCP exposes `get_equity_fundamentals`, but that is a
current snapshot, not a historical monthly panel — and using it for bulk history would
violate the repo's routing rule anyway.

Since `X` appears in every one of Steps 2, 3 and 4, the paper's actual method cannot be
run here. What follows is therefore **not a replication of the paper's estimator**. It is
a replication of the paper's *architecture* with the projection removed:

| Paper | Here | Consequence |
|---|---|---|
| Projected PCA: betas = `X · Θ_β` | **Plain PCA** on `R_C` and `R_J` | This is Pelger (2020), the predecessor the paper explicitly builds on and claims to improve upon. |
| Mispricing `α = X · Θ_α` | **Per-stock intercept** orthogonal to the estimated factors | No characteristic smoothing, so noisier — biased *against* finding a clean signal. |
| Weights `w = (1/N) X Θ_α` | `w ∝ α̂`, scaled to 10% annualised vol | Same construction, unsmoothed input. |

This makes the replication a **direct test of the paper's central claim**: if the
characteristic projection is what generates the 2.49 Sharpe, the unprojected version
should be materially worse. If the unprojected version performs comparably, then the
performance comes from the jump/diffusive decomposition and the alpha estimate — not from
the characteristics — and the paper's headline contribution is doing less work than
claimed.

Either outcome is informative, and both are honest. Neither is "we reproduced the paper".

### 3.4 The other gaps

| Gap | Consequence |
|---|---|
| **History starts 2016-01-04**, paper starts 2000. | 16 of 22 years lost, including the GFC — so the paper's most striking result (+87.7% through the crisis) **cannot be checked at all**. |
| **Survivorship.** Alpaca's asset master is today's tradable list. | Our 500 names all survived to 2026. The paper's sample is delisting-inclusive. Biases results **upward**. |
| **Cross-section is 100, not 1,336/month.** | PCA factor estimates are noisier; the paper's asymptotics are in `N → ∞`. Forced by throughput — see 3.5. |
| **Large caps only.** | The paper's median firm is $1.27B market cap; ours are far larger. Less volatile stocks jump less, so RV, JV and jump counts all come in low. |
| **No Fama-French factors.** | Alpha is reported against CAPM (SPY) only; FF3/FF5 rows of Table 2 cannot be reproduced. |

### 3.5 Scale, and the throughput wall

A 500-name screen was built and then abandoned, for a measured reason worth recording:

```
              30Min request:    822 bars/s
              1Min  request: 13,932 bars/s      (same span, same 25 symbols)
```

Alpaca aggregates intraday bars **server-side from trades**, so a request costs roughly
the same wall time regardless of how many bars come back. A 500-name, 10-year pull at
30-minute resolution projected to **~12 hours**, and fetching 1-minute bars to aggregate
locally is no faster for the same span.

The panel is therefore built by **resampling 1-minute bars already cached** in this repo
(from the `momentum_orb` study) — exact, instant, and capped at those 100 symbols:

```
100 symbols x 2,012 sessions x 13 bars  =  3.7M bars
                                        =  2.3M intraday returns
```

against the paper's 96.1M. Per *month* the panel is 100 x ~250 intervals where the paper
has 1,336 x ~364 — so roughly a thirteenth of the cross-section, which is exactly the
dimension the estimator's consistency depends on.

---

## 4. Implementation notes

- **Lee–Mykland (2008).** Test statistic `|r_t| / (σ̂_t √Δt)` where `σ̂_t` is a local
  bipower-variation volatility estimate over a trailing window. Under the null of no
  jump the maximum over `n` observations follows a Gumbel law; the paper uses a
  **conservative 5% tail of the Gumbel distribution**. Bipower variation is used
  precisely because it is robust to the jumps being detected.
- **Overnight returns are excluded** — the method assumes a continuous-time diffusion
  within the session. The first bar of each day therefore yields no return.
- **Causality.** Weights are estimated on month `m` and held through month `m+1`, rolled
  forward. No look-ahead. The estimation month's data never overlaps the holding month.
- **Volatility scaling.** `w` is scaled so the portfolio has 10% annualised volatility
  *in the estimation month* — using realised vol from month `m`, never `m+1`.
- **The 25th-percentile volume screen** is applied monthly and re-evaluated each month,
  as in the paper.
- **Pseudo-R²** is computed without an intercept, as the ratio of systematic to total
  variation in the projected returns.


---

## 5. Layout

| File | Role |
|---|---|
| `select_universe.py` | liquidity screen (superseded by the throughput wall; kept for reproducibility) |
| `prepare_panel.py` | builds the 30-minute panel by resampling cached 1-minute bars |
| `hf.py` | the engine — Lee-Mykland jumps, PCA factors, mispricing, portfolio |
| `test_hf.py` | unit tests; the volatility look-ahead and overnight exclusion are pinned here |
| `run_backtest.py` | every variant, writing parquet to `out/hf_risk_mispricing/` |
| `hf_risk_mispricing.ipynb` | the replication, charts, assumptions, pros/cons, live requirements |
| [`executable/hf_risk_mispricing.md`](executable/hf_risk_mispricing.md) | **the runbook** — why this is not implementable here, and what to keep |

```bash
source .venv/bin/activate
python -m pytest research/hf_risk_mispricing/test_hf.py -q
python research/hf_risk_mispricing/prepare_panel.py
python research/hf_risk_mispricing/run_backtest.py
```

This is the **historical** lane — Alpaca, read-only.
