---
name: archived-minute-bars
description: The 1Min sip_raw Alpaca cache lives in a local tar archive at ~/pxq_archive, not in the repo. Restore from it instead of refetching. Use whenever a task needs 1-minute equity bars, hits an empty data/alpaca_cache/stocks/1Min/sip_raw, or is about to run `python -m backtest fetch --timeframe 1Min`.
---

# Archived 1-minute bars — restore, don't refetch

On 2026-09-24 the 1.7 GB minute cache was moved out of the repo to free space.
It was **archived, not deleted**. Refetching it from Alpaca is slow and
rate-limited, and it is already sitting on this machine.

```
Archive : ~/pxq_archive/alpaca_1min_sip_raw_2026-09-24.tar   (1.7 GB, 174 symbols)
Restores: data/alpaca_cache/stocks/1Min/sip_raw/
```

## The rule

> Before any 1Min fetch, check the manifest. If the symbol is in the archive,
> restore it. Only pull from Alpaca for symbols the archive does not have.

`BarCache` (`backtest/data.py`) is transparent: once `SYMBOL.csv.gz` and
`SYMBOL.meta.json` are back in `sip_raw/`, `load_bars(...)` and
`python -m backtest ...` use them with no flag and no code change.

## Restore

```bash
.claude/skills/archived-minute-bars/restore.sh AAPL MSFT   # a few symbols
.claude/skills/archived-minute-bars/restore.sh --all       # all 174, ~1.7 GB
```

Restore **only the symbols the task needs**. Restoring everything puts the
1.7 GB straight back into the repo, which is what the cleanup was for.

Check membership without extracting anything:

```bash
grep -qx AAPL .claude/skills/archived-minute-bars/MANIFEST.txt && echo "in archive"
```

## Coverage is two cohorts, with a 2024 hole

`COVERAGE.tsv` holds each symbol's cached `[start, end]`. The archive is **not**
one continuous history:

| Symbols | Coverage |
|---|---|
| 95 | 2016-01-04 → 2023-12-29 |
| 74 | 2025-02-01 → 2026-09-10/15 |
| 5 | shorter, later fragments |

**Nothing covers 2024.** A symbol being present does not mean your date range is
served. Check `COVERAGE.tsv` first — the sidecar records the contiguous window,
so Alpaca is still called for any portion outside it, and that call is correct
behaviour, not a bug.

```bash
grep -P '^AAPL\t' .claude/skills/archived-minute-bars/COVERAGE.tsv
```

## DAL is corrupt

`DAL.csv.gz` was truncated on its original download (Sep 10), before archiving —
`gzip -t` fails on it. The archive preserved the damage faithfully. `restore.sh`
detects and drops it so it refetches cleanly. Never hand it to a backtest.

## Do not

- Do not `python -m backtest fetch --timeframe 1Min` for an archived symbol
  without checking the manifest first.
- Do not `--all` for a single-ticker job.
- Do not delete the archive. The repo copy is the disposable one.
- Do not move the archive into `~/Downloads` — that tree is Google-Drive-synced,
  which is what bloated the machine in the first place.

## Related

- [`data-routing`](../data-routing/SKILL.md) — Historical → Alpaca, Live → Robinhood.
- [`parquet-output`](../parquet-output/SKILL.md) — the cache is `.csv.gz`; derived output stays parquet.
