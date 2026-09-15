"""Did this morning's entry actually run? Verdict to stdout and a notification.

Replaces healthcheck.sh, which is unschedulable: macOS TCC refuses /bin/bash
from a scheduler when the script lives under ~/Downloads. launchd CAN run the
venv python from there, so the check is Python.

The distinction that matters is between "never fired" and "fired and refused" —
on 2026-09-15 cron ran at 08:35:00 exactly and was denied, which looks identical
to a sleeping Mac unless you read /var/mail. Each failure gets its own verdict.
"""

from __future__ import annotations

import subprocess
import sys
from datetime import date
from pathlib import Path

import pandas as pd

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
sys.path[:0] = [str(HERE), str(ROOT)]

LOG_DIR = ROOT / "execution" / "logs"


def notify(title: str, msg: str) -> None:
    try:
        subprocess.run(["/usr/bin/osascript", "-e",
                        f'display notification "{msg}" with title "ORB {title}"'],
                       check=False, timeout=15)
    except Exception:
        pass


def main() -> int:
    sess = pd.Timestamp.now(tz="America/New_York").date()
    if pd.Timestamp(sess).weekday() > 4:
        print("weekend — nothing expected")
        return 0

    launchd_log = LOG_DIR / "launchd_enter.log"
    plan = LOG_DIR / "orb_plans" / f"plan_{sess}.csv"
    text = launchd_log.read_text() if launchd_log.exists() else ""

    if not launchd_log.exists() or not text.strip():
        v, m = "DID NOT RUN", "no launchd output — Mac asleep at 08:35?"
    elif "standing down" in text.rsplit("profile:", 1)[-1]:
        v, m = "STOOD DOWN", "fired outside its ET window"
    elif not plan.exists():
        v, m = "NO PLAN", "ran but produced no plan — check launchd_enter.log"
    else:
        p = pd.read_csv(plan)
        submitted = sum(1 for ln in text.splitlines()
                        if " bracket" in ln or " simple (" in ln)
        timing = [ln for ln in text.splitlines() if "opening range complete" in ln]
        if submitted:
            v = "RAN"
            m = f"{submitted} orders from a {len(p)}-name plan"
        else:
            v, m = "NO ORDERS", f"{len(p)}-name plan but nothing submitted"
        if timing:
            m += f" · {timing[-1].split('complete at')[-1].strip()}"

    print(f"{v}: {m}")
    try:
        sys.path.insert(0, str(HERE))
        from broker import PaperBroker
        with PaperBroker() as b:
            b.assert_paper()
            print(f"  account: {len(b.positions())} positions, "
                  f"{len(b.orders())} resting orders")
    except Exception as exc:
        print(f"  account unreachable: {type(exc).__name__}")
    notify(v, m)
    return 0 if v == "RAN" else 1


if __name__ == "__main__":
    raise SystemExit(main())
