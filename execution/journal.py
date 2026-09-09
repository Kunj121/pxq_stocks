"""Append-only trade journal.

Records orders after they are placed via the Robinhood MCP server. Touches no
network — every value is passed in from an MCP tool response. See README.md.

    python journal.py add --symbol AAPL --side buy --qty 2 --price 227.50 \
        --order-id abc-123 --account-last4 2690
    python journal.py today
    python journal.py summary
"""

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

LOG_DIR = Path(__file__).resolve().parent / "logs"
LOG_FILE = LOG_DIR / "trades.jsonl"


def _load():
    """Read every journal entry. Returns [] if the journal does not exist yet."""
    if not LOG_FILE.exists():
        return []
    entries = []
    for lineno, line in enumerate(LOG_FILE.read_text().splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        try:
            entries.append(json.loads(line))
        except json.JSONDecodeError:
            print(f"  ! skipping malformed line {lineno} in {LOG_FILE.name}")
    return entries


def _today_str():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def count_today():
    """Number of orders journaled today (UTC). Used by guardrails.py."""
    today = _today_str()
    return sum(1 for e in _load() if e.get("ts", "").startswith(today))


def add(args):
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    notional = args.qty * args.price * args.multiplier
    entry = {
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "symbol": args.symbol.upper(),
        "side": args.side,
        "qty": args.qty,
        "price": args.price,
        "multiplier": args.multiplier,
        "notional": round(notional, 2),
        "order_id": args.order_id,
        "account_last4": args.account_last4,
        "note": args.note,
    }
    with LOG_FILE.open("a") as f:
        f.write(json.dumps(entry) + "\n")
    print(f"logged  {entry['side']} {entry['qty']:g} {entry['symbol']} "
          f"@ ${entry['price']:,.2f}  =  ${notional:,.2f}")
    print(f"        order {entry['order_id']}  account ••••{entry['account_last4']}")
    print(f"        -> {LOG_FILE}")
    print()
    print("Confirm the fill with get_equity_orders / get_option_orders — "
          "a placed order is not a filled order.")


def _print_table(entries):
    if not entries:
        print("no entries")
        return
    print(f"{'timestamp':20}  {'sym':6} {'side':4} {'qty':>7} "
          f"{'price':>10} {'notional':>11}  order")
    print("-" * 82)
    for e in entries:
        print(
            f"{e.get('ts','?')[:19]:20}  {e.get('symbol','?'):6} "
            f"{e.get('side','?'):4} {e.get('qty',0):>7g} "
            f"${e.get('price',0):>9,.2f} ${e.get('notional',0):>10,.2f}  "
            f"{e.get('order_id','?')}"
        )


def today(args):
    entries = [e for e in _load() if e.get("ts", "").startswith(_today_str())]
    _print_table(entries)
    if entries:
        total = sum(e.get("notional", 0) for e in entries)
        print("-" * 82)
        print(f"{len(entries)} order(s) today, ${total:,.2f} total notional")


def summary(args):
    entries = _load()
    if not entries:
        print("journal is empty")
        return

    by_symbol = {}
    for e in entries:
        s = by_symbol.setdefault(
            e.get("symbol", "?"), {"orders": 0, "bought": 0.0, "sold": 0.0}
        )
        s["orders"] += 1
        s["bought" if e.get("side") == "buy" else "sold"] += e.get("notional", 0)

    print(f"{'symbol':8} {'orders':>7} {'bought':>13} {'sold':>13} {'net':>13}")
    print("-" * 58)
    for sym in sorted(by_symbol):
        s = by_symbol[sym]
        net = s["bought"] - s["sold"]
        print(f"{sym:8} {s['orders']:>7} ${s['bought']:>12,.2f} "
              f"${s['sold']:>12,.2f} ${net:>12,.2f}")
    print("-" * 58)
    print(f"{len(entries)} order(s) total, first {entries[0].get('ts','?')[:10]}")
    print()
    print("Notional flow only — not P&L. For realized P&L use the MCP tools "
          "get_realized_pnl / get_pnl_trade_history.")


def main():
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = p.add_subparsers(dest="cmd", required=True)

    a = sub.add_parser("add", help="record a placed order")
    a.add_argument("--symbol", required=True)
    a.add_argument("--side", required=True, choices=["buy", "sell"])
    a.add_argument("--qty", required=True, type=float)
    a.add_argument("--price", required=True, type=float)
    a.add_argument("--multiplier", type=float, default=1,
                   help="100 for option contracts, 1 for equities (default)")
    a.add_argument("--order-id", required=True,
                   help="id returned by place_equity_order / place_option_order")
    a.add_argument("--account-last4", required=True,
                   help="last 4 digits only — never journal a full account number")
    a.add_argument("--note", default="")
    a.set_defaults(func=add)

    sub.add_parser("today", help="orders journaled today").set_defaults(func=today)
    sub.add_parser("summary", help="all-time notional by symbol").set_defaults(
        func=summary
    )

    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
