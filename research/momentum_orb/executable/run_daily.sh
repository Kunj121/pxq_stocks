#!/bin/bash
# Daily 5-minute ORB driver — Alpaca PAPER, Robinhood-shaped constraints.
#
#   ./run_daily.sh enter     # 09:35-09:45 ET
#   ./run_daily.sh flatten   # 15:50-16:00 ET
#   ./run_daily.sh report    # after the close (also rebuilds the dashboard)
#   ./run_daily.sh dashboard # rebuild the dashboard on demand
#
# Each phase self-guards on the Eastern-time clock, so the cron entry can be
# written in local time without DST arithmetic: if the wall clock drifts out of
# the window, the phase declines instead of trading at the wrong moment.
#
# DRY RUN is the default everywhere. Set ORB_PLACE=1 to actually submit.

set -uo pipefail
REPO="/Users/kunjshah/Downloads/pxq_stocks"
PY="$REPO/.venv/bin/python"
RUNNER="$REPO/research/momentum_orb/executable/daily_orb.py"
LOGDIR="$REPO/execution/logs"
mkdir -p "$LOGDIR"

PHASE="${1:-}"
ET_HHMM=$(TZ=America/New_York date +%H%M)
ET_DOW=$(TZ=America/New_York date +%u)      # 1=Mon .. 7=Sun
STAMP=$(TZ=America/New_York date +%Y-%m-%dT%H:%M:%S%z)
LOG="$LOGDIR/orb_daily_$(TZ=America/New_York date +%Y-%m-%d).log"

say() { echo "[$STAMP] $*" | tee -a "$LOG"; }

if [ "$ET_DOW" -gt 5 ]; then
  say "weekend — nothing to do"; exit 0
fi

PLACE_FLAG=""
if [ "${ORB_PLACE:-0}" = "1" ]; then PLACE_FLAG="--place"; fi

case "$PHASE" in
  enter)
    # The opening range closes at 09:35. Entering much later than 09:45 means
    # the breakout has already happened without you.
    # 09:35 is the earliest legitimate moment: the range has just closed. The
    # runner itself blocks until the bars are queryable, so firing at 09:35
    # beats padding the schedule — 44% of breakouts trigger inside the first
    # minute and carry most of the profit.
    if [ "$ET_HHMM" \< "0935" ] || [ "$ET_HHMM" \> "0945" ]; then
      say "enter: $ET_HHMM ET is outside 09:35-09:45 — declining"; exit 0
    fi
    say "enter (place=${ORB_PLACE:-0})"
    "$PY" "$RUNNER" enter $PLACE_FLAG 2>&1 | tee -a "$LOG"
    ;;
  flatten)
    if [ "$ET_HHMM" \< "1545" ] || [ "$ET_HHMM" \> "1600" ]; then
      say "flatten: $ET_HHMM ET is outside 15:45-16:00 — declining"; exit 0
    fi
    say "flatten (place=${ORB_PLACE:-0})"
    "$PY" "$RUNNER" flatten $PLACE_FLAG 2>&1 | tee -a "$LOG"
    ;;
  report)
    say "report"
    "$PY" "$RUNNER" report 2>&1 | tee -a "$LOG"
    # Rebuild the dashboard last, so it reflects the completed session.
    say "dashboard"
    "$PY" "$REPO/research/momentum_orb/executable/dashboard.py" 2>&1 | tee -a "$LOG"
    ;;
  dashboard)
    # shift past the phase name so extra flags (--date, --open) pass through
    # without empty positionals reaching argparse.
    shift
    "$PY" "$REPO/research/momentum_orb/executable/dashboard.py" "$@" 2>&1 | tee -a "$LOG"
    ;;
  status)
    "$PY" "$RUNNER" status 2>&1 | tee -a "$LOG"
    ;;
  *)
    echo "usage: $0 {enter|flatten|report|dashboard|status}" >&2; exit 2
    ;;
esac
