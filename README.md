# pxq_stocks

Agentic trading workspace. Two lanes:

- **Live** — Claude Code drives **Robinhood via MCP** for real-time market data and
  order flow. Order execution is governed by [`execution/`](execution/README.md).
- **Historical** — [`backtest/`](backtest/README.md) pulls bars **solely from the
  Alpaca Market Data API** for simulation, metrics, and charting. Read-only.

---

## Routing — which backend serves what

**Historical → Alpaca. Live → Robinhood.** The two lanes never swap jobs.

| Intent | Backend | Entry point |
|---|---|---|
| Download bars, any timeframe, any history | **Alpaca** | `python -m backtest fetch` |
| Backtest, simulate, compute metrics | **Alpaca** | `python -m backtest run` |
| Live quote, fundamentals, technicals | **Robinhood MCP** | `mcp__robinhood-trading__*` |
| Portfolio, positions, buying power, P&L | **Robinhood MCP** | `mcp__robinhood-trading__*` |
| Review, place, or cancel an order | **Robinhood MCP** | [`execution/PROTOCOL.md`](execution/PROTOCOL.md) |

Why the split is not arbitrary:

- The Robinhood MCP **cannot** serve bulk history. `get_equity_historicals` has no
  `1minute` interval (floor is `5minute`), intraday spans are short, and a multi-year
  minute pull is hundreds of thousands of rows — a file, not a tool response.
- Alpaca **must not** serve anything live. This repo holds Alpaca *paper* keys, and
  Alpaca is a different venue with a different book — its quote is not the price you
  get filled at on Robinhood.

A backtest may **motivate** a trade. It may **never price** one. Every number that
reaches an order comes from a live MCP call made now.

This rule is enforced as a skill: [`.claude/skills/data-routing/SKILL.md`](.claude/skills/data-routing/SKILL.md).

---

## Output format — parquet

Every tabular artefact this repo writes is **parquet**. CSV is an opt-out, not the norm.

```bash
python -m backtest fetch --symbols META --start 2023-09-09 --timeframe 1Min
#   -> data/META_1Min.parquet

python -m backtest fetch ... --format csv        # deliberate opt-out
```

`--format {parquet,csv}` applies to `fetch` and to `run` (which writes `equity`, `fills`,
and `weights` in the report directory).

Why it matters on this data: META 1-minute over 3 years is **22 MB as CSV, 7.1 MB as
parquet**, and the CSV loses the timezone-aware index — `2023-09-11 09:30:00-04:00`
comes back as an object-dtype string with mixed EDT/EST offsets, breaking `.dt`
accessors. Parquet returns a real `America/New_York` index and the MultiIndex intact.

Write through the shared helper rather than calling pandas directly:

```python
from backtest.writers import write_frame, read_frame
write_frame(df, "data/META_1Min.parquet")
df = read_frame("data/META_1Min.parquet")
```

Enforced as a skill: [`.claude/skills/parquet-output/SKILL.md`](.claude/skills/parquet-output/SKILL.md).

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

### Connection

Registered and connected as `robinhood-trading` (`https://agent.robinhood.com/mcp/trading`).
Verify with:

```bash
claude mcp list
```

The `mcp__robinhood-trading__*` tools are live and the bundled `robinhood` skill picks
them up automatically.

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

## Execution — `execution/`

Order flow lives in [`execution/`](execution/README.md) and goes **strictly through the
Robinhood MCP server**. No `robin_stocks`, no `yfinance`, no Alpaca, no cached prices.

```bash
source .venv/bin/activate
cd execution

python guardrails.py --symbol AAPL --side buy --qty 2 --price 227.50
python journal.py add --symbol AAPL --side buy --qty 2 --price 227.50 \
    --order-id <id> --account-last4 2690
python journal.py today
```

[`execution/PROTOCOL.md`](execution/PROTOCOL.md) is the mandatory call sequence for
equity and option orders. Its four invariants:

1. `review_*` before every `place_*`
2. Explicit human confirmation between review and place
3. `agentic_allowed: true` accounts only
4. Options resolve via `get_option_chains` → `get_option_instruments` first

Python in `execution/` never touches the network — MCP tools are invoked by Claude, so
`guardrails.py` and `journal.py` only do local arithmetic and record-keeping on values
Claude already fetched.

---

## Backtesting — `backtest/`

Historical simulation lives in [`backtest/`](backtest/README.md) and pulls market
data **solely from the Alpaca Market Data API**. Not Robinhood, not yfinance, not
scraped files. It reads only — it places no orders.

```bash
source .venv/bin/activate
python -m backtest check                     # credentials + connectivity
python -m backtest strategies                # what's available
python -m backtest run --symbols AAPL,MSFT,NVDA \
    --start 2019-01-01 --strategy sma_cross \
    --param fast=20 --param slow=100 --benchmark SPY
```

Credentials come from `~/.claude/mcp-servers/alpaca.env` — the same file the
Alpaca MCP server reads — so the two stay on one set of keys. Nothing secret
enters this repo. See [`backtest/README.md`](backtest/README.md) for the data
model, the strategy interface, the cost model, and the **known limitations**
(survivorship bias, 2016 history floor, optimistic fills).

The division of labour: **MCP servers for live research and order flow**,
**`backtest/` for historical simulation in code**.

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
├── .claude/skills/         # project skills (routing, parquet output)
├── execution/              # Robinhood-MCP-only order execution
│   ├── README.md           #   the MCP-only contract — start here
│   ├── PROTOCOL.md         #   mandatory order call sequence
│   ├── guardrails.py       #   local pre-flight checks
│   ├── journal.py          #   append-only trade journal
│   └── logs/               #   trades.jsonl (gitignored)
├── backtest/               # Alpaca-only data + backtesting package
│   ├── README.md           #   full guide — start here
│   ├── config.py           #   credential resolution
│   ├── client.py           #   the only module that hits the network
│   ├── data.py             #   bars -> pandas, on-disk cache
│   ├── engine.py           #   portfolio simulation
│   ├── metrics.py          #   Sharpe, drawdown, beta, ...
│   ├── strategies/         #   Strategy interface + 3 examples
│   ├── report.py           #   chart + markdown + CSV output
│   ├── cli.py              #   python -m backtest
│   └── tests/              #   71 offline tests
├── data/                   # raw pulls + bar cache (gitignored)
├── out/                    # charts, backtest results (gitignored)
├── notes/                  # research notes
├── main.py                 # entrypoint (still PyCharm boilerplate)
├── requirements.txt
├── requirements.lock.txt
└── ssrn-4729284.pdf        # reference paper
```
