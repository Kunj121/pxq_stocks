#!/bin/bash
# Time-based supervisor for the daily ORB — an alternative to cron.
#
# macOS gates `crontab` writes behind Full Disk Access, and the permission
# dialog cannot be answered from a non-interactive shell, so installing the
# crontab hangs. This loop does the same job without needing that permission:
# it wakes every 20s, checks the Eastern clock, and runs each phase once per day
# inside its window.
#
#   nohup ./supervisor.sh > /tmp/orb_supervisor.log 2>&1 &
#   pkill -f supervisor.sh        # to stop
#
# Weaker than cron in one way that matters: it dies on reboot, logout, or if the
# machine sleeps through the window. Cron is still the durable answer — this is
# the one that can be armed without you at the keyboard.

set -uo pipefail
REPO="/Users/kunjshah/Downloads/pxq_stocks"
RUN="$REPO/research/momentum_orb/executable/run_daily.sh"
STATE="/tmp/orb_supervisor_state"
mkdir -p "$STATE"
export ORB_PLACE="${ORB_PLACE:-1}"

echo "supervisor up $(TZ=America/New_York date) | ORB_PLACE=$ORB_PLACE"

ran_today() { [ -f "$STATE/$1_$(TZ=America/New_York date +%F)" ]; }
mark_ran()  { touch "$STATE/$1_$(TZ=America/New_York date +%F)"; }

while true; do
  HHMM=$(TZ=America/New_York date +%H%M)
  DOW=$(TZ=America/New_York date +%u)
  if [ "$DOW" -le 5 ]; then
    # run_daily.sh re-checks the window itself; these bounds just avoid
    # invoking it hundreds of times a day.
    if [ "$HHMM" \> "0936" ] && [ "$HHMM" \< "0945" ] && ! ran_today enter; then
      echo "--- enter $(TZ=America/New_York date) ---"; "$RUN" enter; mark_ran enter
    fi
    if [ "$HHMM" \> "1555" ] && [ "$HHMM" \< "1600" ] && ! ran_today flatten; then
      echo "--- flatten $(TZ=America/New_York date) ---"; "$RUN" flatten; mark_ran flatten
    fi
    if [ "$HHMM" \> "1609" ] && [ "$HHMM" \< "1620" ] && ! ran_today report; then
      echo "--- report $(TZ=America/New_York date) ---"; "$RUN" report; mark_ran report
    fi
  fi
  sleep 20
done
