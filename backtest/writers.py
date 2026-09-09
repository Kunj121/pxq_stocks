"""Tabular output. Parquet is the default format for every artefact this package writes.

Parquet round-trips dtypes, timezone-aware timestamps, and the index without the
string-reparsing that CSV forces on every read. CSV stays available for hand-inspection
and for tools that cannot read parquet.
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd

DEFAULT_FORMAT = "parquet"
FORMATS = ("parquet", "csv")

_SUFFIX = {"parquet": ".parquet", "csv": ".csv"}
# Recognised on read/write dispatch. CSV may arrive gzipped from the bar cache.
_FROM_SUFFIX = {".parquet": "parquet", ".pq": "parquet", ".csv": "csv", ".gz": "csv"}


def suffix_for(fmt: str) -> str:
    """File extension for a format name."""
    _validate(fmt)
    return _SUFFIX[fmt]


def format_for(path: str | Path) -> str:
    """Infer the format from a path's extension, defaulting to parquet."""
    return _FROM_SUFFIX.get(Path(path).suffix.lower(), DEFAULT_FORMAT)


def with_format(path: str | Path, fmt: str) -> Path:
    """Return ``path`` carrying the extension for ``fmt``."""
    return Path(path).with_suffix(suffix_for(fmt))


def _validate(fmt: str) -> None:
    if fmt not in FORMATS:
        raise ValueError(f"Unknown format {fmt!r}. Use one of: {', '.join(FORMATS)}")


def write_frame(
    frame: pd.DataFrame,
    path: str | Path,
    *,
    fmt: str | None = None,
    index: bool = True,
    index_label: str | None = None,
) -> Path:
    """Write ``frame`` to ``path``, dispatching on ``fmt`` or the path extension.

    Parent directories are created. Returns the path actually written.
    """
    path = Path(path)
    fmt = fmt or format_for(path)
    _validate(fmt)
    path.parent.mkdir(parents=True, exist_ok=True)

    if fmt == "parquet":
        out = frame if index else frame.reset_index(drop=True)
        # Name an unnamed single-level index so the column survives the round trip.
        # A MultiIndex already carries its own names and must not take a scalar.
        if index and index_label and out.index.nlevels == 1 and out.index.name is None:
            out = out.rename_axis(index_label)
        try:
            out.to_parquet(path, engine="pyarrow", compression="snappy", index=index)
        except ImportError as exc:  # pragma: no cover - depends on the environment
            raise RuntimeError(
                "Parquet output needs pyarrow. Install it with "
                "`pip install pyarrow`, or pass --format csv."
            ) from exc
    else:
        frame.to_csv(path, index=index, index_label=index_label)
    return path


def read_frame(path: str | Path, *, fmt: str | None = None) -> pd.DataFrame:
    """Read a frame written by :func:`write_frame`."""
    path = Path(path)
    fmt = fmt or format_for(path)
    _validate(fmt)
    if fmt == "parquet":
        return pd.read_parquet(path, engine="pyarrow")
    return pd.read_csv(path, index_col=0)
