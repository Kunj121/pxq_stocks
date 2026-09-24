"""Bring a Sweep Exit Lab export back into the repo.

The dashboard (out/dashboard, published as an artifact) exports one JSON object
per configuration: the exit rules, the active column filters, the summary, and
every signal that passed with its simulated entry, exit, return and exit reason.
It reaches this side one of three ways:

- **Download .json** on the page, then `python research/sweep_events/exit_lab.py <file>`.
- **Copy JSON**, paste into a file, same command.
- **Save to Claude** on the page writes the object to the artifact's `configs`
  collection; Claude reads it with the Artifact tool (`read_db`, collection
  `configs`) and drops it under `data/exit_lab/<name>.json`, then this loader
  takes it from there.

The simulation is not re-run here. The page marks options with Black-Scholes
off the entry-implied vol on 15-minute bars; reproducing that in Python would be
a second implementation to keep in step. The export *is* the result; this module
makes it a DataFrame joined to the trade log's full columns so it can be sliced
next to everything else in `data/`.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pandas as pd

HERE = Path(__file__).resolve().parent
DATA = HERE.parents[1] / "data"
TRADES = DATA / "dashboard_trades.parquet"
STORE = DATA / "exit_lab"
TZ = "America/New_York"


def load(src: str | Path | dict) -> tuple[dict, pd.DataFrame]:
    """(params, signals). `src` is a path to the export or the parsed object."""
    obj = src if isinstance(src, dict) else json.load(open(src))
    if obj.get("page") != "Sweep Exit Lab":
        raise ValueError("not a Sweep Exit Lab export")
    sig = pd.DataFrame(obj["signals"])
    if len(sig):
        # epoch minutes -> tz-aware ET, the same clock the trade log uses (naive ET there)
        for col in ("entry_epoch_min", "exit_epoch_min", "trigger_epoch_min"):
            sig[col.replace("_epoch_min", "_at")] = (
                pd.to_datetime(sig[col], unit="m", utc=True).dt.tz_convert(TZ).dt.tz_localize(None))
        # join the trade log's 56 columns on the trigger print
        log = pd.read_parquet(TRADES).reset_index().rename(columns={"index": "trade_index"})
        log = log.rename(columns={"ret": "hold_ret", "exit_price": "hold_exit_price", "entry_price": "fill_price"})
        extra = [c for c in log.columns if c not in sig.columns and c not in ("entry_ts", "trade_index")]
        if "trade_index" in sig.columns:
            # exports since v12 carry the trade log's row position: an exact key
            sig = sig.merge(log[["trade_index"] + extra], on="trade_index", how="left")
        else:
            # older exports carry epoch minutes while the log keeps milliseconds, and two
            # prints can share a minute (ARM printed three times in 1:17-1:18 PM). Pair
            # them one-to-one in time order within each (ticker, minute).
            log["trigger_at"] = pd.to_datetime(log["entry_ts"]).dt.floor("min")
            log = log.sort_values("entry_ts")
            log["_k"] = log.groupby(["ticker", "trigger_at"]).cumcount()
            sig = sig.sort_values("trigger_epoch_min")
            sig["_k"] = sig.groupby(["ticker", "trigger_at"]).cumcount()
            sig = sig.merge(log[["ticker", "trigger_at", "_k", "trade_index"] + extra],
                            on=["ticker", "trigger_at", "_k"], how="left").drop(columns="_k")
        if sig["hold_ret"].isna().any():
            raise ValueError(f"{int(sig.hold_ret.isna().sum())} signals did not match the trade log")
    params = {"name": obj.get("name"), "saved_at": obj.get("savedAt"), "rules": obj["params"],
              "filters": obj["filters"], "summary": obj["summary"], "hold_to_expiry": obj["hold_to_expiry"]}
    return params, sig


def describe(params: dict, sig: pd.DataFrame) -> str:
    rules = {k: v for k, v in params["rules"].items() if v not in (None, 0, "skip") or k in ("budget", "acct")}
    lines = [f"config: {params['name'] or '(unnamed)'}  saved {params['saved_at']}",
             f"rules:   {rules}",
             f"filters: {params['filters'] or '{}'}",
             f"signals: {len(sig)}"]
    if len(sig):
        s = params["summary"]; b = params["hold_to_expiry"]
        lines.append(f"  win {s['win_rate']:.1%} · mean {s['mean_ret']:+.1%} · total ${s['total_pnl']:,.0f} · "
                     f"PF {s['profit_factor'] if s['profit_factor'] is not None else 'inf'} · maxDD {s['max_dd']:.1%}"
                     f"   (hold-to-expiry: win {b['win_rate']:.1%} · mean {b['mean_ret']:+.1%} · total ${b['total_pnl']:,.0f})")
        lines.append("  exits: " + ", ".join(f"{k} {v}" for k, v in sorted(s["exit_reasons"].items(), key=lambda kv: -kv[1])))
        cols = ["ticker", "entry_ts", "direction", "strike", "right", "expiry", "entry", "exit", "ret", "pnl", "exit_reason"]
        lines.append(sig[cols].to_string(index=False, float_format=lambda x: f"{x:,.2f}"))
    return "\n".join(lines)


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit("usage: python research/sweep_events/exit_lab.py <export.json> [--save]")
    params, sig = load(sys.argv[1])
    print(describe(params, sig))
    if "--save" in sys.argv and len(sig):
        STORE.mkdir(exist_ok=True)
        name = (params["name"] or Path(sys.argv[1]).stem).replace(" ", "_")
        out = STORE / f"{name}.parquet"
        sig.to_parquet(out, index=False)
        print(f"\nwrote {out}")
