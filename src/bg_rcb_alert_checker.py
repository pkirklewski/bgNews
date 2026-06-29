#!/usr/bin/env python3
"""Hourly RCB-class IMGW alert checker for Boguszów-Gorce.

Mirror of wch_rcb_alert_checker.py — see that file for design rationale.

Cron-driven (recommended: 0 * * * * — every hour on the hour). Each run:
  1. Fetches active IMGW warnings for teryt 0221 (powiat wałbrzyski,
     covers all 7 bg DISTRICTS — verified via Nominatim).
  2. Compares each warning's `id` against data/posted_alerts.json
  3. If at least ONE active warning ID is NEW → publishes a full alert
     post (alert-mode map with storm overlay + scarlet temps + broadcast
     banner + warning cards, share to ALL configured groups, AND post to
     each RCB_ALERT_EXTRA_PROFILE_POSTS profile wall e.g. Straż Miejska)
     and marks ALL currently-active warning IDs as posted in the state file.
  4. If no active warnings OR all already posted → exits quietly (0).

Two streams are intentionally independent:
  - Normal cron (06:30, 19:15):  bg_weather_map_selenium.main()
                                 always publishes a normal-mode map
                                 (NEVER touches alert logic)
  - Alert checker (hourly):      this script publishes alert posts
                                 only on NEW warning IDs, deduped
"""
import sys
import fcntl
import atexit
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

import bg_weather_map_selenium as bg

LOCK_FILE = PROJECT_ROOT / "locks" / "rcb_alert_checker.lock"


def _acquire_lock():
    LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    fh = open(LOCK_FILE, 'w')
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fh.write(str(__import__('os').getpid()))
        fh.flush()
        return fh
    except BlockingIOError:
        fh.close()
        return None


def main():
    lock = _acquire_lock()
    if not lock:
        print("[bg_rcb_alert_checker] another instance running — exiting")
        return 0
    atexit.register(lambda: lock.close())

    new_warnings, all_active = bg.get_new_imgw_alerts()

    if not all_active:
        print(f"[bg_rcb_alert_checker] no active IMGW warnings for teryt "
              f"{bg.IMGW_TERYT_CODES} — nothing to do")
        return 0

    if not new_warnings:
        wids = [x.get('id') for x in all_active]
        print(f"[bg_rcb_alert_checker] {len(all_active)} active warning(s), "
              f"all already posted — skip. IDs: {wids}")
        return 0

    new_ids = [x.get('id') for x in new_warnings]
    print(f"[bg_rcb_alert_checker] {len(new_warnings)} warning(s) to attempt "
          f"(NEW or under retry cap). IDs: {new_ids}")

    # CRITICAL: increment attempts counter BEFORE publish_rcb_alert_only.
    # This persists the attempt even if publish crashes mid-way, so the
    # MAX_ATTEMPTS_PER_WARNING_ID safety cap can eventually break the loop.
    # Without this, a verify-flaky warning would loop forever (the 14× spam
    # incident of 2026-06-29).
    bg._record_publish_attempt(new_warnings)

    success = bg.publish_rcb_alert_only(all_active)

    if success:
        print(f"[bg_rcb_alert_checker] ✅ Alert published — "
              f"state updated for {len(all_active)} warning ID(s)")
        return 0
    else:
        print(f"[bg_rcb_alert_checker] ❌ Alert publish FAILED — "
              f"attempts counter incremented; will retry up to "
              f"MAX_ATTEMPTS_PER_WARNING_ID={bg.MAX_ATTEMPTS_PER_WARNING_ID} times total")
        return 1


if __name__ == "__main__":
    sys.exit(main())
