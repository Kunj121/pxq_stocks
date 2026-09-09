# Execution protocol

The mandatory MCP call sequence. No order is placed outside this flow.

---

## Equity order

### 1 — Resolve the account
```
get_accounts
```
- Never hardcode or guess an account number.
- Select by `brokerage_account_type`: `ira_roth` · `ira_traditional` · `individual`.
- **Check `agentic_allowed`. If `false`, stop.** That account is not tradeable here.
- Pass the full number to tools; show the user only the last 4 (`••••2690`).

### 2 — Establish the price
```
get_equity_quotes  symbols=["AAPL"]
```
Never size an order off a remembered, cached, or estimated price. Re-fetch.
Add `get_equity_fundamentals` / `get_equity_technical_indicators` if the thesis needs them.

### 3 — Check buying power
```
get_portfolio
get_equity_positions
```
Confirm the cash exists and know the existing position before adding to it.

### 4 — Local pre-flight
```bash
python guardrails.py --symbol AAPL --side buy --qty 2 --price <price from step 2>
```
A non-zero exit means the order is rejected locally. Do not proceed.

### 5 — Review with Robinhood
```
review_equity_order
```
**Mandatory.** Never skip to placing. The review is what surfaces Robinhood's own
rejections, estimated cost, and fee treatment.

### 6 — Human confirmation
Show the user the full review output — symbol, side, quantity, order type, limit price,
estimated total, account — and get an **explicit yes**. Silence is not consent. A prior
approval of a different order is not consent for this one.

### 7 — Place
```
place_equity_order
```
Immediately after, record it:
```bash
python journal.py add --symbol AAPL --side buy --qty 2 --price 227.50 \
    --order-id <id from response> --account-last4 2690
```

### 8 — Confirm the fill
```
get_equity_orders
```
Report actual fill state back. A placed order is not a filled order — do not tell the
user they own something until the fill is confirmed.

---

## Option order

Same spine, with contract resolution inserted before pricing.

### 1 — Account
```
get_accounts        # agentic_allowed must be true
```

### 2 — Resolve the contract
```
get_option_chains        # available expirations and strikes
get_option_instruments   # the specific contract id
```
Never construct an option symbol by hand. Resolve it.

### 3 — Price it
```
get_option_quotes        # bid/ask, IV, greeks
```
Check the bid/ask spread before using a market order. Wide spreads → limit order.

### 4 — Local pre-flight
```bash
python guardrails.py --symbol AAPL --side buy --qty 1 --price <premium> --multiplier 100
```
`--multiplier 100` matters: one contract at $2.30 is $230 of notional, not $2.30.

### 5 — Review
```
review_option_order
```

### 6 — Human confirmation
Show contract, expiration, strike, right (call/put), side, quantity, premium, total cost.

### 7 — Place
```
place_option_order
```
Then journal it with `--multiplier 100`.

### 8 — Confirm
```
get_option_orders
```

---

## Cancels

```
get_equity_orders / get_option_orders    # find the live order id
cancel_equity_order / cancel_option_order
```
Cancels do not need pre-approval — reducing exposure is always permitted.

---

## The four invariants

1. **Review before place.** `review_*` runs before every `place_*`, every time.
2. **Explicit human confirmation** between review and place. No silent fills.
3. **`agentic_allowed: true` only.** Never order in an account flagged `false`.
4. **Options resolve first.** `get_option_chains` → `get_option_instruments` before
   quoting or ordering.

---

## Reporting conventions

- `symbols` — uppercase array: `["AAPL", "MSFT"]`
- `bounds` — `regular` | `trading` | `extended` | `24_5`
- `interval` — `5minute` | `10minute` | `hour` | `day` | `week`
- `span` — `day` | `week` | `month` | `3month` | `year` | `5year`
- `null` → **N/A**, never `0`
- `not_found` symbols → "no data for X" (delisted or invalid)
- `financial_status_indicator` never shown without `financial_status_description`
- Account numbers masked to last 4 in all user-facing output
