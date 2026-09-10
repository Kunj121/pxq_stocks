# High-frequency risk decomposition — executable spec

**This is the runbook, not the research.** The study is in
[`../hf_risk_mispricing.md`](../hf_risk_mispricing.md) and
[`../hf_risk_mispricing.ipynb`](../hf_risk_mispricing.ipynb).

> **Nothing here is authorised to run live.** No order without explicit human
> confirmation — [`execution/PROTOCOL.md`](../../../execution/PROTOCOL.md).

---

## 1. Verdict

**Not implementable from this account, and not close.** Unlike the previous two studies,
the blocker is not cost or edge decay — it is that the strategy is a different category of
thing. It needs institutional data subscriptions, a prime broker, and seven figures before
the per-name notional stops being rounding error.

| Requirement | Paper | Here |
|---|---|---|
| Intraday data | TAQ, 2000–2021 | Alpaca SIP from 2016 ✓ |
| Firm characteristics | Jensen et al. (2023), 153 attributes/firm/month | **none** ✗ |
| Cross-section | 1,336 firms/month | 100 ✗ |
| Positions | several hundred, long **and** short, dollar-neutral | cash account, no shorting ✗ |
| Rebalancing | monthly, full book | feasible ✓ |
| Capital | institutional | $282 ✗ |

### What the replication found

| | Paper | This replication |
|---|---|---|
| Arbitrage portfolio Sharpe | **2.49** | **0.79** |
| Annual return | 32.66% | 8.5% |
| CAPM monthly alpha | 2.92% (t = 11.9) | **0.82% (t = 2.5)** |
| CAPM beta | −0.117 | **−0.094** |

The alpha is **real and statistically significant** but a quarter the size. Beta lands
almost exactly on the paper's, which is a good sign the portfolio construction is right.

Three explanations, and they cannot be separated with this data: no characteristic
projection, a cross-section a thirteenth of the paper's, and a period excluding the GFC
that contributed a large share of the paper's cumulative return.

---

## 2. What is worth keeping

The **jump/diffusion decomposition** is the reusable asset, independent of the strategy:

- `research/hf_risk_mispricing/hf.py` implements Lee & Mykland (2008) on any intraday
  panel — trailing bipower volatility, Gumbel critical value, overnight excluded.
- It reproduces the *shape* of the paper's variance decomposition, with levels running
  low exactly as a large-cap-only sample should.
- It is cheap: 2.3M intraday returns processed in seconds, from bars already on disk.

Concretely useful without any of the portfolio machinery:

1. **Jump-aware volatility.** Realized variance overstates diffusive risk on names that
   jump; CV is the better input to a position-sizing rule.
2. **Event detection.** A detected jump is a timestamped, statistically-tested surprise —
   usable as a filter or a trigger elsewhere in this repo.
3. **Regime measurement.** Market-wide jump intensity is a cheap stress indicator.

---

## 3. If it were ever run

### Data gates
- [ ] A **firm-characteristic panel** (Compustat + CRSP or vendor equivalent). Without it
      this is a different estimator, not the paper's.
- [ ] **Cross-section of 1,000+ names** with live intraday data. Note the measured
      throughput wall: 822 bars/s for server-aggregated 30-minute bars, which is what
      capped this study at 100 names.
- [ ] Fama-French factors for evaluation.

### Execution gates
- [ ] Margin account with **short availability across the whole cross-section** — the
      book is dollar-neutral, so roughly half is short.
- [ ] Automated monthly rebalancing of several hundred positions.
- [ ] **A cost model.** The paper has none at all. At this book size and turnover, costs
      are a first-order term, not a correction.

### Capital
- The paper targets 10% annualised volatility, so leverage is modest — but a
  several-hundred-name book needs seven figures before per-name notional is meaningful.

---

## 4. Guardrails

`execution/guardrails.py` caps notional at $500 and 10 orders/day. A several-hundred-name
monthly rebalance is orders of magnitude outside both. They would not be "raised" for this
strategy; they would be irrelevant to it, which is itself the signal that this is not a
strategy for this account.

---

## 5. Honest summary

The paper's variance decomposition replicates in structure. Its portfolio produces a
significant but much smaller alpha once the characteristic projection is removed — which
is consistent with the paper's central claim that the characteristics do real work, though
a thin cross-section and a shorter, calmer period are equally consistent with the gap.

Worth noting the authors' own robustness table: setting `K_J = 0` performs at least as
well in their data, meaning the jump decomposition — the paper's methodological
centrepiece — does not improve the portfolio there. **We find the opposite**, with jump
factors helping at every `K_C`. Neither result settles it, and both are in the notebook.

**Recommended action: keep `hf.py` as jump-detection infrastructure. Do not attempt the
portfolio.**
