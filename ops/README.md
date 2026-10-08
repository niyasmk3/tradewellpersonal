# ops — everything needed to rebuild this deployment on a fresh Mac

Versioned copies of the files that live OUTSIDE the research repo (they sit
in the project root or ~/Library and are git-excluded there):

- `local-start.sh` → copy to the Tradewell project root. Patches a copy of
  upstream `start.sh` at launch (ports 8777/3777) — never edits upstream.
- `launchd/com.tradewell.*.plist` → copy to `~/Library/LaunchAgents/`, then
  `launchctl load` each. Jobs: stack (RunAtLoad, PATH includes nvm node),
  recorder (KeepAlive), report (16:00 Mon–Fri), morning (08:40 Mon–Fri:
  flushes the stale Kite token), uptime (300s watchdog), gold (live feeder).
  Paths inside assume /Users/niyas/Developer/Tradewell — edit if different.

NOT here, on purpose: `backend/.env` (secrets — restore from the app's own
iCloud state backup), `recorder/data/` (mirrored nightly to iCloud
`TradewellBackups/recorder-data/`), and the owner's app code (his repo).

Restore order: clone owner's repo → clone this repo into `recorder/` →
copy ops files → restore .env + data from iCloud → load launchd jobs.
