---
name: data-routing
description: Routes market-data and trading requests to the correct backend in pxq_stocks — Alpaca for all historical data and backtesting, Robinhood MCP for all live quotes and order execution. Use whenever a request involves downloading bars, fetching prices, backtesting a strategy, checking a portfolio, or placing/reviewing/cancelling an order.
---

# Data routing: Alpaca vs Robinhood

This repo has two lanes and they never swap jobs.

| Intent | Backend | Entry point |
|---|---|---|
| Download bars, any timeframe, any history | **Alpaca** | `python -m backtest fetch` |
| Backtest, simulate, compute metrics | **Alpaca** | `python -m backtest run` |
| Live quote, fundamentals, technicals | **Robinhood MCP** | `mcp__robinhood-trading__*` |
| Portfolio, positions, buying power, P&L | **Robinhood MCP** | `mcp__robinhood-trading__*` |
| Review, place, or cancel an order | **Robinhood MCP** | `execution/PROTOCOL.md` |

## The one-line rule

> **Historical → Alpaca. Live → Robinhood.**
> Anything that reads the past is `backtest/`. Anything that touches the present or
> moves money is the Robinhood MCP.

## Historical → Alpaca

```bash
source .venv/bin/activate
python -m backtest check                      # credentials + connectivity
python -m backtest fetch --symbols META --start 2023-09-09 \
    --timeframe 1Min --session regular       # -> data/META_1Min.parquet
python -m backtest run --symbols AAPL,MSFT --start 2019-01-01 \
    --strategy sma_cross --param fast=20 --param slow=100 --benchmark SPY
```

- Timeframes: `1Min`, `5Min`, `15Min`, `1Hour`, `1Day`, `1Week`, …
- `--session regular` (09:30–16:00 ET, 390 bars/day) or `extended` (04:00–20:00 ET)
- Credentials resolve from `~/.claude/mcp-servers/alpaca.env`. Never hardcode them.
- Bars cache under `data/alpaca_cache/`; `--no-cache` forces a fresh pull.
- Output is **parquet** by default — see
  [`parquet-output`](../parquet-output/SKILL.md). `--format csv` opts out.
- This lane **never places an order**. It only reads.

**Do not** use the Robinhood MCP for bulk history. `get_equity_historicals` has no
`1minute` interval (its floor is `5minute`), its intraday spans are short, and a
multi-year minute pull is hundreds of thousands of rows — a file, not a tool response.

## Live → Robinhood MCP

Always call an MCP tool rather than reasoning from memory, a cached number, or a CSV.

```
get_accounts            # first, always — resolve the account, check agentic_allowed
get_equity_quotes       # current price
get_portfolio           # buying power
review_equity_order     # mandatory before placing
place_equity_order      # only after explicit human confirmation
```

Full sequence for equities and options: [`execution/PROTOCOL.md`](../../../execution/PROTOCOL.md).

The four invariants:

1. `review_*` before every `place_*`
2. Explicit human confirmation between review and place
3. `agentic_allowed: true` accounts only
4. Options resolve `get_option_chains` → `get_option_instruments` first

**Do not** use Alpaca for anything live. The repo holds Alpaca *paper* trading keys, and
Alpaca is a different venue with a different book — its quote is not the price you will
be filled at on Robinhood.

## The boundary that matters

A backtest may **motivate** a trade. It may **never price** one.

Every number that reaches an order — quote, size, buying power, existing position —
comes from a live Robinhood MCP call made now. Alpaca bars are another venue's history,
adjusted differently, and as stale as the last cache write.

## Never

- `yfinance`, `robin_stocks`, scraped quotes, vendored price CSVs — in either lane
- Alpaca data as an input to a live order
- Robinhood MCP for bulk historical downloads
- Hardcoded prices, sizes, or account numbers
