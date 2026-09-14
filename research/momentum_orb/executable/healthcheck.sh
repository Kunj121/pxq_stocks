#!/bin/bash
# Did this morning's ORB entry actually run? Verdict to stdout, the daily log,
# and a macOS notification.
#
# This is a LOCAL check by necessity: execution/logs is gitignored so trading
# records stay on the machine, which means a cloud agent cannot see it.
#
#   ./healthcheck.sh          # today
#   ./healthcheck.sh --quiet  # no desktop notification

set -uo pipefail
REPO="/Users/kunjshah/Downloads/pxq_stocks"
PY="$REPO/.venv/bin/python"
D=$(TZ=America/New_York date +%F)
LOG="$REPO/execution/logs/orb_daily_$D.log"
PLAN="$REPO/execution/logs/orb_plans/plan_$D.csv"
QUIET="${1:-}"

notify() {
  [ "$QUIET" = "--quiet" ] && return
  /usr/bin/osascript -e "display notification \"$2\" with title \"ORB $1\"" 2>/dev/null
}

DOW=$(TZ=America/New_York date +%u)
if [ "$DOW" -gt 5 ]; then echo "weekend — nothing expected"; exit 0; fi

if [ ! -f "$LOG" ]; then
  MSG="no log for $D — cron never fired (Mac asleep at 08:35 CT?)"
  echo "FAIL: $MSG"; notify "DID NOT RUN" "$MSG"; exit 1
fi

if grep -q "declining" "$LOG"; then
  MSG="$(grep 'declining' "$LOG" | tail -1)"
  echo "FAIL: entry declined — $MSG"; notify "DECLINED" "outside its window"; exit 1
fi

# grep -c prints 0 AND exits 1 on no match, so `|| echo 0` would emit "0\n0"
# and break the integer test below. Swallow the exit code instead.
SUBMITTED=$(grep -cE '^[[:space:]]+[A-Z]+[[:space:]]+(bracket|simple)' "$LOG" 2>/dev/null) || true
SUBMITTED=${SUBMITTED:-0}
RANGE=$(grep -o 'opening range complete at [0-9:]*' "$LOG" | tail -1)
POS=$("$PY" -c "
import sys; sys.path.insert(0,'$REPO/research/momentum_orb/executable')
from broker import PaperBroker
with PaperBroker() as b:
    print(f'{len(b.positions())} positions, {len(b.orders())} resting orders')
" 2>/dev/null || echo "account unreachable")

if [ "$SUBMITTED" -gt 0 ]; then
  MSG="$SUBMITTED orders submitted · $POS"
  echo "OK: $MSG"
  [ -n "$RANGE" ] && echo "    $RANGE"
  [ -f "$PLAN" ] && echo "    plan: $(( $(wc -l < "$PLAN") - 1 )) names"
  notify "RAN" "$MSG"
else
  MSG="log exists but no orders submitted — check $LOG"
  echo "WARN: $MSG"; notify "NO ORDERS" "$MSG"; exit 1
fi
