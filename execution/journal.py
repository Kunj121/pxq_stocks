"""Append-only trade journal.

Records orders after they are placed via the Robinhood MCP server. Touches no
network — every value is passed in from an MCP tool response. See README.md.

Two files are written side by side in logs/:

    trades.jsonl   full record, one JSON object per order (guardrails reads this)
    trades.csv     date,time,symbol,ticker,side,price,qty,fees,strategy

    python journal.py add --symbol AAPL --side buy --qty 2 --price 227.50 \
        --order-id abc-123 --account-last4 2690 --strategy test
    python journal.py from-mcp            # hook mode: place_* payload on stdin
    python journal.py amend --order-id abc-123 --price 227.61 --fees 0
    python journal.py today
    python journal.py summary
"""

import argparse
import csv
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

LOG_DIR = Path(__file__).resolve().parent / "logs"
LOG_FILE = LOG_DIR / "trades.jsonl"
CSV_FILE = LOG_DIR / "trades.csv"

# Column order is fixed by the operator. date/time are local wall-clock — the
# same clock the trade was placed on — while the jsonl `ts` stays UTC.
CSV_COLUMNS = ["date", "time", "symbol", "ticker", "side",
               "price", "qty", "fees", "strategy"]

DEFAULT_STRATEGY = "test"


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


def _read_csv():
    if not CSV_FILE.exists():
        return []
    with CSV_FILE.open(newline="") as f:
        return list(csv.DictReader(f))


def _write_csv(rows):
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    with CSV_FILE.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=CSV_COLUMNS)
        w.writeheader()
        w.writerows(rows)


def _csv_row(entry):
    """Project a jsonl entry onto the operator's CSV schema."""
    local = datetime.fromisoformat(entry["ts"]).astimezone()
    return {
        "date": local.strftime("%Y-%m-%d"),
        "time": local.strftime("%H:%M:%S"),
        # symbol and ticker are the same value today; kept as separate columns
        # because the schema asks for both.
        "symbol": entry["symbol"],
        "ticker": entry["symbol"],
        "side": entry["side"],
        "price": f"{entry['price']:.4f}",
        "qty": f"{entry['qty']:g}",
        "fees": f"{entry.get('fees', 0.0):.2f}",
        "strategy": entry.get("strategy", DEFAULT_STRATEGY),
    }


def _append_csv(entry):
    rows = _read_csv()
    rows.append(_csv_row(entry))
    _write_csv(rows)


def _record(symbol, side, qty, price, order_id, account_last4,
            multiplier=1.0, fees=0.0, strategy=DEFAULT_STRATEGY, note="",
            state="", ts=None):
    """Append one order to both the jsonl journal and the CSV. Idempotent on
    order_id — a re-run (hook fires, then Claude journals by hand) updates the
    existing record instead of duplicating it."""
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    existing = {e.get("order_id") for e in _load()}
    if order_id and order_id in existing:
        return amend_entry(order_id, price=price, fees=fees, qty=qty,
                           strategy=strategy, state=state)

    entry = {
        "ts": ts or datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "symbol": symbol.upper(),
        "side": side,
        "qty": qty,
        "price": price,
        "multiplier": multiplier,
        "notional": round(qty * price * multiplier, 2),
        "fees": fees,
        "strategy": strategy,
        "order_id": order_id,
        "account_last4": account_last4,
        "state": state,
        "note": note,
    }
    with LOG_FILE.open("a") as f:
        f.write(json.dumps(entry) + "\n")
    _append_csv(entry)
    return entry


def add(args):
    entry = _record(
        symbol=args.symbol, side=args.side, qty=args.qty, price=args.price,
        order_id=args.order_id, account_last4=args.account_last4,
        multiplier=args.multiplier, fees=args.fees, strategy=args.strategy,
        note=args.note,
    )
    print(f"logged  {entry['side']} {entry['qty']:g} {entry['symbol']} "
          f"@ ${entry['price']:,.2f}  =  ${entry['notional']:,.2f}"
          f"  [{entry['strategy']}]")
    print(f"        order {entry['order_id']}  account ••••{entry['account_last4']}")
    print(f"        -> {LOG_FILE}")
    print(f"        -> {CSV_FILE}")
    print()
    print("Confirm the fill with get_equity_orders / get_option_orders — "
          "a placed order is not a filled order.")


def amend_entry(order_id, price=None, fees=None, qty=None, strategy=None,
                state=None):
    """Correct an already-journaled order in place, in both files.

    The hook records an order the instant it is placed, when average_price and
    fees are still unknown. This is how the real fill gets back into the log.
    """
    entries = _load()
    target = None
    for e in entries:
        if e.get("order_id") == order_id:
            target = e
    if target is None:
        sys.exit(f"no journal entry with order_id {order_id}")

    if price is not None:
        target["price"] = price
    if qty is not None:
        target["qty"] = qty
    if fees is not None:
        target["fees"] = fees
    if strategy is not None:
        target["strategy"] = strategy
    if state:
        target["state"] = state
    target["notional"] = round(
        target["qty"] * target["price"] * target.get("multiplier", 1), 2
    )

    with LOG_FILE.open("w") as f:
        for e in entries:
            f.write(json.dumps(e) + "\n")
    _write_csv([_csv_row(e) for e in entries])
    return target


def amend(args):
    entry = amend_entry(args.order_id, price=args.price, fees=args.fees,
                        qty=args.qty, strategy=args.strategy, state=args.state)
    print(f"amended {entry['order_id']}: {entry['side']} {entry['qty']:g} "
          f"{entry['symbol']} @ ${entry['price']:,.2f} "
          f"fees ${entry.get('fees', 0):,.2f} [{entry.get('strategy')}]")


def _find_order(node):
    """Walk an arbitrary MCP response and return the first dict that looks like
    an order. The hook payload nests the tool result differently depending on
    transport, so shape-match rather than assume a path."""
    if isinstance(node, str):
        try:
            return _find_order(json.loads(node))
        except (json.JSONDecodeError, ValueError):
            return None
    if isinstance(node, dict):
        if "symbol" in node and "side" in node and ("id" in node or "quantity" in node):
            return node
        for v in node.values():
            found = _find_order(v)
            if found:
                return found
    if isinstance(node, list):
        for v in node:
            found = _find_order(v)
            if found:
                return found
    return None


def _num(*candidates, nonzero=False):
    """First candidate that parses as a float. With nonzero=True, zeros are
    skipped too — a freshly placed order reports cumulative_quantity 0, which
    must not beat the quantity actually ordered."""
    for c in candidates:
        if c is None or c == "":
            continue
        try:
            v = float(c)
        except (TypeError, ValueError):
            continue
        if nonzero and v == 0:
            continue
        return v
    return None


def from_mcp(args):
    """Hook entry point: a PostToolUse payload for a place_*_order tool arrives
    on stdin. Log whatever the broker echoed back. Never raises — a journaling
    failure must not look like an order failure."""
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        return

    tool = payload.get("tool_name", "")
    if "place_" not in tool or "order" not in tool:
        return

    tool_input = payload.get("tool_input") or {}
    order = _find_order(payload.get("tool_response")) or {}
    pending_notional = None

    symbol = order.get("symbol") or tool_input.get("symbol")
    side = order.get("side") or tool_input.get("side")
    if not symbol or not side:
        return

    qty = _num(order.get("cumulative_quantity"), order.get("quantity"),
               tool_input.get("quantity"), nonzero=True)
    price = _num(order.get("average_price"), order.get("price"),
                 tool_input.get("limit_price"), nonzero=True)
    if qty is None or price is None:
        # A dollar-based market order carries neither until it fills; derive
        # what we can so the row still exists, and let `amend` fix it.
        notional = _num(order.get("dollar_based_amount"),
                        tool_input.get("dollar_amount"))
        if notional is None:
            return
        qty = qty or 0.0
        price = price or 0.0
        pending_notional = notional

    account = str(order.get("account_number")
                  or tool_input.get("account_number") or "")

    ts = None
    created = order.get("created_at")
    if created:
        try:
            ts = (datetime.fromisoformat(created.replace("Z", "+00:00"))
                  .astimezone(timezone.utc).isoformat(timespec="seconds"))
        except ValueError:
            ts = None

    entry = _record(
        symbol=symbol,
        side=side,
        qty=qty,
        price=price,
        order_id=order.get("id", ""),
        account_last4=account[-4:],
        multiplier=100.0 if "option" in tool else 1.0,
        fees=_num(order.get("fees")) or 0.0,
        strategy=args.strategy,
        state=order.get("state", ""),
        note=f"auto-journaled from {tool}",
        ts=ts,
    )
    if pending_notional is not None:
        # A dollar-based order has no share count or price until it fills.
        # Keep the notional so nothing is lost, and flag the row for amending.
        entry["notional"] = pending_notional
        amend_entry(entry["order_id"], state="pending-fill")
    # Surfaced to the user by the hook runner, and back into Claude's context.
    if pending_notional is not None:
        msg = (f"journaled {entry['side']} ${pending_notional:,.2f} of "
               f"{entry['symbol']} -> logs/trades.csv (price/qty unknown until "
               f"it fills: confirm with get_equity_orders, then "
               f"`journal.py amend --order-id {entry['order_id']} "
               f"--price <avg> --qty <filled> --fees <fees>`)")
    else:
        msg = (f"journaled {entry['side']} {entry['qty']:g} "
               f"{entry['symbol']} @ ${entry['price']:,.2f} -> logs/trades.csv")
    print(json.dumps({"systemMessage": msg}))


def _print_table(entries):
    if not entries:
        print("no entries")
        return
    print(f"{'timestamp':20}  {'sym':6} {'side':4} {'qty':>7} "
          f"{'price':>10} {'notional':>11}  {'strategy':10} order")
    print("-" * 96)
    for e in entries:
        print(
            f"{e.get('ts','?')[:19]:20}  {e.get('symbol','?'):6} "
            f"{e.get('side','?'):4} {e.get('qty',0):>7g} "
            f"${e.get('price',0):>9,.2f} ${e.get('notional',0):>10,.2f}  "
            f"{e.get('strategy','')[:10]:10} {e.get('order_id','?')}"
        )


def today(args):
    entries = [e for e in _load() if e.get("ts", "").startswith(_today_str())]
    _print_table(entries)
    if entries:
        total = sum(e.get("notional", 0) for e in entries)
        print("-" * 96)
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
    a.add_argument("--fees", type=float, default=0.0)
    a.add_argument("--strategy", default=DEFAULT_STRATEGY)
    a.add_argument("--order-id", required=True,
                   help="id returned by place_equity_order / place_option_order")
    a.add_argument("--account-last4", required=True,
                   help="last 4 digits only — never journal a full account number")
    a.add_argument("--note", default="")
    a.set_defaults(func=add)

    m = sub.add_parser("from-mcp",
                       help="hook mode: read a PostToolUse payload on stdin")
    m.add_argument("--strategy", default=DEFAULT_STRATEGY)
    m.set_defaults(func=from_mcp)

    d = sub.add_parser("amend", help="correct a journaled order with its fill")
    d.add_argument("--order-id", required=True)
    d.add_argument("--price", type=float)
    d.add_argument("--qty", type=float)
    d.add_argument("--fees", type=float)
    d.add_argument("--strategy")
    d.add_argument("--state")
    d.set_defaults(func=amend)

    sub.add_parser("today", help="orders journaled today").set_defaults(func=today)
    sub.add_parser("summary", help="all-time notional by symbol").set_defaults(
        func=summary
    )

    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
