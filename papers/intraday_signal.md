# Intraday Option Entry Framework — 1-Hour Regime + Pullback Pricing

## Objective

Use the primary signal to determine whether there is a **buy or sell opportunity**.

Use the Bollinger Band / pullback framework to determine whether the underlying is at a relatively favorable location to buy the option, rather than entering after an extended directional move.

> A lower Bollinger Band touch is a **setup condition**, not an automatic buy signal.

---

## 1. Hourly Bullish Regime Filter

Only consider long calls when the underlying satisfies the broader bullish regime.

\[
\begin{aligned}
&C_d > SMA_{50,d} > SMA_{200,d} \\
&P_t > VWAP_t \\
&EMA_{20}^{1h} > EMA_{50}^{1h} \\
&50 \le RSI_{14}^{1h} \le 70 \\
&ADX_{14}^{1h} \ge 20 \\
&+DI_{14}^{1h} > -DI_{14}^{1h} \\
&RVOL_{\text{TOD}} \ge 1.5
\end{aligned}
\]

### Interpretation

| Filter | Purpose |
|---|---|
| Daily close > SMA50 > SMA200 | Restricts trades to a higher-timeframe bullish trend |
| Price > session VWAP | Confirms positive intraday session bias |
| Hourly EMA20 > EMA50 | Confirms intermediate intraday trend alignment |
| Hourly RSI(14) between 50 and 70 | Positive momentum without requiring an excessively extended move |
| Hourly ADX(14) ≥ 20 | Confirms a developing or established trend |
| +DI > −DI | Adds directional confirmation to ADX |
| Time-of-day-adjusted RVOL ≥ 1.5 | Requires above-normal participation for that time of day |

---

## 2. Bollinger Band Price-Location Filter

Calculate hourly Bollinger Bands using the standard configuration:

\[
\begin{aligned}
M_t &= SMA_{20,t} \\
U_t &= M_t + 2\sigma_{20,t} \\
L_t &= M_t - 2\sigma_{20,t}
\end{aligned}
\]

Where:

- \(M_t\) is the middle Bollinger Band
- \(U_t\) is the upper Bollinger Band
- \(L_t\) is the lower Bollinger Band
- \(\sigma_{20,t}\) is the standard deviation of the previous 20 hourly closes

Use Bollinger %B to normalize the price location:

\[
\%B_t = \frac{P_t-L_t}{U_t-L_t}
\]

| %B value | Interpretation |
|---:|---|
| 1.00 | Price is at the upper Bollinger Band |
| 0.50 | Price is at the middle band / 20-hour SMA |
| 0.00 | Price is at the lower Bollinger Band |
| Below 0.00 | Price is below the lower Bollinger Band |
| Above 1.00 | Price is above the upper Bollinger Band |

---

## 3. Preferred Long-Call Entry Zones

| Setup type | Hourly %B zone | Interpretation | Action |
|---|---:|---|---|
| Shallow pullback | 0.40–0.60 | Pullback toward the middle band / rising hourly mean | Preferred in a strong trend |
| Standard pullback | 0.20–0.40 | Price trades in the lower portion of its recent distribution | Look for confirmation |
| Deep pullback | 0.00–0.20 | Price approaches or tests the lower Bollinger Band | Require a reclaim and reversal |
| Breakdown risk | Below 0.00 for multiple bars | Price is persistently below the lower band | Avoid buying calls until structure recovers |

### Default pullback rule

```text
Hourly bullish regime = TRUE
AND hourly %B <= 0.40
AND hourly SMA20 / Bollinger middle band is flat or rising
AND hourly EMA20 remains above EMA50
THEN activate lower-timeframe entry monitoring
```

---

## 4. Do Not Buy on the First Lower-Band Touch

Do not enter the call simply because:

```text
Price <= lower Bollinger Band
```

A lower-band touch can represent:

- A normal pullback within an uptrend
- A volatility expansion
- A failed breakout
- A trend reversal
- A continuation of downside momentum

Instead, wait for price to stabilize and confirm that buyers have returned.

---

## 5. Lower-Timeframe Confirmation

Once the hourly pullback condition is active, use a 5-minute or 15-minute chart for entry confirmation.

Buy only when at least one confirmation occurs:

```text
1. Price forms a higher low.

2. Price closes back above VWAP after testing or briefly losing VWAP.

3. Price closes back above the hourly lower Bollinger Band.

4. Price breaks above the local pullback high.

5. A bullish reversal candle forms and price breaks its high.

6. Bar-level relative volume expands during the reversal.
```

### Example long-call trigger

```text
Hourly trend is bullish
AND hourly %B <= 0.40
AND price is near VWAP, hourly EMA20, or prior breakout support
AND 15-minute chart forms a higher low
AND 15-minute price closes above the local pullback high
AND 15-minute relative volume is above normal
THEN buy call, subject to option-quality filters
```

---

## 6. Deep Pullback / Lower-Band Reclaim Setup

Use this stricter rule if the goal is to buy a deeper discount.

\[
\begin{aligned}
&\text{Hourly bullish regime is active} \\
&\%B^{1h} \le 0.15 \\
&P_t \text{ touches or penetrates the hourly lower band} \\
&P_t \text{ subsequently closes back above } L_t \\
&\text{Lower-timeframe reversal confirmation occurs}
\end{aligned}
\]

```text
Do not buy the call while price is still falling through the lower band.

Buy only after price reclaims the lower band and breaks a lower-timeframe reversal level.
```

---

## 7. VWAP and Trend-Integrity Rules

For long calls, classify the pullback as follows:

| Underlying behavior | Interpretation | Action |
|---|---|---|
| Pullback holds above VWAP | Strong trend continuation setup | Highest-quality long-call candidate |
| Pullback dips below VWAP but promptly reclaims it | Potentially valid pullback | Wait for reclaim confirmation |
| Price remains below VWAP and VWAP slopes down | Weakening intraday structure | Avoid or require stronger confirmation |
| Hourly EMA20 falls below EMA50 | Trend deterioration | Do not take continuation calls |
| Hourly Bollinger middle band slopes down | Pullback may be a regime shift rather than value | Avoid lower-band dip buying |

---

## 8. Option-Contract Quality Gate

A good stock entry is not automatically a good option purchase.

Before buying the call, evaluate the specific contract.

\[
\mathrm{Mid} = \frac{\mathrm{Bid}+\mathrm{Ask}}{2}
\]

### Initial contract rules

```text
0.40 <= Delta <= 0.70

Bid-ask spread / midpoint <= 5% to 10%

Adequate option volume and displayed quote size

Implied volatility is not extreme relative to its own recent history

Use a limit order near midpoint; do not automatically lift the ask
```

### Avoid contracts with

- Wide bid-ask spreads
- Sparse displayed size
- Low liquidity relative to your intended position size
- Extreme IV after a catalyst, unless the expected underlying move justifies it
- Far out-of-the-money strikes when the forecast is a moderate continuation rather than an explosive move
- Very short-dated options when the expected move is too slow to overcome theta and transaction costs

---

## 9. Underlying-Based Invalidation

Stops and invalidation should be defined using the **underlying**, not only the option premium.

Potential invalidation conditions:

```text
Price breaks and closes below the pullback low

Price fails a VWAP reclaim and remains below VWAP

Hourly EMA20 loses slope and price remains below it

Hourly EMA20 crosses below EMA50

Hourly Bollinger middle band turns materially downward

Hourly %B remains below 0.00 across multiple bars
```

```text
Do not average down solely because the option premium has fallen.

A cheaper call is not necessarily a better call if the underlying trend has failed.
```

---

## 10. Final Combined Long-Call Rule

### Regime filter

\[
\begin{aligned}
&C_d > SMA_{50,d} > SMA_{200,d} \\
&P_t > VWAP_t \\
&EMA_{20}^{1h} > EMA_{50}^{1h} \\
&50 \le RSI_{14}^{1h} \le 70 \\
&ADX_{14}^{1h} \ge 20 \\
&+DI_{14}^{1h} > -DI_{14}^{1h} \\
&RVOL_{\mathrm{TOD}} \ge 1.5
\end{aligned}
\]

### Better-price setup

\[
\%B^{1h} \le 0.40
\]

### Confirmation

```text
Price holds or reclaims VWAP, EMA20, prior breakout support, or the lower Bollinger Band

AND

A 5-minute or 15-minute chart confirms a higher low, reversal, or break above the pullback high
```

### Option execution

```text
Buy an ATM or modestly ITM call with approximately 0.40–0.70 delta

AND

Use a limit order near the midpoint

AND

Reject the trade if the bid-ask spread, IV, or liquidity makes the contract unattractive
```

---

## Key Principle

```text
The primary signal answers: “Should I be bullish or bearish?”

The hourly trend filter answers: “Is this trade direction supported by the current regime?”

The Bollinger Band condition answers: “Has price pulled back enough to offer a better location?”

The lower-timeframe confirmation answers: “Has the pullback likely ended?”

The option-quality filter answers: “Is the option contract reasonably priced and executable?”
```