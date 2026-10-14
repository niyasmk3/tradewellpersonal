# ops — everything needed to rebuild this deployment on a fresh Mac

Versioned copies of the files that live OUTSIDE the research repo (they sit
in the project root or ~/Library and are git-excluded there):

- `local-start.sh` → copy to the Tradewell project root. Patches a copy of
  upstream `start.sh` at launch (ports 8777/3777) — never edits upstream.
  Also warns when the checkout is not on `local-ui` (see below).
- `local-ui.patch` → the one local commit on the app checkout's `local-ui`
  branch (see "Local UI patch" below). Fresh Mac: `git checkout -b local-ui
  main && git am recorder/ops/local-ui.patch` before starting the stack.
- `launchd/com.tradewell.*.plist` → copy to `~/Library/LaunchAgents/`, then
  `launchctl load` each. Jobs: stack (RunAtLoad, PATH includes nvm node),
  recorder (KeepAlive), report (16:00 Mon–Fri), morning (08:40 Mon–Fri:
  flushes the stale Kite token), uptime (300s watchdog), gold (live feeder).
  Paths inside assume /Users/niyas/Developer/Tradewell — edit if different.

NOT here, on purpose: `backend/.env` (secrets — restore from the app's own
iCloud state backup), `recorder/data/` (mirrored nightly to iCloud
`TradewellBackups/recorder-data/`), and the owner's app code (his repo).

Restore order: clone owner's repo → clone this repo into `recorder/` →
copy ops files → `git am` the local-ui patch → restore .env + data from
iCloud → load launchd jobs.

## Local UI patch (local-ui branch)

Since 14-Oct-2026 the app checkout (`/Users/niyas/Developer/Tradewell`)
carries ONE local branch, `local-ui`, cut from `main`. It holds a single
21-line commit to `frontend/components/Dashboard.tsx` and
`frontend/components/SignalPanel.tsx`: the dashboard polls
`/signals/{symbol}` for the symbol you are viewing, so BANKNIFTY and
FINNIFTY show their regime, score meters and card instead of "Signals run
on NIFTY only in Phase 2". The owner has not acted on the field note
(`notes/owner-note-2026-10-09-multi-underlying-ui.md`); this is the
stopgap until he does. Every hunk is marked `Local patch (local-ui branch)`.

Rules that keep the pull routine fast-forward:

- `main` is never edited and always equals `origin/main`. Nothing is ever
  pushed to origin (the owner's repo). `local-ui` is rebased, never merged.
- **The stack must run from `local-ui`.** `local-start.sh` runs `npm run
  dev` in whatever branch is checked out, so the checkout stays on
  `local-ui` at all times (`git -C /Users/niyas/Developer/Tradewell branch`
  shows `* local-ui`). The launcher warns on stderr (`logs/launchd.log`,
  the stack's launchd log) when it starts on any other branch; the stack
  still runs, just with the owner's NIFTY-only panel.
- The nightly `evening_report.sh` is unaffected: it pushes the `main` REF
  to `personal/app-mirror` and never touches the working tree. There is no
  automated pull — the pull is manual, below. (`update_after_close_today.sh`
  is the 04-Aug one-shot; run on `local-ui` it cannot fast-forward once the
  owner has new commits and stops with "PULL FAILED" — nothing deployed.
  Use the routine below instead of re-running it.)

After each owner update:

    git -C /Users/niyas/Developer/Tradewell checkout main
    git -C /Users/niyas/Developer/Tradewell pull --ff-only origin main
    git -C /Users/niyas/Developer/Tradewell checkout local-ui
    git -C /Users/niyas/Developer/Tradewell rebase main

The running Next dev server hot-reloads the rebased files. Restart the
stack only if the update touched `frontend/package.json` or `backend/`:

    launchctl unload ~/Library/LaunchAgents/com.tradewell.stack.plist
    launchctl load ~/Library/LaunchAgents/com.tradewell.stack.plist

Sanity check after the rebase (one commit, the two files, ~21 lines, tsc
clean):

    git -C /Users/niyas/Developer/Tradewell log --oneline main..local-ui
    git -C /Users/niyas/Developer/Tradewell diff --stat main..local-ui
    /Users/niyas/Developer/Tradewell/frontend/node_modules/.bin/tsc --noEmit -p /Users/niyas/Developer/Tradewell/frontend/tsconfig.json

On a rebase conflict (the owner edited the same lines): open the file git
names, keep the owner's version of everything except the hunks marked
`Local patch (local-ui branch)`, then

    git -C /Users/niyas/Developer/Tradewell add frontend/components
    git -C /Users/niyas/Developer/Tradewell rebase --continue

and run the sanity check. If the owner's change makes the patch moot (his
own multi-underlying panel) or impossible to re-apply, abort instead —
`git -C /Users/niyas/Developer/Tradewell rebase --abort` — and drop the
patch (below). Never resolve a conflict by touching `main`, and never
`git push origin` anything. After a resolution that changed the hunks,
refresh the versioned copy:

    git -C /Users/niyas/Developer/Tradewell format-patch main..local-ui --stdout > /Users/niyas/Developer/Tradewell/recorder/ops/local-ui.patch

Drop the patch once the owner ships the real fix:

    git -C /Users/niyas/Developer/Tradewell checkout main
    git -C /Users/niyas/Developer/Tradewell branch -D local-ui

then delete the branch-guard block from `local-start.sh` (both copies:
project root and `ops/local-start.sh`), delete `ops/local-ui.patch`, and
remove this section and the ops note in `experiment/PROGRAM.md`.
