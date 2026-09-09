---
name: parquet-output
description: Parquet is the default on-disk format for every tabular artefact in pxq_stocks — downloaded bars, backtest equity curves, fills, and weights. Use whenever writing, reading, or naming a data file, saving a DataFrame, or choosing an output path or format in this repo.
---

# Parquet is the default output format

Every tabular artefact this repo writes is **parquet** unless someone deliberately asks
for something else. CSV is an escape hatch, not the norm.

## Why

| | CSV | Parquet |
|---|---|---|
| META 1Min × 3y | 22 MB | **7.1 MB** |
| Timezone-aware index | lost — reparse every read | preserved |
| MultiIndex | flattened to columns | preserved |
| dtypes | re-inferred, sometimes wrongly | stored in the file |
| Read | full text parse | columnar, selective |

The timezone point is the one that bites. A CSV of minute bars round-trips
`2023-09-11 09:30:00-04:00` into an object-dtype string column that silently breaks
`.dt` accessors and mixes EDT/EST offsets. Parquet gives back a real
`America/New_York` DatetimeIndex.

## Writing

Use the shared writer — never call `to_csv` / `to_parquet` directly:

```python
from backtest.writers import write_frame, read_frame, suffix_for, with_format

write_frame(df, "data/META_1Min.parquet")          # dispatches on extension
write_frame(df, path, fmt="parquet", index=True)   # or explicitly
write_frame(fills, path, fmt=fmt, index=False)     # index=False for flat tables
df = read_frame("data/META_1Min.parquet")
```

`write_frame` creates parent directories, names an unnamed single-level index via
`index_label`, leaves a MultiIndex alone, and raises a clear error if pyarrow is missing.

## CLI

```bash
python -m backtest fetch --symbols META --start 2023-09-09 --timeframe 1Min
#   -> data/META_1Min.parquet

python -m backtest fetch --symbols META --start 2023-09-09 --format csv
#   -> data/META_1Min.csv          (deliberate opt-out)

python -m backtest fetch --symbols META --start 2023-09-09 --out data/x.csv
#   -> data/x.csv                  (explicit extension wins over --format)
```

`--format {parquet,csv}` applies to both `fetch` and `run`. For `run` it sets the format
of `equity`, `fills`, and `weights` in the report directory; `equity.png` and
`summary.md` are unaffected.

## Naming

`data/<SYMBOLS>_<timeframe>.parquet` — e.g. `data/META_1Min.parquet`,
`data/AAPL-MSFT_1Day.parquet`. Don't put the span in the name; the file's own index
carries it, and a stale `_3y` suffix outlives the range it described.

## Reading elsewhere

```python
import pandas as pd
df = pd.read_parquet("data/META_1Min.parquet")   # needs pyarrow
```

`pyarrow` is in `requirements.txt`. Anything that reads these files needs it.

## Exceptions — leave these alone

- `summary.md`, `equity.png` — not tabular.
- `data/alpaca_cache/**/*.csv.gz` — the internal bar cache. Deliberately still gzipped
  CSV; converting it orphans every existing cache entry for no user-visible gain. It is
  not "output" and nothing reads it directly.
- Anything a user explicitly asks for as CSV.

## Never

- `df.to_csv(...)` as the default path for a new artefact — use `write_frame`.
- Writing both a CSV and a parquet of the same data. Pick one; parquet.
- Putting a span (`_3y`, `_2023`) in a data filename.
