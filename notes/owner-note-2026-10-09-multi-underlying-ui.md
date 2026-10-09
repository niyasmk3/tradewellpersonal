# Field note: the dashboard hides non-NIFTY signals once SIGNAL_UNDERLYINGS is widened

**From:** Niyas's deployment · 09-Oct-2026 · pull-only, no tracked files edited.

With `SIGNAL_UNDERLYINGS=NIFTY,BANKNIFTY,FINNIFTY` the backend does run all
three engines (boot log: `signals on ['NIFTY', 'BANKNIFTY', 'FINNIFTY']`;
`GET /signals/BANKNIFTY` returns a live regime/score/action), and cards for
every symbol reach the paper book, the archive and the alert webhook. But
the dashboard can't show them:

- `frontend/components/Dashboard.tsx:52-69` polls `api.signal("NIFTY", mode)`
  for all three modes regardless of the viewed symbol.
- `frontend/components/SignalPanel.tsx:305` renders "Signals run on NIFTY
  only in Phase 2. Switch to NIFTY to see setups." for any other symbol.

Both predate the knob doing anything beyond NIFTY. Suggested change: poll
`api.signal(symbol, mode)` for the viewed symbol (keep the always-on NIFTY
polls for alerts/chime if you want the "fire while watching another chart"
behaviour), and drop the Phase-2 message when the symbol is in the
configured list (the backend already 404s symbols that aren't). The right
rail's Signals history has the same NIFTY assumption.

Workaround on my side meanwhile: a static all-symbol board on the research
origin polling `/signals/{symbol}` for each symbol × mode — read-only, no
app code touched.
