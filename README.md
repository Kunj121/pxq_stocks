# pxq_stocks

Agentic trading workspace. Claude Code drives **Robinhood via MCP** for all market data
and order flow; the local Python venv is for analysis, backtesting, and charting on the
side.

---

## Setup

```bash
# already done once — repeat only on a fresh clone
python3.13 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Verify:

```bash
source .venv/bin/activate
python -c "import pandas, numpy, scipy, matplotlib; print('ok')"
```

- Interpreter: **Python 3.13.11** (`.venv/`)
- `requirements.txt` — loose, edit this one
- `requirements.lock.txt` — exact pinned versions (`pip freeze`), for reproducing the env

PyCharm: *Settings → Project → Python Interpreter → Add → Existing → `.venv/bin/python`*

---

## Robinhood MCP

All market data and trading go through the Robinhood MCP server, **not** through
`robin_stocks`, `yfinance`, or scraped data. MCP is the source of truth.

### Connecting it

The server is **not currently registered** in this environment (`claude mcp list` shows
Supabase, Google Drive, Calendar, Gmail, Vercel — no Robinhood). Register it before
trading:

```bash
claude mcp list           # confirm what's connected
claude mcp add ...        # add the Robinhood server per its install docs
```

Once connected, the `mcp__robinhood-trading__*` tools appear and the bundled
`robinhood` skill picks them up automatically.

### Tool surface

| Area | Tools |
|---|---|
| Research | `get_equity_quotes`, `get_equity_fundamentals`, `get_equity_historicals`, `get_equity_technical_indicators`, `get_index_quotes`, `get_earnings_calendar`, `get_earnings_results`, `search` |
| Portfolio | `get_portfolio`, `get_equity_positions`, `get_option_positions`, `get_equity_tax_lots`, `get_realized_pnl`, `get_pnl_trade_history` |
| Orders | `review_equity_order`, `place_equity_order`, `get_equity_orders`, `cancel_equity_order` (+ `*_option_order` equivalents) |
| Options | `get_option_chains`, `get_option_instruments`, `get_option_quotes`, `get_option_historicals` |
| Scanner | `get_scans`, `create_scan`, `run_scan`, `update_scan_filters`, `update_scan_config` |
| Watchlists | `get_watchlists`, `get_watchlist_items`, `create_watchlist`, `add_to_watchlist`, `remove_from_watchlist`, `get_popular_watchlists` |

### Account resolution

`get_accounts` first, always — never hardcode an account number.

| Account | `brokerage_account_type` |
|---|---|
| Roth IRA | `ira_roth` |
| Traditional IRA | `ira_traditional` |
| Margin / Individual | `individual` |

Accounts are masked to the last 4 digits in conversation (`••••2690`) but passed to tools
in full.

---

## Order safety rules

These are non-negotiable and apply to every order:

1. **Review before place.** `review_equity_order` / `review_option_order` runs first,
   every time.
2. **Explicit human confirmation.** The review output is shown and confirmed before any
   `place_*` call. No silent fills.
3. **`agentic_allowed: true` only.** Accounts flagged `false` are off limits for orders.
4. **Options need contract resolution.** `get_option_chains` → `get_option_instruments`
   before quoting or ordering.

Guardrails in `.env` (read by your own scripts — the MCP server does not enforce them):

```
MAX_ORDER_NOTIONAL_USD=500
MAX_DAILY_ORDERS=10
ALLOW_LIVE_ORDERS=false
```

`.env` holds **no Robinhood credentials** — MCP handles its own auth.

---

## Conventions

- `symbols` — uppercase array: `["AAPL", "MSFT"]`
- `bounds` — `regular` | `trading` | `extended` | `24_5`
- historicals `interval` — `5minute` | `10minute` | `hour` | `day` | `week`
- historicals `span` — `day` | `week` | `month` | `3month` | `year` | `5year`
- `null` renders as **N/A**, never `0`
- symbols in `not_found` → "no data for X" (delisted or invalid)
- `financial_status_indicator` always shown with `financial_status_description`

---

## Layout

```
.
├── .venv/                  # Python 3.13 virtualenv (gitignored)
├── .env                    # guardrails, no secrets (gitignored)
├── .env.example            # template
├── data/                   # raw pulls, CSV/parquet (gitignored)
├── out/                    # charts, backtest results (gitignored)
├── notes/                  # research notes
├── main.py                 # entrypoint (still PyCharm boilerplate)
├── requirements.txt
├── requirements.lock.txt
└── ssrn-4729284.pdf        # reference paper
```
