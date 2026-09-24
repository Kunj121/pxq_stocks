#!/usr/bin/env bash
# Restore 1Min sip_raw bars from the local archive into the Alpaca cache.
# Usage: restore.sh SYMBOL [SYMBOL ...]     |     restore.sh --all
set -euo pipefail

ARCHIVE="${PXQ_ARCHIVE:-$HOME/pxq_archive/alpaca_1min_sip_raw_2026-09-24.tar}"
DEST="${BACKTEST_CACHE_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)/data/alpaca_cache}/stocks/1Min"

[[ -f "$ARCHIVE" ]] || { echo "No archive at $ARCHIVE" >&2; exit 1; }
[[ $# -gt 0 ]] || { echo "Usage: restore.sh SYMBOL [SYMBOL ...] | --all" >&2; exit 2; }

mkdir -p "$DEST"

if [[ "$1" == "--all" ]]; then
  echo "Restoring all 174 symbols (~1.7 GB) into $DEST/sip_raw ..."
  tar -xf "$ARCHIVE" -C "$DEST"
else
  members=()
  for s in "$@"; do
    S=$(printf %s "$s" | tr "[:lower:]" "[:upper:]")
    members+=("sip_raw/${S}.csv.gz" "sip_raw/${S}.meta.json")
  done
  tar -xf "$ARCHIVE" -C "$DEST" "${members[@]}"
fi

# DAL was truncated on its original download; never hand it back as valid cache.
if [[ -f "$DEST/sip_raw/DAL.csv.gz" ]] && ! gzip -t "$DEST/sip_raw/DAL.csv.gz" 2>/dev/null; then
  rm -f "$DEST/sip_raw/DAL.csv.gz" "$DEST/sip_raw/DAL.meta.json"
  echo "NOTE: DAL is corrupt in the archive and was dropped — it will refetch from Alpaca."
fi

echo "Restored into $DEST/sip_raw:"
ls -la "$DEST/sip_raw" | tail -n +2 | awk '{print "  " $NF, $5}' | grep csv.gz || true
